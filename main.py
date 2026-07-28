"""Bot 24/7 para copiar entradas do tipster Águas Profundas via Telegram."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, TextIO

from bet_analytix_writer import BetAnalytixWriter
from betano_odds_monitor import BetanoOddsClient, BetanoOddsMonitor
from bet_scraper import BetAnalytixClient
from config import Settings, load_settings
from database import BetStateStore, TELEGRAM_DELIVERY_PRIVATE
from models import (
    Bet,
    Tipster,
    build_bookmaker_map,
    build_tipster_map,
    filter_bets_by_tipsters,
    parse_bet,
    resolve_tipster_id,
    to_int,
)
from operational_alerts import OperationalAlerter
from telegram_reaction_listener import TelegramReactionListener
from telegram_notifier import TelegramNotifier


logger = logging.getLogger(__name__)


class SingleInstanceError(RuntimeError):
    """Indica que outra instancia do bot ja esta rodando."""


@dataclass
class ReferenceCache:
    """Cache simples para referências de tipsters."""

    tipsters_by_id: dict[int, Tipster]
    bookmaker_names: dict[str, str]
    target_tipster_ids: tuple[int, ...]
    refreshed_at_monotonic: float


class BetMonitor:
    """Orquestra coleta, filtro, deduplicação e envio das apostas novas."""

    def __init__(
        self,
        settings: Settings,
        client: BetAnalytixClient,
        store: BetStateStore,
        notifier: TelegramNotifier,
        alerter: OperationalAlerter | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._store = store
        self._notifier = notifier
        self._alerter = alerter
        self._reference_cache: ReferenceCache | None = None

    def run_forever(self) -> None:
        """Executa o polling contínuo até interrupção manual."""

        logger.info(
            "Monitorando bankroll %s para os tipsters %s a cada %ss.",
            self._settings.bankroll_id,
            self._target_label(),
            self._settings.poll_interval_seconds,
        )

        while True:
            started_at = time.monotonic()
            try:
                self.poll_once()
            except Exception as exc:
                logger.exception("Falha no ciclo de monitoramento; o bot continuará tentando.")
                self._alert("Falha no ciclo de monitoramento", str(exc), "main_monitor_poll")

            finally:
                self._record_heartbeat()

            elapsed = time.monotonic() - started_at
            delay = max(1.0, self._settings.poll_interval_seconds - elapsed)
            time.sleep(delay)

    def poll_once(self) -> None:
        """Executa uma rodada completa de coleta e notificação."""

        references = self._get_references()
        raw_bets = self._client.fetch_all_bets()
        filtered_raw_bets = filter_bets_by_tipsters(
            raw_bets=raw_bets,
            tipsters_by_id=references.tipsters_by_id,
            target_names=self._settings.target_tipster_names,
            fallback_target_ids=references.target_tipster_ids,
        )
        bets = self._parse_bets(filtered_raw_bets, references.tipsters_by_id, references.bookmaker_names)
        new_bets = [bet for bet in bets if not self._store.has_notified(bet.id)]
        new_bets.sort(key=lambda bet: bet.id)

        if not self._store.is_initialized():
            self._handle_first_successful_poll(new_bets)
            return

        if not new_bets:
            logger.info("Nenhuma aposta nova de %s.", self._target_label())
            return

        for bet in new_bets:
            sent_message = self._notifier.send_bet(bet, chat_id=self._delivery_chat_id())
            self._store.save_telegram_message(sent_message.chat_id, sent_message.message_id, sent_message.text)
            self._store.mark_notified(bet)
            logger.info("Aposta nova enviada ao Telegram: bet_id=%s tipster=%s", bet.id, bet.tipster_name)

    def _handle_first_successful_poll(self, current_bets: list[Bet]) -> None:
        if self._settings.notify_existing_on_first_run:
            for bet in current_bets:
                sent_message = self._notifier.send_bet(bet, chat_id=self._delivery_chat_id())
                self._store.save_telegram_message(sent_message.chat_id, sent_message.message_id, sent_message.text)
                self._store.mark_notified(bet)
                logger.info("Aposta histórica enviada no primeiro boot: bet_id=%s", bet.id)
        else:
            inserted = self._store.mark_many_as_seen(current_bets)
            logger.info(
                "Primeiro boot concluído sem spam: %s apostas atuais foram marcadas como já vistas.",
                inserted,
            )
        self._store.set_initialized()

    def _parse_bets(
        self,
        raw_bets: list[dict[str, Any]],
        tipsters_by_id: dict[int, Tipster],
        bookmaker_names: dict[str, str],
    ) -> list[Bet]:
        parsed: list[Bet] = []
        for raw_bet in raw_bets:
            try:
                parsed.append(
                    parse_bet(
                        raw_bet=raw_bet,
                        tipsters_by_id=tipsters_by_id,
                        sport_names=self._settings.sport_names,
                        bookmaker_names=bookmaker_names,
                        timezone_name=self._settings.timezone,
                        default_tipster_name=self._settings.target_tipster_name,
                    )
                )
            except ValueError:
                logger.exception("Aposta ignorada por payload inesperado: %s", raw_bet)
        return parsed

    def _get_references(self) -> ReferenceCache:
        now = time.monotonic()
        if self._reference_cache and now - self._reference_cache.refreshed_at_monotonic < self._settings.reference_refresh_seconds:
            return self._reference_cache

        bankroll = self._client.fetch_bankroll()
        user_id = to_int(bankroll.get("id_user"))
        if user_id is None:
            raise RuntimeError(f"Não foi possível identificar id_user no payload da bankroll: {bankroll!r}")

        all_data = self._client.fetch_reference_data(user_id)
        tipsters_by_id = build_tipster_map(all_data)
        bookmaker_names = self._fetch_bookmaker_names()
        target_tipster_ids = self._resolve_target_tipster_ids(tipsters_by_id)

        if not target_tipster_ids:
            raise RuntimeError("Nenhum ID de tipster alvo disponível para filtragem.")

        self._reference_cache = ReferenceCache(
            tipsters_by_id=tipsters_by_id,
            bookmaker_names=bookmaker_names,
            target_tipster_ids=target_tipster_ids,
            refreshed_at_monotonic=now,
        )
        return self._reference_cache

    def _fetch_bookmaker_names(self) -> dict[str, str]:
        """Busca nomes das casas e aplica sobrescritas opcionais do `.env`."""

        try:
            bookmaker_names = build_bookmaker_map(self._client.fetch_bookmakers())
            logger.info("Catálogo de casas carregado: %s bookmakers.", len(bookmaker_names))
        except Exception:
            logger.exception("Não foi possível carregar o catálogo de casas; usando apenas BOOKMAKER_NAMES_JSON.")
            bookmaker_names = {}

        bookmaker_names.update(self._settings.bookmaker_names)
        return bookmaker_names

    def _resolve_target_tipster_ids(self, tipsters_by_id: dict[int, Tipster]) -> tuple[int, ...]:
        """Resolve todos os tipsters alvo por nome e aplica fallback por ID."""

        resolved_ids: list[int] = []
        for name in self._settings.target_tipster_names:
            resolved_id = resolve_tipster_id(tipsters_by_id, name)
            if resolved_id is None:
                logger.warning("Tipster %s não foi encontrado por nome nas referências.", name)
                continue
            resolved_ids.append(resolved_id)
            logger.info("Tipster alvo resolvido: %s -> ID %s.", name, resolved_id)

        for fallback_id in self._settings.target_tipster_ids:
            if fallback_id not in resolved_ids:
                resolved_ids.append(fallback_id)

        return tuple(resolved_ids)

    def _target_label(self) -> str:
        return ", ".join(self._settings.target_tipster_names)

    def _delivery_chat_id(self) -> str:
        mode = self._store.get_telegram_delivery_mode()
        if mode == TELEGRAM_DELIVERY_PRIVATE and self._settings.telegram_admin_user_id is not None:
            return str(self._settings.telegram_admin_user_id)
        return str(self._settings.telegram_chat_id)

    def _record_heartbeat(self) -> None:
        try:
            self._store.set_metadata("monitor_heartbeat_ts", str(int(time.time())))
        except Exception:
            logger.exception("Nao foi possivel gravar heartbeat do monitor.")

    def _alert(self, title: str, details: str | None, dedupe_key: str) -> None:
        if self._alerter is not None:
            self._alerter.send(title, details, dedupe_key=dedupe_key)


def configure_logging(settings: Settings) -> None:
    """Configura logging estruturado para operação contínua."""

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def main() -> None:
    """Ponto de entrada da aplicação."""

    settings = load_settings()
    configure_logging(settings)
    lock_path = settings.sqlite_path.parent / "bot.lock"

    try:
        with single_instance_lock(lock_path):
            run_bot(settings)
    except SingleInstanceError as exc:
        logger.error("%s", exc)


def run_bot(settings: Settings) -> None:
    """Inicializa os componentes e mantem o processo principal ativo."""

    store = BetStateStore(settings.sqlite_path)
    store.initialize()

    notifier = TelegramNotifier(settings)
    alerter = OperationalAlerter(settings, "bot proprio")
    try:
        notifier.send_startup()
        logger.info("Mensagem BOT ON enviada ao Telegram.")
    except Exception:
        logger.exception("Não foi possível enviar a mensagem BOT ON; o monitoramento continuará.")
        alerter.send("Falha ao enviar BOT ON", "O monitoramento continuara ativo.", "main_startup_message")

    alerter.send("BOT ON", "Bot proprio iniciado e listener ativo.", "main_started")

    monitor = BetMonitor(
        settings=settings,
        client=BetAnalytixClient(settings),
        store=store,
        notifier=notifier,
        alerter=alerter,
    )
    betano_client = BetanoOddsClient(settings) if settings.betano_monitor_enabled else None
    betano_monitor = (
        BetanoOddsMonitor(
            settings=settings,
            store=store,
            client=betano_client,
            notifier=notifier,
            alerter=alerter,
        )
        if betano_client is not None
        else None
    )

    if settings.copytrade_enabled:
        if settings.telegram_admin_user_id is None:
            raise RuntimeError("COPYTRADE_ENABLED=true exige TELEGRAM_ADMIN_USER_ID no .env.")

        monitor_thread = threading.Thread(target=monitor.run_forever, name="bet-monitor", daemon=True)
        monitor_thread.start()
        if betano_monitor is not None:
            betano_thread = threading.Thread(target=betano_monitor.run_forever, name="betano-odds-monitor", daemon=True)
            betano_thread.start()
            logger.info("Monitor de odds Betano habilitado.")
        else:
            logger.info("Monitor de odds Betano desabilitado por BETANO_MONITOR_ENABLED=false.")
        listener = TelegramReactionListener(
            settings=settings,
            store=store,
            writer=BetAnalytixWriter(settings),
            betano_client=betano_client,
            alerter=alerter,
        )
        listener.run_forever()
    else:
        if betano_monitor is not None:
            betano_thread = threading.Thread(target=betano_monitor.run_forever, name="betano-odds-monitor", daemon=True)
            betano_thread.start()
            logger.info("Monitor de odds Betano habilitado.")
        else:
            logger.info("Monitor de odds Betano desabilitado por BETANO_MONITOR_ENABLED=false.")
        monitor.run_forever()


@contextmanager
def single_instance_lock(lock_path: Path) -> Iterator[None]:
    """Impede duas instancias simultaneas usando um lock de arquivo."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    locked = False
    try:
        _lock_file(lock_file, lock_path)
        locked = True
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        yield
    finally:
        if locked:
            try:
                _unlock_file(lock_file)
            finally:
                lock_file.close()
        else:
            lock_file.close()


def _lock_file(lock_file: TextIO, lock_path: Path) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise SingleInstanceError(f"Outra instancia do bot ja esta rodando. Lock: {lock_path}") from exc
        return

    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise SingleInstanceError(f"Outra instancia do bot ja esta rodando. Lock: {lock_path}") from exc


def _unlock_file(lock_file: TextIO) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            logger.exception("Falha ao liberar lock de instancia.")
        return

    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        logger.exception("Falha ao liberar lock de instancia.")


if __name__ == "__main__":
    main()
