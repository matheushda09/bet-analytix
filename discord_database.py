"""Persistencia SQLite do listener Discord."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from message_parser import ParsedTelegramTip


class DiscordSignalStore:
    """Fila local idempotente para sinais capturados via reacao no Discord."""

    def __init__(self, sqlite_path: Path) -> None:
        self._sqlite_path = sqlite_path

    def initialize(self) -> None:
        """Cria tabelas e indices necessarios para operacao continua."""

        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_signal_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    signal_sender_id INTEGER NOT NULL,
                    reacting_user_id INTEGER NOT NULL,
                    source_bookmaker_name TEXT,
                    source_tipster_name TEXT,
                    source_bet_id INTEGER NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    raw_message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_ts INTEGER NOT NULL,
                    last_error TEXT,
                    bet_analytix_bet_id INTEGER,
                    response_json TEXT,
                    created_at_ts INTEGER NOT NULL,
                    updated_at_ts INTEGER NOT NULL,
                    UNIQUE(guild_id, channel_id, message_id)
                )
                """
            )
            try:
                connection.execute("ALTER TABLE discord_signal_jobs ADD COLUMN source_bookmaker_name TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                connection.execute("ALTER TABLE discord_signal_jobs ADD COLUMN source_tipster_name TEXT")
            except sqlite3.OperationalError:
                pass
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_discord_signal_jobs_due
                ON discord_signal_jobs (status, next_attempt_ts)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bankroll_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL CHECK(type IN ('deposit','withdrawal','adjustment')),
                    amount_cents INTEGER NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'BRL',
                    bookmaker_id INTEGER,
                    bookmaker_name TEXT,
                    description TEXT,
                    created_at_ts INTEGER NOT NULL,
                    created_by_user_id INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bankroll_transactions_created
                ON bankroll_transactions (created_at_ts)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bankroll_transactions_bookmaker
                ON bankroll_transactions (bookmaker_id)
                """
            )
            # Migracao: adiciona colunas se a tabela ja existir sem elas
            try:
                connection.execute("ALTER TABLE bankroll_transactions ADD COLUMN bookmaker_id INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                connection.execute("ALTER TABLE bankroll_transactions ADD COLUMN bookmaker_name TEXT")
            except sqlite3.OperationalError:
                pass
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bankroll_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_date TEXT NOT NULL UNIQUE,
                    report_json TEXT NOT NULL,
                    sent_at_ts INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS green_bets_accounted (
                    bet_id INTEGER PRIMARY KEY,
                    bookmaker_id INTEGER NOT NULL,
                    bookmaker_name TEXT NOT NULL,
                    stake_cents INTEGER NOT NULL,
                    profit_cents INTEGER NOT NULL,
                    return_cents INTEGER NOT NULL,
                    accounted_at_ts INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_green_bets_accounted_bookmaker
                ON green_bets_accounted (bookmaker_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS red_bets_accounted (
                    bet_id INTEGER PRIMARY KEY,
                    bookmaker_id INTEGER NOT NULL,
                    bookmaker_name TEXT NOT NULL,
                    stake_cents INTEGER NOT NULL,
                    accounted_at_ts INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_red_bets_accounted_bookmaker
                ON red_bets_accounted (bookmaker_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bookmaker_withdrawal_balance (
                    bookmaker_id INTEGER PRIMARY KEY,
                    bookmaker_name TEXT NOT NULL,
                    available_cents INTEGER NOT NULL DEFAULT 0,
                    updated_at_ts INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS withdrawal_allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id INTEGER NOT NULL,
                    bookmaker_id INTEGER NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    allocated_at_ts INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS green_sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at_ts INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bet_states (
                    bet_id INTEGER PRIMARY KEY,
                    state INTEGER NOT NULL,
                    profit_cents INTEGER NOT NULL DEFAULT 0,
                    first_seen_at_ts INTEGER NOT NULL,
                    updated_at_ts INTEGER NOT NULL
                )
                """
            )
            # Migracao: adiciona coluna first_seen_at_ts se a tabela ja existir sem ela
            try:
                connection.execute("ALTER TABLE bet_states ADD COLUMN first_seen_at_ts INTEGER")
            except sqlite3.OperationalError:
                pass

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS peixeesperto_sync_state (
                    message_id INTEGER PRIMARY KEY,
                    estado TEXT NOT NULL,
                    bet_analytix_bet_id INTEGER,
                    matched_source_bet_id INTEGER,
                    synced_at_ts INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sports_event_matches (
                    source_bet_id INTEGER PRIMARY KEY,
                    mode TEXT NOT NULL,
                    match_status TEXT NOT NULL,
                    match_reason TEXT NOT NULL,
                    sport TEXT,
                    signal_participants_json TEXT,
                    normalized_signal_participants_json TEXT,
                    provider TEXT,
                    external_event_id TEXT,
                    participant_home TEXT,
                    participant_away TEXT,
                    normalized_event_participants_json TEXT,
                    competition TEXT,
                    country TEXT,
                    starts_at_utc TEXT,
                    event_status TEXT,
                    confidence REAL,
                    participant_1_score REAL,
                    participant_2_score REAL,
                    second_best_confidence REAL,
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    providers_consulted_json TEXT,
                    reasons_json TEXT,
                    from_cache INTEGER NOT NULL DEFAULT 0,
                    fallback_datetime_utc TEXT NOT NULL,
                    raw_payload_json TEXT,
                    matched_at_ts INTEGER NOT NULL,
                    last_checked_at_ts INTEGER,
                    next_check_at_ts INTEGER,
                    applied_to_bet INTEGER NOT NULL DEFAULT 0,
                    applied_starts_at_utc TEXT,
                    bet_analytix_bet_id INTEGER,
                    last_refresh_error TEXT
                )
                """
            )
            try:
                connection.execute(
                    "ALTER TABLE sports_event_matches ADD COLUMN applied_starts_at_utc TEXT"
                )
            except sqlite3.OperationalError:
                pass
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sports_event_matches_recheck
                ON sports_event_matches (
                    mode, match_status, applied_to_bet, next_check_at_ts
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sports_event_match_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_bet_id INTEGER NOT NULL,
                    provider TEXT,
                    external_event_id TEXT,
                    previous_starts_at_utc TEXT,
                    new_starts_at_utc TEXT,
                    previous_status TEXT,
                    new_status TEXT,
                    action TEXT NOT NULL,
                    details_json TEXT,
                    created_at_ts INTEGER NOT NULL
                )
                """
            )

    def enqueue_signal(
        self,
        guild_id: str | int,
        channel_id: str | int,
        message_id: int,
        signal_sender_id: int,
        reacting_user_id: int,
        tip: ParsedTelegramTip,
        raw_message: str,
        source_bookmaker_name: str | None = None,
        source_tipster_name: str | None = None,
    ) -> bool:
        """Insere um sinal na fila; retorna `False` quando for duplicado."""

        now = int(time.time())
        payload_json = json.dumps(tip_to_payload(tip), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO discord_signal_jobs (
                    guild_id,
                    channel_id,
                    message_id,
                    signal_sender_id,
                    reacting_user_id,
                    source_bookmaker_name,
                    source_tipster_name,
                    source_bet_id,
                    payload_json,
                    raw_message,
                    status,
                    attempts,
                    next_attempt_ts,
                    created_at_ts,
                    updated_at_ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    str(guild_id),
                    str(channel_id),
                    message_id,
                    signal_sender_id,
                    reacting_user_id,
                    source_bookmaker_name,
                    source_tipster_name,
                    tip.source_bet_id,
                    payload_json,
                    raw_message,
                    now,
                    now,
                    now,
                ),
            )
        return cursor.rowcount > 0

    def get_job_by_message(self, guild_id: str | int, channel_id: str | int, message_id: int) -> sqlite3.Row | None:
        """Busca um job pela origem Discord."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM discord_signal_jobs
                WHERE guild_id = ? AND channel_id = ? AND message_id = ?
                """,
                (str(guild_id), str(channel_id), message_id),
            ).fetchone()
        return row

    def claim_due_jobs(self, limit: int, stale_processing_seconds: int = 900) -> list[sqlite3.Row]:
        """Reserva jobs vencidos para envio, recuperando jobs travados apos queda."""

        now = int(time.time())
        stale_before = now - stale_processing_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT *
                FROM discord_signal_jobs
                WHERE (
                    status IN ('pending', 'retry') AND next_attempt_ts <= ?
                ) OR (
                    status = 'processing' AND updated_at_ts <= ?
                )
                ORDER BY created_at_ts ASC
                LIMIT ?
                """,
                (now, stale_before, limit),
            ).fetchall()
            job_ids = [int(row["id"]) for row in rows]
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                connection.execute(
                    f"""
                    UPDATE discord_signal_jobs
                    SET status = 'processing',
                        updated_at_ts = ?
                    WHERE id IN ({placeholders})
                    """,
                    (now, *job_ids),
                )
        return list(rows)

    def mark_job_success(self, job_id: int, bet_analytix_bet_id: int | None, response: Any) -> None:
        """Marca um job como concluido depois de resposta positiva."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE discord_signal_jobs
                SET status = 'done',
                    bet_analytix_bet_id = ?,
                    response_json = ?,
                    last_error = NULL,
                    updated_at_ts = ?
                WHERE id = ?
                """,
                (bet_analytix_bet_id, json.dumps(response, ensure_ascii=False, sort_keys=True), int(time.time()), job_id),
            )

    def schedule_job_retry(self, job_id: int, error: str, delay_seconds: float) -> None:
        """Reagenda um job com backoff sem descartar o sinal original."""

        now = int(time.time())
        next_attempt = now + max(1, int(delay_seconds))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE discord_signal_jobs
                SET status = 'retry',
                    attempts = attempts + 1,
                    next_attempt_ts = ?,
                    last_error = ?,
                    updated_at_ts = ?
                WHERE id = ?
                """,
                (next_attempt, error[:1000], now, job_id),
            )

    def mark_job_failed(self, job_id: int, error: str) -> None:
        """Marca um job como falha permanente, sem novas tentativas."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE discord_signal_jobs
                SET status = 'failed',
                    last_error = ?,
                    updated_at_ts = ?
                WHERE id = ?
                """,
                (error[:1000], int(time.time()), job_id),
            )

    def mark_job_awaiting_bookmaker(self, job_id: int, question: str) -> None:
        """Pausa o job e aguarda o admin informar o nome correto da casa."""

        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE discord_signal_jobs
                SET status = 'awaiting_bookmaker',
                    last_error = ?,
                    next_attempt_ts = ?,
                    updated_at_ts = ?
                WHERE id = ?
                """,
                (question[:1000], now + 7 * 86400, now, job_id),
            )

    def get_jobs_awaiting_bookmaker(self, limit: int = 10) -> list[sqlite3.Row]:
        """Retorna jobs que estao aguardando correcao do nome da bookmaker."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM discord_signal_jobs
                WHERE status = 'awaiting_bookmaker'
                ORDER BY created_at_ts ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return list(rows)

    def get_pending_bets_for_result_sync(
        self,
        max_age_hours: int = 72,
        limit: int = 1000,
    ) -> list[sqlite3.Row]:
        """Retorna apostas ja enviadas ao Bet-Analytix que ainda nao foram sincronizadas com o PeixeEsperto."""

        since_ts = int(time.time()) - max_age_hours * 3600
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT j.*
                FROM discord_signal_jobs j
                LEFT JOIN peixeesperto_sync_state s ON s.matched_source_bet_id = j.source_bet_id
                WHERE j.status = 'done'
                  AND j.bet_analytix_bet_id IS NOT NULL
                  AND j.created_at_ts >= ?
                  AND s.message_id IS NULL
                ORDER BY j.created_at_ts DESC
                LIMIT ?
                """,
                (since_ts, limit),
            ).fetchall()
        return list(rows)

    def get_synced_message_ids(self) -> set[int]:
        """Retorna o conjunto de message_ids do PeixeEsperto ja sincronizados."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT message_id FROM peixeesperto_sync_state"
            ).fetchall()
        return {int(row["message_id"]) for row in rows}

    def record_peixeesperto_sync(
        self,
        message_id: int,
        estado: str,
        bet_analytix_bet_id: int,
        matched_source_bet_id: int,
    ) -> None:
        """Marca um resultado do PeixeEsperto como ja sincronizado."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO peixeesperto_sync_state
                (message_id, estado, bet_analytix_bet_id, matched_source_bet_id, synced_at_ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (message_id, estado, bet_analytix_bet_id, matched_source_bet_id, int(time.time())),
            )

    def record_sports_event_match(self, audit: dict[str, Any]) -> None:
        """Persiste o resultado aceito, shadow ou fallback de forma auditável."""

        now = int(time.time())
        starts_at = audit.get("starts_at_utc")
        next_check = now + 1800 if audit.get("match_status") == "accepted" else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sports_event_matches (
                    source_bet_id, mode, match_status, match_reason, sport,
                    signal_participants_json, normalized_signal_participants_json,
                    provider, external_event_id, participant_home, participant_away,
                    normalized_event_participants_json, competition, country,
                    starts_at_utc, event_status, confidence,
                    participant_1_score, participant_2_score,
                    second_best_confidence, candidate_count,
                    providers_consulted_json, reasons_json, from_cache,
                    fallback_datetime_utc, raw_payload_json,
                    matched_at_ts, last_checked_at_ts, next_check_at_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_bet_id) DO UPDATE SET
                    mode = excluded.mode,
                    match_status = excluded.match_status,
                    match_reason = excluded.match_reason,
                    sport = excluded.sport,
                    signal_participants_json = excluded.signal_participants_json,
                    normalized_signal_participants_json = excluded.normalized_signal_participants_json,
                    provider = excluded.provider,
                    external_event_id = excluded.external_event_id,
                    participant_home = excluded.participant_home,
                    participant_away = excluded.participant_away,
                    normalized_event_participants_json = excluded.normalized_event_participants_json,
                    competition = excluded.competition,
                    country = excluded.country,
                    starts_at_utc = excluded.starts_at_utc,
                    event_status = excluded.event_status,
                    confidence = excluded.confidence,
                    participant_1_score = excluded.participant_1_score,
                    participant_2_score = excluded.participant_2_score,
                    second_best_confidence = excluded.second_best_confidence,
                    candidate_count = excluded.candidate_count,
                    providers_consulted_json = excluded.providers_consulted_json,
                    reasons_json = excluded.reasons_json,
                    from_cache = excluded.from_cache,
                    fallback_datetime_utc = excluded.fallback_datetime_utc,
                    raw_payload_json = excluded.raw_payload_json,
                    matched_at_ts = excluded.matched_at_ts,
                    last_checked_at_ts = excluded.last_checked_at_ts,
                    next_check_at_ts = excluded.next_check_at_ts,
                    last_refresh_error = NULL
                """,
                (
                    int(audit["source_bet_id"]),
                    str(audit["mode"]),
                    str(audit["match_status"]),
                    str(audit["match_reason"]),
                    audit.get("sport"),
                    _json_or_none(audit.get("signal_participants")),
                    _json_or_none(audit.get("normalized_signal_participants")),
                    audit.get("provider"),
                    audit.get("external_event_id"),
                    audit.get("participant_home"),
                    audit.get("participant_away"),
                    _json_or_none(audit.get("normalized_event_participants")),
                    audit.get("competition"),
                    audit.get("country"),
                    starts_at,
                    audit.get("event_status"),
                    audit.get("confidence"),
                    audit.get("participant_1_score"),
                    audit.get("participant_2_score"),
                    audit.get("second_best_confidence"),
                    int(audit.get("candidate_count") or 0),
                    _json_or_none(audit.get("providers_consulted")),
                    _json_or_none(audit.get("reasons")),
                    1 if audit.get("from_cache") else 0,
                    str(audit["fallback_datetime_utc"]),
                    _json_or_none(audit.get("raw_payload")),
                    now,
                    now if starts_at else None,
                    next_check,
                ),
            )

    def mark_sports_event_applied(
        self,
        source_bet_id: int,
        bet_analytix_bet_id: int | None,
        next_check_at_ts: int | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sports_event_matches
                SET applied_to_bet = 1,
                    applied_starts_at_utc = starts_at_utc,
                    bet_analytix_bet_id = ?,
                    next_check_at_ts = ?,
                    last_refresh_error = NULL
                WHERE source_bet_id = ?
                  AND mode = 'enabled'
                  AND match_status = 'accepted'
                """,
                (bet_analytix_bet_id, next_check_at_ts, source_bet_id),
            )

    def get_due_sports_event_matches(self, limit: int = 50) -> list[sqlite3.Row]:
        now = int(time.time())
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, j.payload_json
                FROM sports_event_matches m
                JOIN discord_signal_jobs j ON j.source_bet_id = m.source_bet_id
                WHERE m.mode = 'enabled'
                  AND m.match_status = 'accepted'
                  AND m.applied_to_bet = 1
                  AND m.bet_analytix_bet_id IS NOT NULL
                  AND m.provider IS NOT NULL
                  AND m.external_event_id IS NOT NULL
                  AND m.next_check_at_ts IS NOT NULL
                  AND m.next_check_at_ts <= ?
                ORDER BY m.next_check_at_ts ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return list(rows)

    def record_sports_event_refresh(
        self,
        *,
        source_bet_id: int,
        starts_at_utc: str,
        event_status: str | None,
        next_check_at_ts: int | None,
        action: str,
        applied_datetime_updated: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """
                SELECT provider, external_event_id, starts_at_utc, event_status
                FROM sports_event_matches WHERE source_bet_id = ?
                """,
                (source_bet_id,),
            ).fetchone()
            if previous is None:
                return
            if (
                previous["starts_at_utc"] != starts_at_utc
                or previous["event_status"] != event_status
            ):
                connection.execute(
                    """
                    INSERT INTO sports_event_match_history (
                        source_bet_id, provider, external_event_id,
                        previous_starts_at_utc, new_starts_at_utc,
                        previous_status, new_status, action, details_json, created_at_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_bet_id,
                        previous["provider"],
                        previous["external_event_id"],
                        previous["starts_at_utc"],
                        starts_at_utc,
                        previous["event_status"],
                        event_status,
                        action,
                        _json_or_none(details),
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE sports_event_matches
                SET starts_at_utc = ?,
                    applied_starts_at_utc = CASE
                        WHEN ? = 1 THEN ?
                        ELSE applied_starts_at_utc
                    END,
                    event_status = ?,
                    last_checked_at_ts = ?,
                    next_check_at_ts = ?,
                    last_refresh_error = NULL
                WHERE source_bet_id = ?
                """,
                (
                    starts_at_utc,
                    1 if applied_datetime_updated else 0,
                    starts_at_utc,
                    event_status,
                    now,
                    next_check_at_ts,
                    source_bet_id,
                ),
            )

    def record_sports_event_refresh_error(
        self,
        source_bet_id: int,
        error: str,
        next_check_at_ts: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sports_event_matches
                SET last_checked_at_ts = ?,
                    next_check_at_ts = ?,
                    last_refresh_error = ?
                WHERE source_bet_id = ?
                """,
                (int(time.time()), next_check_at_ts, error[:1000], source_bet_id),
            )

    def update_job_bookmaker(self, job_id: int, new_bookmaker: str) -> bool:
        """Atualiza o nome da casa no payload e reativa o job para processamento."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM discord_signal_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return False
            payload = json.loads(str(row["payload_json"]))
            payload["bookmaker"] = new_bookmaker
            now = int(time.time())
            connection.execute(
                """
                UPDATE discord_signal_jobs
                SET payload_json = ?,
                    status = 'pending',
                    attempts = 0,
                    next_attempt_ts = ?,
                    last_error = NULL,
                    updated_at_ts = ?
                WHERE id = ?
                """,
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), now, now, job_id),
            )
        return True

    def count_jobs(self, statuses: Sequence[str] | None = None) -> int:
        """Conta jobs da fila, opcionalmente filtrando por status."""

        with self._connect() as connection:
            if not statuses:
                row = connection.execute("SELECT COUNT(*) AS count FROM discord_signal_jobs").fetchone()
                return int(row["count"])

            placeholders = ",".join("?" for _ in statuses)
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM discord_signal_jobs WHERE status IN ({placeholders})",
                tuple(statuses),
            ).fetchone()
            return int(row["count"])

    def record_transaction(
        self,
        transaction_type: str,
        amount_cents: int,
        created_by_user_id: int,
        description: str | None = None,
        currency: str = "BRL",
        bookmaker_id: int | None = None,
        bookmaker_name: str | None = None,
        created_at_ts: int | None = None,
    ) -> int:
        """Registra um deposito, saque ou ajuste manual no controle de bankroll."""

        now = created_at_ts if created_at_ts is not None else int(time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO bankroll_transactions (
                    type, amount_cents, currency, bookmaker_id, bookmaker_name, description, created_at_ts, created_by_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (transaction_type, amount_cents, currency, bookmaker_id, bookmaker_name, description, now, created_by_user_id),
            )
        return int(cursor.lastrowid)

    def list_transactions(
        self,
        since_ts: int | None = None,
        until_ts: int | None = None,
        types: Sequence[str] | None = None,
        limit: int = 1000,
    ) -> list[sqlite3.Row]:
        """Lista transacoes financeiras registradas localmente."""

        conditions: list[str] = []
        params: list[Any] = []
        if since_ts is not None:
            conditions.append("created_at_ts >= ?")
            params.append(since_ts)
        if until_ts is not None:
            conditions.append("created_at_ts <= ?")
            params.append(until_ts)
        if types:
            placeholders = ",".join("?" for _ in types)
            conditions.append(f"type IN ({placeholders})")
            params.extend(types)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM bankroll_transactions
                {where}
                ORDER BY created_at_ts DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return list(rows)

    def mark_report_sent(self, report_date: str, report_json: str) -> None:
        """Evita envio duplicado do relatorio diario."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bankroll_reports (report_date, report_json, sent_at_ts)
                VALUES (?, ?, ?)
                ON CONFLICT(report_date) DO UPDATE SET
                    report_json = excluded.report_json,
                    sent_at_ts = excluded.sent_at_ts
                """,
                (report_date, report_json, int(time.time())),
            )

    def get_report_for_date(self, report_date: str) -> sqlite3.Row | None:
        """Verifica se o relatorio de um dia ja foi enviado."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bankroll_reports WHERE report_date = ?",
                (report_date,),
            ).fetchone()
        return row

    def is_green_bet_recorded(self, bet_id: int) -> bool:
        """Verifica se um green ja foi contabilizado no saldo a sacar."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM green_bets_accounted WHERE bet_id = ?",
                (bet_id,),
            ).fetchone()
        return row is not None

    def record_green_bet(
        self,
        bet_id: int,
        bookmaker_id: int,
        bookmaker_name: str,
        stake_cents: int,
        profit_cents: int,
        return_cents: int,
        accounted_at_ts: int | None = None,
    ) -> None:
        """Marca um green como contabilizado e credita o saldo da casa."""

        now = accounted_at_ts if accounted_at_ts is not None else int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO green_bets_accounted (
                    bet_id, bookmaker_id, bookmaker_name, stake_cents, profit_cents, return_cents, accounted_at_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (bet_id, bookmaker_id, bookmaker_name, stake_cents, profit_cents, return_cents, now),
            )
            connection.execute(
                """
                INSERT INTO bookmaker_withdrawal_balance (bookmaker_id, bookmaker_name, available_cents, updated_at_ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(bookmaker_id) DO UPDATE SET
                    bookmaker_name = excluded.bookmaker_name,
                    available_cents = bookmaker_withdrawal_balance.available_cents + excluded.available_cents,
                    updated_at_ts = excluded.updated_at_ts
                """,
                (bookmaker_id, bookmaker_name, return_cents, now),
            )

    def is_red_bet_recorded(self, bet_id: int) -> bool:
        """Verifica se um red ja foi contabilizado no saldo real."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM red_bets_accounted WHERE bet_id = ?",
                (bet_id,),
            ).fetchone()
        return row is not None

    def record_red_bet(
        self,
        bet_id: int,
        bookmaker_id: int,
        bookmaker_name: str,
        stake_cents: int,
        accounted_at_ts: int | None = None,
    ) -> None:
        """Marca um red como contabilizado e debita o saldo da casa."""

        now = accounted_at_ts if accounted_at_ts is not None else int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO red_bets_accounted (
                    bet_id, bookmaker_id, bookmaker_name, stake_cents, accounted_at_ts
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (bet_id, bookmaker_id, bookmaker_name, stake_cents, now),
            )

    def get_green_bets_accounted_by_bookmaker(self) -> list[sqlite3.Row]:
        """Retorna o total de lucro de greens contabilizados por casa."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT bookmaker_id, bookmaker_name, COALESCE(SUM(profit_cents), 0) AS profit_cents
                FROM green_bets_accounted
                GROUP BY bookmaker_id, bookmaker_name
                ORDER BY profit_cents DESC
                """
            ).fetchall()
        return list(rows)

    def get_red_bets_accounted_by_bookmaker(self) -> list[sqlite3.Row]:
        """Retorna o total de stake perdido em reds contabilizados por casa."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT bookmaker_id, bookmaker_name, COALESCE(SUM(stake_cents), 0) AS stake_cents
                FROM red_bets_accounted
                GROUP BY bookmaker_id, bookmaker_name
                ORDER BY stake_cents DESC
                """
            ).fetchall()
        return list(rows)

    def get_accounted_bet_ids(self) -> set[int]:
        """Retorna o conjunto de IDs de apostas ja contabilizadas (green ou red)."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT bet_id FROM green_bets_accounted
                UNION
                SELECT bet_id FROM red_bets_accounted
                """
            ).fetchall()
        return {int(row["bet_id"]) for row in rows}

    def get_bookmaker_withdrawal_balances(self) -> list[sqlite3.Row]:
        """Retorna o saldo disponivel para saque por casa."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bookmaker_withdrawal_balance
                WHERE available_cents > 0
                ORDER BY available_cents DESC
                """
            ).fetchall()
        return list(rows)

    def get_total_withdrawal_balance(self) -> int:
        """Retorna o total disponivel para saque em todas as casas."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(available_cents), 0) AS total FROM bookmaker_withdrawal_balance"
            ).fetchone()
        return int(row["total"])

    def allocate_withdrawal(
        self,
        amount_cents: int,
        bookmaker_id: int | None = None,
        transaction_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Abate um saque do saldo disponivel das casas.

        Se bookmaker_id for informado, abate apenas daquela casa.
        Caso contrario, abate das casas com maior saldo primeiro.
        Retorna as alocacoes feitas.
        """

        now = int(time.time())
        allocations: list[dict[str, Any]] = []
        with self._connect() as connection:
            if bookmaker_id is not None:
                rows = connection.execute(
                    "SELECT * FROM bookmaker_withdrawal_balance WHERE bookmaker_id = ? AND available_cents > 0",
                    (bookmaker_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM bookmaker_withdrawal_balance WHERE available_cents > 0 ORDER BY available_cents DESC"
                ).fetchall()

            remaining = amount_cents
            for row in rows:
                if remaining <= 0:
                    break
                available = int(row["available_cents"])
                take = min(available, remaining)
                new_available = available - take
                connection.execute(
                    """
                    UPDATE bookmaker_withdrawal_balance
                    SET available_cents = ?, updated_at_ts = ?
                    WHERE bookmaker_id = ?
                    """,
                    (new_available, now, int(row["bookmaker_id"])),
                )
                if transaction_id is not None:
                    connection.execute(
                        """
                        INSERT INTO withdrawal_allocations (transaction_id, bookmaker_id, amount_cents, allocated_at_ts)
                        VALUES (?, ?, ?, ?)
                        """,
                        (transaction_id, int(row["bookmaker_id"]), take, now),
                    )
                allocations.append({
                    "bookmaker_id": int(row["bookmaker_id"]),
                    "bookmaker_name": str(row["bookmaker_name"]),
                    "amount_cents": take,
                })
                remaining -= take

        return allocations

    def get_green_sync_state(self, key: str) -> str | None:
        """Le o estado de sincronizacao de greens."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM green_sync_state WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def set_green_sync_state(self, key: str, value: str) -> None:
        """Grava o estado de sincronizacao de greens."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO green_sync_state (key, value, updated_at_ts)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at_ts = excluded.updated_at_ts
                """,
                (key, value, int(time.time())),
            )

    def clear_accounted_bets(self) -> None:
        """Limpa o historico de apostas contabilizadas (green/red)."""

        with self._connect() as connection:
            connection.execute("DELETE FROM green_bets_accounted")
            connection.execute("DELETE FROM red_bets_accounted")
            connection.execute("DELETE FROM bookmaker_withdrawal_balance")

    def update_transaction_bookmaker(
        self,
        transaction_id: int,
        bookmaker_id: int,
        bookmaker_name: str,
    ) -> None:
        """Atualiza a casa de uma transacao ja existente."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE bankroll_transactions
                SET bookmaker_id = ?, bookmaker_name = ?
                WHERE id = ?
                """,
                (bookmaker_id, bookmaker_name, transaction_id),
            )

    def get_bet_state(self, bet_id: int) -> sqlite3.Row | None:
        """Retorna o estado conhecido anterior de uma aposta."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bet_states WHERE bet_id = ?",
                (bet_id,),
            ).fetchone()
        return row

    def get_bet_states(self, bet_ids: Sequence[int]) -> dict[int, sqlite3.Row]:
        """Retorna os estados conhecidos para um conjunto de apostas."""

        if not bet_ids:
            return {}

        placeholders = ",".join("?" for _ in bet_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM bet_states WHERE bet_id IN ({placeholders})",
                tuple(bet_ids),
            ).fetchall()
        return {int(row["bet_id"]): row for row in rows}

    def get_first_deposit_ts_by_bookmaker(self) -> dict[int, int]:
        """Retorna o timestamp do primeiro deposito por casa."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT bookmaker_id, MIN(created_at_ts) AS first_deposit_ts
                FROM bankroll_transactions
                WHERE type = 'deposit' AND bookmaker_id IS NOT NULL
                GROUP BY bookmaker_id
                """
            ).fetchall()
        return {int(row["bookmaker_id"]): int(row["first_deposit_ts"]) for row in rows}

    def upsert_bet_state(self, bet_id: int, state: int, profit_cents: int) -> None:
        """Atualiza ou insere o estado atual de uma aposta."""

        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bet_states (bet_id, state, profit_cents, first_seen_at_ts, updated_at_ts)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(bet_id) DO UPDATE SET
                    state = excluded.state,
                    profit_cents = excluded.profit_cents,
                    updated_at_ts = excluded.updated_at_ts,
                    first_seen_at_ts = COALESCE(bet_states.first_seen_at_ts, excluded.first_seen_at_ts)
                """,
                (bet_id, state, profit_cents, now, now),
            )

    def set_metadata(self, key: str, value: str) -> None:
        """Grava metadados operacionais, como heartbeat."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO metadata (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._sqlite_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def tip_to_payload(tip: ParsedTelegramTip) -> dict[str, Any]:
    """Serializa uma tip para persistencia auditavel."""

    return {
        "tipster": tip.tipster,
        "event_datetime": tip.event_datetime.isoformat(),
        "sport": tip.sport,
        "league": tip.league,
        "pick": tip.pick,
        "odd": tip.odd,
        "stake": tip.stake,
        "bookmaker": tip.bookmaker,
        "source_bet_id": tip.source_bet_id,
        "event": tip.event,
        "extra_note": tip.extra_note,
        "is_accumulator": tip.is_accumulator,
    }


def tip_from_payload(payload: dict[str, Any]) -> ParsedTelegramTip:
    """Reidrata uma tip persistida no SQLite."""

    return ParsedTelegramTip(
        tipster=str(payload["tipster"]),
        event_datetime=datetime.fromisoformat(str(payload["event_datetime"])),
        sport=str(payload["sport"]),
        league=str(payload["league"]) if payload.get("league") else None,
        pick=str(payload["pick"]),
        odd=float(payload["odd"]),
        stake=float(payload["stake"]),
        bookmaker=str(payload["bookmaker"]),
        source_bet_id=int(payload["source_bet_id"]),
        event=str(payload["event"]) if payload.get("event") else None,
        extra_note=str(payload["extra_note"]) if payload.get("extra_note") is not None else None,
        is_accumulator=bool(payload.get("is_accumulator", False)),
    )


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
