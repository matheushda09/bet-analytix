"""Modulo de controle de bankroll integrado ao Bet-Analytix.

Responsavel por ler o estado financeiro da bankroll, calcular exposicao
por casa de aposta e registrar movimentacoes manuais (depositos/saques).
"""

from __future__ import annotations

import logging
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from bet_analytix_writer import BetAnalytixWriter
from discord_config import BankrollSettings
from discord_database import DiscordSignalStore


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BankrollSummary:
    """Snapshot financeiro de uma bankroll."""

    current_capital: Decimal
    start_capital: Decimal
    profit: Decimal
    roi: Decimal
    progression: Decimal
    bets_pending: int
    stake_pending: Decimal
    total_bets: int
    bets_won: int
    bets_lost: int
    currency_symbol: str

    @property
    def is_profitable(self) -> bool:
        return self.profit >= 0


@dataclass(frozen=True)
class BookmakerExposure:
    """Dinheiro em apostas pendentes em uma casa especifica."""

    bookmaker_id: int
    bookmaker_name: str
    pending_stake: Decimal
    bet_count: int


@dataclass(frozen=True)
class Transaction:
    """Movimentacao manual registrada localmente."""

    id: int
    type: str
    amount: Decimal
    currency: str
    bookmaker_id: int | None
    bookmaker_name: str | None
    description: str | None
    created_at_ts: int
    created_by_user_id: int


@dataclass(frozen=True)
class BookmakerWithdrawalBalance:
    """Saldo disponivel para saque em uma casa especifica."""

    bookmaker_id: int
    bookmaker_name: str
    available: Decimal


@dataclass(frozen=True)
class BookmakerBet:
    """Aposta individual de uma casa de aposta."""

    bet_id: int
    bookmaker_id: int
    bookmaker_name: str
    label: str
    stake: Decimal
    odds: Decimal
    state: int
    profit: Decimal
    gain: Decimal
    event_datetime: datetime
    is_green: bool
    is_red: bool
    is_pending: bool


@dataclass(frozen=True)
class BookmakerBalance:
    """Saldo real de uma casa de aposta (depositos + ganhos - perdas - em jogo - saques)."""

    bookmaker_id: int
    bookmaker_name: str
    deposits: Decimal
    withdrawals: Decimal
    in_play: Decimal
    lost: Decimal
    won: Decimal
    available: Decimal


@dataclass(frozen=True)
class BankrollReport:
    """Relatorio consolidado de bankroll."""

    summary: BankrollSummary
    exposures: list[BookmakerExposure]
    today_transactions: list[Transaction]
    total_pending: Decimal
    today_deposits: Decimal
    today_withdrawals: Decimal
    withdrawal_balances: list[BookmakerWithdrawalBalance]
    total_available_to_withdraw: Decimal
    bookmaker_balances: list[BookmakerBalance]
    total_bookmaker_balance: Decimal
    currency_symbol: str
    generated_at_ts: int


class BankrollController:
    """Orquestra leitura da API e persistencia local de movimentacoes."""

    _PENDING_STATE = 0

    def __init__(
        self,
        writer: BetAnalytixWriter,
        store: DiscordSignalStore,
        settings: BankrollSettings,
        timezone_name: str = "America/Sao_Paulo",
    ) -> None:
        self._writer = writer
        self._store = store
        self._settings = settings
        try:
            self._timezone = ZoneInfo(timezone_name)
        except Exception:
            self._timezone = ZoneInfo("America/Sao_Paulo")

        self._green_cutoff_ts: int | None = None
        if settings.green_cutoff_utc:
            try:
                cutoff_dt = datetime.fromisoformat(settings.green_cutoff_utc)
                if cutoff_dt.tzinfo is None:
                    cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)
                self._green_cutoff_ts = int(cutoff_dt.timestamp())
            except Exception as exc:
                logger.warning("BANKROLL_GREEN_CUTOFF_UTC invalido (%s): %s", settings.green_cutoff_utc, exc)

        self._migrate_legacy_transactions()

    def _migrate_legacy_transactions(self) -> None:
        """Tenta vincular depositos/saques antigos sem casa a partir da descricao."""

        try:
            self._writer._load_bookmakers()
        except Exception as exc:
            logger.warning("Nao foi possivel carregar catalogo de casas para migracao: %s", exc)
            return

        bookmakers_by_name = self._writer._bookmakers_by_name
        if bookmakers_by_name is None:
            return

        rows = self._store.list_transactions(types=["deposit", "withdrawal"], limit=10000)
        migrated = 0
        for row in rows:
            bookmaker_id_raw = row["bookmaker_id"]
            description = row["description"]
            if bookmaker_id_raw is not None or not description:
                continue

            normalized = _normalize_text(str(description))
            bookmaker_id = bookmakers_by_name.get(normalized)
            if bookmaker_id is None:
                continue

            bookmaker_name = self._writer.get_bookmaker_name(bookmaker_id) or str(description)
            self._store.update_transaction_bookmaker(
                transaction_id=int(row["id"]),
                bookmaker_id=bookmaker_id,
                bookmaker_name=bookmaker_name,
            )
            migrated += 1

        if migrated > 0:
            logger.info("Migradas %s transacoes legadas sem bookmaker_id.", migrated)

    def fetch_summary(self) -> BankrollSummary:
        """Busca o resumo financeiro da bankroll no Bet-Analytix."""

        bankroll = self._writer.fetch_bankroll_summary()
        stats_global = bankroll.get("stats_global") or {}
        global_stats = stats_global.get("global") if isinstance(stats_global, dict) else {}

        def _dec(value: Any) -> Decimal:
            if value is None:
                return Decimal("0")
            try:
                return Decimal(str(value))
            except Exception:
                return Decimal("0")

        return BankrollSummary(
            current_capital=_dec(global_stats.get("currentCapital")),
            start_capital=_dec(global_stats.get("startCapital")),
            profit=_dec(global_stats.get("profit")),
            roi=_dec(global_stats.get("roi")),
            progression=_dec(global_stats.get("progression")),
            bets_pending=int(global_stats.get("betsPending") or 0),
            stake_pending=_dec(global_stats.get("stakePending")),
            total_bets=int(global_stats.get("totalBets") or 0),
            bets_won=int(global_stats.get("betsWon") or 0),
            bets_lost=int(global_stats.get("betsLost") or 0),
            currency_symbol=self._settings.currency_symbol,
        )

    def fetch_pending_by_bookmaker(self) -> list[BookmakerExposure]:
        """Calcula o dinheiro 'na rua' agrupado por casa de aposta."""

        exposures: dict[int, dict[str, Any]] = {}
        page = 1
        max_pages = max(1, self._settings.pending_bets_max_pages)

        while page <= max_pages:
            payload = self._writer.fetch_bankroll_bets_page(page=page)
            if not isinstance(payload, dict):
                break

            bets = payload.get("bets")
            if not isinstance(bets, list):
                break

            for bet in bets:
                if not isinstance(bet, dict):
                    continue
                state = bet.get("state")
                if state != self._PENDING_STATE:
                    continue

                bookmaker_id_raw = bet.get("bookmaker")
                bookmaker_id = self._to_int(bookmaker_id_raw)
                if bookmaker_id is None:
                    continue

                stake = self._to_decimal(bet.get("stake"))
                if stake is None or stake <= 0:
                    continue

                if bookmaker_id not in exposures:
                    name = self._writer.get_bookmaker_name(bookmaker_id)
                    exposures[bookmaker_id] = {
                        "name": name or f"Casa {bookmaker_id}",
                        "stake": Decimal("0"),
                        "count": 0,
                    }
                exposures[bookmaker_id]["stake"] += stake
                exposures[bookmaker_id]["count"] += 1

            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
            total_pages = self._to_int(pagination.get("totalPages")) or page
            has_next_page = bool(pagination.get("hasNextPage"))
            if page >= total_pages or not has_next_page:
                break
            page += 1

        if page >= max_pages and has_next_page:
            logger.warning(
                "Limite de paginas de apostas pendentes atingido (%s); exposicao por casa pode estar incompleta.",
                max_pages,
            )

        return [
            BookmakerExposure(
                bookmaker_id=bookmaker_id,
                bookmaker_name=data["name"],
                pending_stake=data["stake"],
                bet_count=data["count"],
            )
            for bookmaker_id, data in sorted(exposures.items(), key=lambda item: item[1]["stake"], reverse=True)
        ]

    def record_transaction(
        self,
        transaction_type: str,
        amount: Decimal,
        created_by_user_id: int,
        description: str | None = None,
        bookmaker_id: int | None = None,
        bookmaker_name: str | None = None,
    ) -> int:
        """Registra um deposito, saque ou ajuste manual."""

        amount_cents = int((amount * Decimal("100")).to_integral_value())
        return self._store.record_transaction(
            transaction_type=transaction_type,
            amount_cents=amount_cents,
            created_by_user_id=created_by_user_id,
            description=description,
            currency="BRL",
            bookmaker_id=bookmaker_id,
            bookmaker_name=bookmaker_name,
        )

    def list_today_transactions(self) -> list[Transaction]:
        """Retorna depositos/saques registrados hoje no timezone configurado."""

        now = datetime.now(self._timezone)
        start_of_day = datetime(now.year, now.month, now.day, tzinfo=self._timezone)
        since_ts = int(start_of_day.timestamp())
        return self.list_transactions_since(since_ts)

    def list_transactions_since(self, since_ts: int) -> list[Transaction]:
        """Retorna transacoes a partir de um timestamp."""

        rows = self._store.list_transactions(since_ts=since_ts, types=["deposit", "withdrawal"])
        return [self._row_to_transaction(row) for row in rows]

    def sync_green_bets(self) -> tuple[int, Decimal, int, Decimal]:
        """Sincroniza apostas e contabiliza transicoes pendente -> green/red.

        A logica principal e detectar apostas que MUDARAM de pendente (state=0)
        para green (state=1, profit>0) ou red (state=2, profit<0) entre uma
        sincronizacao e outra. Isso permite contabilizar corretamente apostas
        que o bot acompanhou desde o inicio, ignorando o historico antigo do
        Bet-Analytix.

        Na primeira execucao, apenas registra o estado de todas as apostas
        conhecidas, sem contabilizar nenhum resultado.

        Retorna a quantidade e valor de novos greens e reds contabilizados.
        """

        baseline_str = self._store.get_green_sync_state("last_seen_bet_id")
        baseline: int | None = self._to_int(baseline_str) if baseline_str is not None else None
        is_first_sync = baseline is None

        green_count = 0
        green_total = Decimal("0")
        red_count = 0
        red_total = Decimal("0")
        max_seen_id = baseline or 0
        page = 1

        while page <= self._settings.pending_bets_max_pages:
            payload = self._writer.fetch_bankroll_bets_page(page=page)
            if not isinstance(payload, dict):
                break

            bets = payload.get("bets")
            if not isinstance(bets, list):
                break

            for bet in bets:
                if not isinstance(bet, dict):
                    continue

                bet_id = self._to_int(bet.get("id"))
                if bet_id is None:
                    continue

                if bet_id > max_seen_id:
                    max_seen_id = bet_id

                state = self._to_int(bet.get("state"))
                profit = self._to_decimal(bet.get("profit"))
                profit_cents = int((profit * Decimal("100")).to_integral_value()) if profit is not None else 0
                is_green = state == 1 and profit is not None and profit > 0
                is_red = state == 2 or (profit is not None and profit < 0)

                previous = self._store.get_bet_state(bet_id)

                if is_first_sync:
                    # Na primeira vez so registramos o baseline e estados, nao contamos.
                    self._store.upsert_bet_state(bet_id, state or 0, profit_cents)
                    continue

                previous_state = self._to_int(previous["state"]) if previous is not None else None
                was_pending = previous_state is None or previous_state == self._PENDING_STATE

                if is_green and was_pending and not self._store.is_green_bet_recorded(bet_id):
                    # Filtro por data de corte configurada (se houver).
                    if self._green_cutoff_ts is not None:
                        bet_ts = self._to_int(bet.get("date"))
                        if bet_ts is not None and bet_ts < self._green_cutoff_ts:
                            self._store.upsert_bet_state(bet_id, state or 0, profit_cents)
                            continue

                    stake = self._to_decimal(bet.get("stake"))
                    gain = self._to_decimal(bet.get("gain"))
                    if stake is None or stake <= 0:
                        self._store.upsert_bet_state(bet_id, state or 0, profit_cents)
                        continue

                    return_amount = gain if gain is not None and gain > 0 else stake + profit
                    if return_amount <= 0:
                        self._store.upsert_bet_state(bet_id, state or 0, profit_cents)
                        continue

                    bookmaker_id = self._to_int(bet.get("bookmaker"))
                    if bookmaker_id is None:
                        self._store.upsert_bet_state(bet_id, state or 0, profit_cents)
                        continue

                    bookmaker_name = self._writer.get_bookmaker_name(bookmaker_id) or f"Casa {bookmaker_id}"

                    self._store.record_green_bet(
                        bet_id=bet_id,
                        bookmaker_id=bookmaker_id,
                        bookmaker_name=bookmaker_name,
                        stake_cents=int((stake * Decimal("100")).to_integral_value()),
                        profit_cents=int((profit * Decimal("100")).to_integral_value()),
                        return_cents=int((return_amount * Decimal("100")).to_integral_value()),
                    )
                    green_total += return_amount
                    green_count += 1
                    logger.info(
                        "Green contabilizado: bet_id=%s casa=%s retorno=%s",
                        bet_id,
                        bookmaker_name,
                        return_amount,
                    )

                elif is_red and was_pending and not self._store.is_red_bet_recorded(bet_id):
                    stake = self._to_decimal(bet.get("stake"))
                    if stake is None or stake <= 0:
                        self._store.upsert_bet_state(bet_id, state or 0, profit_cents)
                        continue

                    bookmaker_id = self._to_int(bet.get("bookmaker"))
                    if bookmaker_id is None:
                        self._store.upsert_bet_state(bet_id, state or 0, profit_cents)
                        continue

                    bookmaker_name = self._writer.get_bookmaker_name(bookmaker_id) or f"Casa {bookmaker_id}"

                    self._store.record_red_bet(
                        bet_id=bet_id,
                        bookmaker_id=bookmaker_id,
                        bookmaker_name=bookmaker_name,
                        stake_cents=int((stake * Decimal("100")).to_integral_value()),
                    )
                    red_total += stake
                    red_count += 1
                    logger.info(
                        "Red contabilizado: bet_id=%s casa=%s stake=%s",
                        bet_id,
                        bookmaker_name,
                        stake,
                    )

                # Atualiza o estado conhecido da aposta.
                self._store.upsert_bet_state(bet_id, state or 0, profit_cents)

            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
            total_pages = self._to_int(pagination.get("totalPages")) or page
            has_next_page = bool(pagination.get("hasNextPage"))
            if page >= total_pages or not has_next_page:
                break
            page += 1

        self._store.set_green_sync_state("last_seen_bet_id", str(max_seen_id))
        if is_first_sync:
            logger.info("Primeira sincronizacao: baseline e estados registrados. Nenhum resultado antigo contabilizado.")
        else:
            logger.info(
                "Sincronizacao: %s green(s) (R$ %s), %s red(s) (R$ %s)",
                green_count,
                green_total,
                red_count,
                red_total,
            )
        return green_count, green_total, red_count, red_total

    def reset_green_baseline(self) -> int:
        """Redefine o baseline de greens para o maior ID atual da API.

        Use quando quiser que o bot comece a contar apenas greens marcados
        a partir deste momento, ignorando apostas antigas.
        """

        max_id = 0
        page = 1
        while page <= self._settings.pending_bets_max_pages:
            payload = self._writer.fetch_bankroll_bets_page(page=page)
            if not isinstance(payload, dict):
                break

            bets = payload.get("bets")
            if not isinstance(bets, list):
                break

            for bet in bets:
                if not isinstance(bet, dict):
                    continue
                bet_id = self._to_int(bet.get("id"))
                if bet_id is not None and bet_id > max_id:
                    max_id = bet_id

            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
            total_pages = self._to_int(pagination.get("totalPages")) or page
            has_next_page = bool(pagination.get("hasNextPage"))
            if page >= total_pages or not has_next_page:
                break
            page += 1

        self._store.set_green_sync_state("last_seen_bet_id", str(max_id))
        self._store.clear_accounted_bets()
        logger.info("Baseline de apostas resetado para %s e historico de greens/reds limpo.", max_id)
        return max_id

    def get_withdrawal_balances(self) -> list[BookmakerWithdrawalBalance]:
        """Retorna o saldo disponivel para saque por casa."""

        rows = self._store.get_bookmaker_withdrawal_balances()
        return [
            BookmakerWithdrawalBalance(
                bookmaker_id=int(row["bookmaker_id"]),
                bookmaker_name=str(row["bookmaker_name"]),
                available=Decimal(int(row["available_cents"])) / Decimal("100"),
            )
            for row in rows
        ]

    def resolve_bookmaker_id(self, name_or_id: str | int) -> int | None:
        """Resolve um nome ou id de casa para o ID numerico."""

        bookmaker_id = self._to_int(name_or_id)
        if bookmaker_id is not None:
            return bookmaker_id

        self._writer._load_bookmakers()
        if self._writer._bookmakers_by_name is None:
            return None
        return self._writer._bookmakers_by_name.get(_normalize_text(str(name_or_id)))

    def get_bets_by_bookmaker(self, name_or_id: str | int) -> list[BookmakerBet]:
        """Retorna todas as apostas de uma casa de aposta especifica."""

        bookmaker_id = self.resolve_bookmaker_id(name_or_id)
        if bookmaker_id is None:
            raise ValueError(f"Casa nao encontrada: {name_or_id}")

        all_bets = self.get_bets_by_bookmaker_all()
        bets = [bet for bet in all_bets if bet.bookmaker_id == bookmaker_id]
        return sorted(bets, key=lambda b: b.event_datetime, reverse=True)

    def get_bookmaker_balances(self) -> list[BookmakerBalance]:
        """Calcula o saldo real por casa de aposta.

        O saldo reflete apenas o dinheiro depositado pelo usuario e apostas
        que o bot acompanhou desde pendente ate a resolucao. Apostas antigas
        ja resolvidas no Bet-Analytix antes do inicio do rastreamento sao
        ignoradas.

        Formula por casa:
        disponivel = depositos - saques - reds_contabilizados - pendentes_atuais + lucro_greens_contabilizados
        """

        # IDs de apostas ja contabilizadas (green ou red). Apostas pendentes
        # que ja mudaram de estado nao devem aparecer novamente como pendentes.
        accounted_ids = self._store.get_accounted_bet_ids()

        # Timestamp do primeiro deposito por casa. Apostas pendentes so reduzem
        # o saldo se o bot as conheceu DEPOIS do primeiro deposito. Isso evita
        # reduzir o saldo por apostas que ja existiam antes do usuario depositar.
        first_deposit_ts_by_bookmaker = self._store.get_first_deposit_ts_by_bookmaker()

        # Stake das apostas atualmente pendentes, agrupadas por casa.
        # Exclui apostas ja contabilizadas e apostas anteriores ao deposito.
        pending_by_bookmaker: dict[int, dict[str, Any]] = {}
        pending_bet_ids: list[int] = []
        pending_bets: list[tuple[int, int, Decimal]] = []  # bookmaker_id, bet_id, stake
        page = 1
        while page <= self._settings.pending_bets_max_pages:
            payload = self._writer.fetch_bankroll_bets_page(page=page)
            if not isinstance(payload, dict):
                break

            raw_bets = payload.get("bets")
            if not isinstance(raw_bets, list):
                break

            for bet in raw_bets:
                if not isinstance(bet, dict):
                    continue

                bet_id = self._to_int(bet.get("id"))
                bookmaker_id = self._to_int(bet.get("bookmaker"))
                if bet_id is None or bookmaker_id is None:
                    continue
                if bet_id in accounted_ids:
                    continue

                state = self._to_int(bet.get("state")) or 0
                if state != self._PENDING_STATE:
                    continue

                stake = self._to_decimal(bet.get("stake")) or Decimal("0")
                if stake <= 0:
                    continue

                pending_bet_ids.append(bet_id)
                pending_bets.append((bookmaker_id, bet_id, stake))

                if bookmaker_id not in pending_by_bookmaker:
                    name = self._writer.get_bookmaker_name(bookmaker_id) or f"Casa {bookmaker_id}"
                    pending_by_bookmaker[bookmaker_id] = {"name": name, "in_play": Decimal("0")}

            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
            total_pages = self._to_int(pagination.get("totalPages")) or page
            has_next_page = bool(pagination.get("hasNextPage"))
            if page >= total_pages or not has_next_page:
                break
            page += 1

        # Filtra pendentes que foram vistos pelo bot apos o primeiro deposito.
        bet_states = self._store.get_bet_states(pending_bet_ids)
        for bookmaker_id, bet_id, stake in pending_bets:
            first_deposit_ts = first_deposit_ts_by_bookmaker.get(bookmaker_id)
            if first_deposit_ts is None:
                continue
            state_row = bet_states.get(bet_id)
            if state_row is None:
                continue
            # first_seen_at_ts pode ser NULL em registros antigos; nesse caso,
            # fallback para updated_at_ts para nao quebrar a logica.
            first_seen_raw = state_row["first_seen_at_ts"]
            bet_seen_ts = int(first_seen_raw) if first_seen_raw is not None else int(state_row["updated_at_ts"])
            if bet_seen_ts <= first_deposit_ts:
                continue
            pending_by_bookmaker[bookmaker_id]["in_play"] += stake

        # Estrutura final por casa.
        balances_data: dict[int, dict[str, Any]] = {}

        def _ensure_bookmaker(bookmaker_id: int, name: str | None = None) -> dict[str, Any]:
            if bookmaker_id not in balances_data:
                resolved_name = name or self._writer.get_bookmaker_name(bookmaker_id) or f"Casa {bookmaker_id}"
                balances_data[bookmaker_id] = {
                    "name": resolved_name,
                    "deposits": Decimal("0"),
                    "withdrawals": Decimal("0"),
                    "in_play": Decimal("0"),
                    "lost": Decimal("0"),
                    "won": Decimal("0"),
                }
            return balances_data[bookmaker_id]

        # Greens contabilizados (apenas lucro liquido).
        for row in self._store.get_green_bets_accounted_by_bookmaker():
            bookmaker_id = int(row["bookmaker_id"])
            data = _ensure_bookmaker(bookmaker_id, str(row["bookmaker_name"]))
            data["won"] += Decimal(int(row["profit_cents"])) / Decimal("100")

        # Reds contabilizados (stake perdida).
        for row in self._store.get_red_bets_accounted_by_bookmaker():
            bookmaker_id = int(row["bookmaker_id"])
            data = _ensure_bookmaker(bookmaker_id, str(row["bookmaker_name"]))
            data["lost"] += Decimal(int(row["stake_cents"])) / Decimal("100")

        # Apostas pendentes atuais.
        for bookmaker_id, data in pending_by_bookmaker.items():
            bm = _ensure_bookmaker(bookmaker_id, data["name"])
            bm["in_play"] += data["in_play"]

        # Depositos e saques manuais.
        transactions = self._store.list_transactions(types=["deposit", "withdrawal"], limit=10000)
        for row in transactions:
            transaction = self._row_to_transaction(row)
            if transaction.bookmaker_id is None:
                continue
            bm = _ensure_bookmaker(transaction.bookmaker_id, transaction.bookmaker_name)
            if transaction.type == "deposit":
                bm["deposits"] += transaction.amount
            elif transaction.type == "withdrawal":
                bm["withdrawals"] += transaction.amount

        # Monta resultado
        balances: list[BookmakerBalance] = []
        for bookmaker_id, data in sorted(balances_data.items(), key=lambda item: item[1]["deposits"] + item[1]["won"], reverse=True):
            available = data["deposits"] - data["withdrawals"] - data["lost"] - data["in_play"] + data["won"]
            if available <= 0 and data["deposits"] == 0 and data["won"] == 0:
                continue
            balances.append(
                BookmakerBalance(
                    bookmaker_id=bookmaker_id,
                    bookmaker_name=data["name"],
                    deposits=data["deposits"],
                    withdrawals=data["withdrawals"],
                    in_play=data["in_play"],
                    lost=data["lost"],
                    won=data["won"],
                    available=available,
                )
            )

        return balances

    def get_bets_by_bookmaker_all(self) -> list[BookmakerBet]:
        """Retorna todas as apostas de todas as casas da bankroll."""

        bets: list[BookmakerBet] = []
        page = 1

        while page <= self._settings.pending_bets_max_pages:
            payload = self._writer.fetch_bankroll_bets_page(page=page)
            if not isinstance(payload, dict):
                break

            raw_bets = payload.get("bets")
            if not isinstance(raw_bets, list):
                break

            for bet in raw_bets:
                if not isinstance(bet, dict):
                    continue

                bet_id = self._to_int(bet.get("id"))
                bookmaker_id = self._to_int(bet.get("bookmaker"))
                if bet_id is None or bookmaker_id is None:
                    continue

                state = self._to_int(bet.get("state")) or 0
                profit = self._to_decimal(bet.get("profit")) or Decimal("0")
                gain = self._to_decimal(bet.get("gain")) or Decimal("0")
                stake = self._to_decimal(bet.get("stake")) or Decimal("0")
                odds = self._to_decimal(bet.get("odds")) or Decimal("0")
                label = str(bet.get("label") or "").strip() or f"Aposta #{bet_id}"

                event_ts = self._to_int(bet.get("date")) or 0
                event_datetime = datetime.fromtimestamp(event_ts, tz=timezone.utc)

                bets.append(
                    BookmakerBet(
                        bet_id=bet_id,
                        bookmaker_id=bookmaker_id,
                        bookmaker_name=self._writer.get_bookmaker_name(bookmaker_id) or f"Casa {bookmaker_id}",
                        label=label,
                        stake=stake,
                        odds=odds,
                        state=state,
                        profit=profit,
                        gain=gain,
                        event_datetime=event_datetime,
                        is_green=state == 1 and profit > 0,
                        is_red=state == 2 or profit < 0,
                        is_pending=state == 0,
                    )
                )

            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
            total_pages = self._to_int(pagination.get("totalPages")) or page
            has_next_page = bool(pagination.get("hasNextPage"))
            if page >= total_pages or not has_next_page:
                break
            page += 1

        return bets

    def allocate_withdrawal(
        self,
        amount: Decimal,
        bookmaker_name_or_id: str | int | None = None,
    ) -> list[BookmakerWithdrawalBalance]:
        """Registra um saque e abate o saldo disponivel das casas.

        Se nenhuma casa for informada, abate das casas com maior saldo primeiro.
        Retorna as casas e valores abatidos.
        """

        amount_cents = int((amount * Decimal("100")).to_integral_value())
        bookmaker_id: int | None = None

        if bookmaker_name_or_id is not None:
            bookmaker_id = self._to_int(bookmaker_name_or_id)
            if bookmaker_id is None:
                # Tenta resolver pelo nome
                self._writer._load_bookmakers()
                bookmaker_id = self._writer._bookmakers_by_name.get(_normalize_text(str(bookmaker_name_or_id)))
                if bookmaker_id is None:
                    raise ValueError(f"Casa nao encontrada: {bookmaker_name_or_id}")

        allocations = self._store.allocate_withdrawal(amount_cents=amount_cents, bookmaker_id=bookmaker_id)
        return [
            BookmakerWithdrawalBalance(
                bookmaker_id=allocation["bookmaker_id"],
                bookmaker_name=allocation["bookmaker_name"],
                available=Decimal(int(allocation["amount_cents"])) / Decimal("100"),
            )
            for allocation in allocations
        ]

    def build_report(self) -> BankrollReport:
        """Monta o relatorio consolidado diario."""

        summary = self.fetch_summary()
        exposures = self.fetch_pending_by_bookmaker()
        total_pending = sum((exposure.pending_stake for exposure in exposures), Decimal("0"))
        transactions = self.list_today_transactions()

        today_deposits = Decimal("0")
        today_withdrawals = Decimal("0")
        for transaction in transactions:
            if transaction.type == "deposit":
                today_deposits += transaction.amount
            elif transaction.type == "withdrawal":
                today_withdrawals += transaction.amount

        withdrawal_balances = self.get_withdrawal_balances()
        total_available_to_withdraw = sum((balance.available for balance in withdrawal_balances), Decimal("0"))

        bookmaker_balances = self.get_bookmaker_balances()
        total_bookmaker_balance = sum((balance.available for balance in bookmaker_balances), Decimal("0"))

        return BankrollReport(
            summary=summary,
            exposures=exposures,
            today_transactions=transactions,
            total_pending=total_pending,
            today_deposits=today_deposits,
            today_withdrawals=today_withdrawals,
            withdrawal_balances=withdrawal_balances,
            total_available_to_withdraw=total_available_to_withdraw,
            bookmaker_balances=bookmaker_balances,
            total_bookmaker_balance=total_bookmaker_balance,
            currency_symbol=self._settings.currency_symbol,
            generated_at_ts=int(time.time()),
        )

    def format_money(self, value: Decimal) -> str:
        """Formata um valor para exibicao com o simbolo da moeda."""

        return f"{self._settings.currency_symbol} {value:,.2f}"

    @staticmethod
    def _to_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            if isinstance(value, int):
                return value
            return int(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_decimal(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    @staticmethod
    def _row_to_transaction(row: Any) -> Transaction:
        bookmaker_id_raw = row["bookmaker_id"]
        bookmaker_id: int | None = None
        if bookmaker_id_raw is not None:
            try:
                bookmaker_id = int(bookmaker_id_raw)
            except (TypeError, ValueError):
                bookmaker_id = None

        return Transaction(
            id=int(row["id"]),
            type=str(row["type"]),
            amount=Decimal(int(row["amount_cents"])) / Decimal("100"),
            currency=str(row["currency"]),
            bookmaker_id=bookmaker_id,
            bookmaker_name=str(row["bookmaker_name"]) if row["bookmaker_name"] is not None else None,
            description=str(row["description"]) if row["description"] is not None else None,
            created_at_ts=int(row["created_at_ts"]),
            created_by_user_id=int(row["created_by_user_id"]),
        )


def _normalize_text(value: str) -> str:
    """Normaliza texto para busca de nomes de casas de aposta."""

    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return "".join(char for char in without_marks if char.isalnum())
