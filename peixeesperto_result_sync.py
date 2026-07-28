"""Sincroniza resultados de apostas do site resultados.peixeesperto.com.br com o Bet-Analytix."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from typing import TYPE_CHECKING

from bet_analytix_writer import BetAnalytixWriter
from discord_config import PeixeEspertoSettings
from discord_database import DiscordSignalStore, tip_from_payload

if TYPE_CHECKING:
    from userbot_telegram_notifier import UserbotTelegramNotifier


logger = logging.getLogger(__name__)


_PEIXEESPERTO_BASE_URL = "https://resultados.peixeesperto.com.br"
_STATE_MAP = {
    "Ganha": 1,
    "Perdida": 2,
    "Empate/Anulada": 3,
}


@dataclass(frozen=True)
class PeixeEspertoBet:
    """Aposta normalizada retornada pela API do PeixeEsperto."""

    message_id: int
    event: str
    pick: str
    bookmaker: str
    tipster: str
    sport: str
    odd: float
    stake: float
    profit: float
    estado: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class _LocalCandidate:
    """Candidato local indexado para matching rapido."""

    job_id: int
    source_bet_id: int
    bet_analytix_bet_id: int
    tip: Any
    odd: float
    stake: float
    event_norm: str
    pick_norm: str
    bookmaker_norm: str
    source_bookmaker: str
    source_tipster: str | None


class PeixeEspertoResultSync:
    """Busca resultados no PeixeEsperto e reflete no Bet-Analytix."""

    def __init__(
        self,
        settings: PeixeEspertoSettings,
        store: DiscordSignalStore,
        writer: BetAnalytixWriter,
        bookmaker_aliases: dict[str, str] | None = None,
        notifier: "UserbotTelegramNotifier | None" = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._writer = writer
        self._bookmaker_equivalence = _build_bookmaker_equivalence(bookmaker_aliases or {})
        self._notifier = notifier
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "Referer": f"{_PEIXEESPERTO_BASE_URL}/",
            }
        )

    def sync_once(self) -> tuple[int, int, int, int]:
        """Executa uma rodada de sincronizacao.

        Retorna (atualizadas, ambiguas, ja_resolvidas, ignoradas).
        Eh seguro contra execucoes simultaneas: se outra thread ja estiver
        rodando, retorna zeros imediatamente.
        """

        if not self._settings.enabled:
            return 0, 0, 0, 0

        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            logger.debug("PeixeEsperto sync ja em execucao; pulando rodada.")
            return 0, 0, 0, 0

        try:
            return self._sync_once_locked()
        finally:
            self._lock.release()

    def _sync_once_locked(self) -> tuple[int, int, int, int]:
        """Logica interna de sincronizacao (deve ser chamada com o lock adquirido)."""

        pending_jobs = self._store.get_pending_bets_for_result_sync(
            max_age_hours=self._settings.sync_max_age_hours,
            limit=1000,
        )
        if not pending_jobs:
            logger.debug("Nenhuma aposta pendente de sincronizacao com PeixeEsperto.")
            return 0, 0, 0, 0

        logger.info(
            "PeixeEsperto sync: %s apostas pendentes no banco local.",
            len(pending_jobs),
        )

        local_index = self._build_local_index(pending_jobs)
        if not local_index:
            logger.debug("Nenhum candidato local valido para sincronizacao.")
            return 0, 0, 0, 0

        source_tipsters_by_job = [
            _source_tipster_from_job(job)
            for job in pending_jobs
        ]
        source_tipsters = {
            source_tipster
            for source_tipster in source_tipsters_by_job
            if source_tipster
        }
        peixe_bets = self._fetch_recent_results(
            source_tipsters,
            include_unfiltered=any(
                source_tipster is None
                for source_tipster in source_tipsters_by_job
            ),
        )

        updated = 0
        ambiguous = 0
        already_resolved = 0
        ignored = 0
        updated_bet_ids: set[int] = set()

        for peixe_bet in peixe_bets:
            if peixe_bet.estado not in _STATE_MAP:
                ignored += 1
                continue

            matches_by_job: dict[int, _LocalCandidate] = {}
            for key in self._local_index_keys(peixe_bet.event, peixe_bet.pick, peixe_bet.bookmaker):
                for match in local_index.get(key, []):
                    matches_by_job[match.job_id] = match
            matches = list(matches_by_job.values())
            matches = [m for m in matches if m.bet_analytix_bet_id not in updated_bet_ids]
            matches = [
                match
                for match in matches
                if _tipsters_compatible(match.source_tipster, peixe_bet.tipster)
            ]

            if len(matches) == 0:
                continue
            if len(matches) > 1:
                narrowed_matches = _narrow_by_odd_and_stake(matches, peixe_bet)
                if len(narrowed_matches) == 1:
                    matches = narrowed_matches
                    logger.info(
                        "PeixeEsperto message_id=%s desambiguada com odd/stake.",
                        peixe_bet.message_id,
                    )
                else:
                    logger.warning(
                        "PeixeEsperto message_id=%s (%s / %s) tem %s correspondencias validas; pulando.",
                        peixe_bet.message_id,
                        peixe_bet.event,
                        peixe_bet.pick,
                        len(matches),
                    )
                    ambiguous += 1
                    continue

            candidate = matches[0]
            result = self._apply_result(peixe_bet, candidate)
            if result == "updated":
                updated += 1
                updated_bet_ids.add(candidate.bet_analytix_bet_id)
            elif result == "already_resolved":
                already_resolved += 1
                updated_bet_ids.add(candidate.bet_analytix_bet_id)
            elif result == "failed":
                ignored += 1

        logger.info(
            "PeixeEsperto sync concluido: %s atualizadas, %s ambiguas, "
            "%s ja resolvidas, %s ignoradas.",
            updated,
            ambiguous,
            already_resolved,
            ignored,
        )
        return updated, ambiguous, already_resolved, ignored

    def _build_local_index(
        self, jobs: list[Any]
    ) -> dict[tuple[str, str, str], list[_LocalCandidate]]:
        """Converte jobs do banco em indice rapido para matching."""

        index: dict[tuple[str, str, str], list[_LocalCandidate]] = {}
        for job in jobs:
            try:
                tip = tip_from_payload(json.loads(job["payload_json"]))
            except Exception:
                logger.exception("Falha ao parsear payload do job id=%s; ignorando.", job["id"])
                continue
            if tip.is_accumulator:
                continue
            if not tip.event or not tip.pick:
                logger.warning(
                    "Job id=%s sem evento/pick definido; ignorando no matching.",
                    job["id"],
                )
                continue

            # O bot armazena tip.pick como "Evento: Aposta" para exibicao no Bet-Analytix.
            # No matching usamos apenas a parte da aposta, sem o evento.
            real_pick = _extract_pick_from_label(tip.pick, tip.event)
            source_bookmaker = str(job["source_bookmaker_name"] or tip.bookmaker)
            source_tipster = _source_tipster_from_job(job)
            candidate = _LocalCandidate(
                job_id=int(job["id"]),
                source_bet_id=int(job["source_bet_id"]),
                bet_analytix_bet_id=int(job["bet_analytix_bet_id"]),
                tip=tip,
                odd=tip.odd,
                stake=tip.stake,
                event_norm=_normalize_match_text(tip.event),
                pick_norm=_normalize_match_text(real_pick),
                bookmaker_norm=_bookmaker_canonical(
                    tip.bookmaker, self._bookmaker_equivalence
                ),
                source_bookmaker=source_bookmaker,
                source_tipster=source_tipster,
            )
            bookmaker_keys = _bookmaker_match_keys(tip.bookmaker, self._bookmaker_equivalence)
            bookmaker_keys.update(
                _bookmaker_match_keys(source_bookmaker, self._bookmaker_equivalence)
            )
            for bookmaker_key in bookmaker_keys:
                key = (candidate.event_norm, candidate.pick_norm, bookmaker_key)
                index.setdefault(key, []).append(candidate)

        # Loga conflitos locais para facilitar debug
        for key, candidates in index.items():
            if len(candidates) > 1:
                logger.warning(
                    "Multiplas apostas locais identicas encontradas para %s: %s. "
                    "O sync exigira match unico por evento/pick/casa.",
                    key,
                    [c.bet_analytix_bet_id for c in candidates],
                )

        return index

    def _local_index_keys(
        self, event: str, pick: str, bookmaker: str
    ) -> set[tuple[str, str, str]]:
        event_norm = _normalize_match_text(event)
        pick_norm = _normalize_match_text(pick)
        return {
            (event_norm, pick_norm, bookmaker_key)
            for bookmaker_key in _bookmaker_match_keys(
                bookmaker, self._bookmaker_equivalence
            )
        }

    def _fetch_recent_results(
        self,
        source_tipsters: set[str] | None = None,
        include_unfiltered: bool = False,
    ) -> list[PeixeEspertoBet]:
        """Busca paginas recentes de resultados do PeixeEsperto.

        Para de buscar quando:
        - Encontra apostas mais antigas que o cutoff configurado;
        - Alcanca o maximo de paginas;

        Quando os tipsters de origem sao conhecidos, consulta cada um
        separadamente para evitar que apostas de outros tipsters ocupem as
        primeiras paginas da API.
        """

        results_by_message_id: dict[int, PeixeEspertoBet] = {}
        cutoff_ts = time.time() - self._settings.sync_max_age_hours * 3600
        already_synced = self._store.get_synced_message_ids()
        tipster_filters: list[str | None] = sorted(source_tipsters or set())
        if include_unfiltered or not tipster_filters:
            tipster_filters.append(None)

        now_utc = datetime.now(timezone.utc)
        safe_start_date = (
            now_utc - timedelta(hours=self._settings.sync_max_age_hours, days=1)
        ).strftime("%Y-%m-%d")
        safe_end_date = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")

        for tipster_filter in tipster_filters:
            for page in range(1, self._settings.sync_max_pages + 1):
                params = {
                    "sort_by": "Data",
                    "sort_order": "desc",
                    "page": page,
                    "per_page": self._settings.sync_per_page,
                    "start_date": safe_start_date,
                    "end_date": safe_end_date,
                    "estado": ",".join(_STATE_MAP),
                }
                if tipster_filter:
                    params["tipster"] = tipster_filter
                url = (
                    f"{_PEIXEESPERTO_BASE_URL}/api/grupo/{self._settings.group_slug}/apostas"
                    f"?{urlencode(params)}"
                )
                data = self._get_with_retry(url)
                if data is None:
                    break

                apostas = data.get("apostas") if isinstance(data, dict) else None
                if not isinstance(apostas, list):
                    logger.warning("Resposta inesperada do PeixeEsperto na pagina %s: %r", page, data)
                    break

                if not apostas:
                    break

                page_has_valid_date = False
                page_is_entirely_old = True
                for raw in apostas:
                    bet = self._parse_peixe_bet(raw)
                    if bet is None:
                        page_is_entirely_old = False
                        continue
                    try:
                        bet_ts = _parse_datetime(
                            bet.raw.get("Data") or "",
                            getattr(self._settings, "timezone_name", "America/Sao_Paulo"),
                        ).timestamp()
                    except ValueError:
                        logger.warning("Data invalida no PeixeEsperto message_id=%s; ignorando.", bet.message_id)
                        page_is_entirely_old = False
                        continue
                    page_has_valid_date = True
                    if bet_ts >= cutoff_ts:
                        page_is_entirely_old = False
                    if bet_ts < cutoff_ts or bet.message_id in already_synced:
                        continue
                    if bet.estado in _STATE_MAP:
                        results_by_message_id[bet.message_id] = bet

                if page_has_valid_date and page_is_entirely_old:
                    logger.info(
                        "PeixeEsperto sync: pagina %s de tipster=%s so continha apostas antigas; parando.",
                        page,
                        tipster_filter or "*",
                    )
                    break

                current_page = data.get("current_page") if isinstance(data, dict) else page
                total_pages = data.get("pages") if isinstance(data, dict) else page
                if current_page >= total_pages:
                    break

        return list(results_by_message_id.values())

    def _get_with_retry(self, url: str, max_retries: int = 2) -> dict[str, Any] | None:
        """Faz GET na API do PeixeEsperto com retry simples."""

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                response = self._session.get(url, timeout=30)
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict):
                    return data
                logger.warning("Resposta do PeixeEsperto nao e objeto JSON: %r", data)
                return None
            except Exception as exc:
                last_error = exc
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning("Falha ao buscar PeixeEsperto (tentativa %s): %s. Retentando em %ss.", attempt + 1, exc, wait)
                    time.sleep(wait)
        logger.exception("Falha ao buscar resultados do PeixeEsperto apos retries: %s", last_error)
        return None

    def _parse_peixe_bet(self, raw: dict[str, Any]) -> PeixeEspertoBet | None:
        """Converte um item da API em dataclass normalizada."""

        try:
            return PeixeEspertoBet(
                message_id=int(raw["message_id"]),
                event=str(raw.get("Jogo") or "").strip(),
                pick=str(raw.get("Aposta") or "").strip(),
                bookmaker=str(raw.get("Casa") or "").strip(),
                tipster=str(raw.get("Tipster") or "").strip(),
                sport=str(raw.get("Esporte") or "").strip(),
                odd=float(str(raw.get("Odd") or "0").replace(",", ".")),
                stake=float(str(raw.get("Valor") or "0").replace(",", ".")),
                profit=float(str(raw.get("Lucro") or "0").replace(",", ".")),
                estado=str(raw.get("Estado") or "").strip(),
                raw=raw,
            )
        except (TypeError, ValueError, KeyError):
            logger.warning("Item invalido do PeixeEsperto ignorado: %r", raw)
            return None

    def _apply_result(
        self,
        peixe_bet: PeixeEspertoBet,
        candidate: _LocalCandidate,
    ) -> str:
        """Atualiza a aposta no Bet-Analytix e persiste o sync local.

        Retorna: 'updated', 'already_resolved', 'failed'.
        """

        bet_id = candidate.bet_analytix_bet_id
        state = _STATE_MAP[peixe_bet.estado]
        try:
            # Passa profit=None para que o Bet-Analytix calcule o lucro com base
            # na stake real da aposta local, nao na stake registrada no PeixeEsperto.
            response = self._writer.update_bet_state(
                bet_id=bet_id,
                state=state,
                profit=None,
            )
        except Exception:
            logger.exception(
                "Falha ao atualizar bet_id=%s para estado=%s (PeixeEsperto message_id=%s).",
                bet_id,
                state,
                peixe_bet.message_id,
            )
            return "failed"

        if response is None:
            # update_bet_state retorna None quando a aposta nao esta pendente ou nao foi encontrada
            logger.info(
                "PeixeEsperto message_id=%s → Bet-Analytix bet_id=%s nao encontrada ou ja resolvida; marcando como sincronizado.",
                peixe_bet.message_id,
                bet_id,
            )
            self._store.record_peixeesperto_sync(
                message_id=peixe_bet.message_id,
                estado=peixe_bet.estado,
                bet_analytix_bet_id=bet_id,
                matched_source_bet_id=candidate.source_bet_id,
            )
            return "already_resolved"

        self._store.record_peixeesperto_sync(
            message_id=peixe_bet.message_id,
            estado=peixe_bet.estado,
            bet_analytix_bet_id=bet_id,
            matched_source_bet_id=candidate.source_bet_id,
        )
        actual_profit = float(response.get("profit") or peixe_bet.profit or 0)
        logger.info(
            "PeixeEsperto message_id=%s → Bet-Analytix bet_id=%s atualizado para %s (profit=%s).",
            peixe_bet.message_id,
            bet_id,
            peixe_bet.estado,
            actual_profit,
        )
        self._notify_sync_result(peixe_bet, candidate, response)
        return "updated"


    def _notify_sync_result(
        self,
        peixe_bet: PeixeEspertoBet,
        candidate: _LocalCandidate,
        response: dict[str, Any],
    ) -> None:
        """Envia notificacao Telegram quando uma aposta e atualizada pelo sync."""

        if self._notifier is None:
            return
        try:
            tip = candidate.tip
            stake = float(response.get("stake") or tip.stake or 0)
            profit = float(response.get("profit") or peixe_bet.profit or 0)
            self._notifier.send_peixeesperto_sync_result(
                bet_analytix_bet_id=candidate.bet_analytix_bet_id,
                event=tip.event,
                pick=peixe_bet.pick,
                bookmaker=tip.bookmaker,
                odd=peixe_bet.odd,
                stake=stake,
                state_text=peixe_bet.estado,
                profit=profit,
            )
        except Exception:
            logger.exception("Falha ao enviar notificacao Telegram de sync PeixeEsperto.")


def _extract_pick_from_label(pick: str, event: str | None) -> str:
    """Remove o prefixo 'Evento: ' do pick quando o bot o armazenou assim."""

    if not event:
        return pick
    cleaned_event = " ".join(event.strip().split())
    cleaned_pick = " ".join(pick.strip().split())
    prefix = f"{cleaned_event}: "
    if cleaned_pick.startswith(prefix):
        return cleaned_pick[len(prefix):]
    return cleaned_pick


def _normalize_match_text(value: str) -> str:
    """Normaliza texto para comparacao tolerante: minusculas, sem acentos, espacos unificados."""

    cleaned = " ".join(str(value).strip().split())
    normalized = unicodedata.normalize("NFKD", cleaned.lower())
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return without_marks


def _build_bookmaker_equivalence(
    aliases: dict[str, str],
) -> dict[str, str]:
    """Constroi mapa de equivalencia de nomes de casas.

    Permite match bidirecional: se o sinal manda 'Pitaco' e o Bet-Analytix
    usa 'Rei Do Pitaco', ambos convergem para o mesmo nome canonico.
    Tambem inclui variaoes comuns de capitalizacao.
    """

    equivalence: dict[str, str] = {}
    for original, mapped in aliases.items():
        canonical = _normalize_bookmaker_text(mapped)
        for name in (original, mapped):
            equivalence[_normalize_bookmaker_text(name)] = canonical
    return equivalence


def _bookmaker_canonical(name: str, equivalence: dict[str, str]) -> str:
    """Retorna nome canonico da casa para matching."""

    normalized = _normalize_bookmaker_text(name)
    if normalized in equivalence:
        return equivalence[normalized]
    return normalized


def _normalize_bookmaker_text(value: str) -> str:
    """Normaliza casa removendo acentos, espacos e pontuacao."""

    return "".join(char for char in _normalize_match_text(value) if char.isalnum())


def _bookmaker_match_keys(name: str, equivalence: dict[str, str]) -> set[str]:
    """Gera chaves equivalentes por alias e pela presenca do sufixo ``bet``."""

    canonical = _bookmaker_canonical(name, equivalence)
    keys = {canonical}
    if canonical.endswith("bet") and len(canonical) > 3:
        without_bet = canonical[:-3]
        if len(without_bet) >= 3:
            keys.add(without_bet)
    elif len(canonical) >= 3:
        keys.add(f"{canonical}bet")
    return keys


def _source_tipster_from_job(job: Any) -> str | None:
    """Recupera o ADM preservado ou extrai o legado da mensagem bruta."""

    try:
        stored = str(job["source_tipster_name"] or "").strip()
    except (IndexError, KeyError):
        stored = ""
    if stored:
        return stored

    try:
        raw_message = str(job["raw_message"] or "")
    except (IndexError, KeyError):
        return None
    match = re.search(r"ADM\s*:\s*([^\r\n]+)", raw_message, re.IGNORECASE)
    if not match:
        return None
    value = " ".join(match.group(1).strip().split())
    return value or None


def _normalize_tipster(value: str) -> str:
    normalized = _normalize_match_text(value).lstrip("@")
    return "".join(char for char in normalized if char.isalnum())


def _tipsters_compatible(source_tipster: str | None, api_tipster: str) -> bool:
    """Exige igualdade quando ambos os lados informam o tipster."""

    if not source_tipster or not api_tipster:
        return True
    return _normalize_tipster(source_tipster) == _normalize_tipster(api_tipster)


def _narrow_by_odd_and_stake(
    matches: list[_LocalCandidate],
    peixe_bet: PeixeEspertoBet,
) -> list[_LocalCandidate]:
    """Desempata apenas quando odd e stake apontam para um unico candidato."""

    return [
        match
        for match in matches
        if abs(match.odd - peixe_bet.odd) <= 0.005
        and abs(match.stake - peixe_bet.stake) <= 0.01
    ]


def _parse_datetime(value: str, timezone_name: str = "America/Sao_Paulo") -> datetime:
    """Parseia datas no formato '2026-07-02 16:54:32'."""

    try:
        timezone_info = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone_info = timezone.utc
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone_info)
        except ValueError:
            continue
    raise ValueError(f"Data invalida: {value!r}")
