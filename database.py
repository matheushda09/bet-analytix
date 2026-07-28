"""Persistência SQLite para evitar notificações duplicadas."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from message_parser import ParsedTelegramTip
from models import Bet


TELEGRAM_DELIVERY_MODE_KEY = "telegram_delivery_mode"
TELEGRAM_DELIVERY_CHANNEL = "channel"
TELEGRAM_DELIVERY_PRIVATE = "private"
TELEGRAM_DELIVERY_MODES = {TELEGRAM_DELIVERY_CHANNEL, TELEGRAM_DELIVERY_PRIVATE}


class BetStateStore:
    """Armazena apostas já notificadas e metadados de inicialização."""

    def __init__(self, sqlite_path: Path) -> None:
        self._sqlite_path = sqlite_path

    def initialize(self) -> None:
        """Cria as tabelas necessárias caso ainda não existam."""

        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS notified_bets (
                    bet_id INTEGER PRIMARY KEY,
                    tipster_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    notified_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
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
                CREATE TABLE IF NOT EXISTS telegram_messages (
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    sent_at_ts INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, message_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS copytrade_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_bet_id INTEGER NOT NULL UNIQUE,
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_ts INTEGER NOT NULL,
                    last_error TEXT,
                    bet_analytix_bet_id INTEGER,
                    response_json TEXT,
                    created_at_ts INTEGER NOT NULL,
                    updated_at_ts INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_copytrade_jobs_due
                ON copytrade_jobs (status, next_attempt_ts)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bet_datetime_adjustment_sessions (
                    admin_user_id INTEGER PRIMARY KEY,
                    bet_analytix_bet_id INTEGER NOT NULL,
                    created_at_ts INTEGER NOT NULL,
                    updated_at_ts INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bet_odd_stake_adjustment_sessions (
                    admin_user_id INTEGER PRIMARY KEY,
                    source_bet_id INTEGER NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    created_at_ts INTEGER NOT NULL,
                    updated_at_ts INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS betano_odd_monitors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    created_by_user_id INTEGER NOT NULL,
                    link TEXT NOT NULL,
                    booking_code TEXT NOT NULL,
                    target_odd REAL NOT NULL,
                    current_odd REAL,
                    bet_summary TEXT,
                    betslip_json TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    error_count INTEGER NOT NULL DEFAULT 0,
                    next_check_ts INTEGER NOT NULL,
                    last_error TEXT,
                    alert_sent_at_ts INTEGER,
                    created_at_ts INTEGER NOT NULL,
                    updated_at_ts INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_betano_odd_monitors_due
                ON betano_odd_monitors (status, next_check_ts)
                """
            )

    def is_initialized(self) -> bool:
        """Indica se o bot já concluiu o primeiro polling com sucesso."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                ("initialized",),
            ).fetchone()
        return bool(row and row["value"] == "1")

    def set_initialized(self) -> None:
        """Marca o estado como inicializado."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO metadata (key, value, updated_at)
                VALUES ('initialized', '1', datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """
            )

    def get_metadata(self, key: str) -> str | None:
        """Lê um valor simples da tabela de metadados."""

        with self._connect() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def get_metadata_int(self, key: str) -> int | None:
        """Lê um valor inteiro da tabela de metadados."""

        value = self.get_metadata(key)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def set_metadata(self, key: str, value: str) -> None:
        """Grava um valor simples na tabela de metadados."""

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

    def get_telegram_delivery_mode(self) -> str:
        """Retorna o destino atual das notificacoes do monitor."""

        mode = self.get_metadata(TELEGRAM_DELIVERY_MODE_KEY)
        if mode in TELEGRAM_DELIVERY_MODES:
            return mode
        return TELEGRAM_DELIVERY_CHANNEL

    def toggle_telegram_delivery_mode(self) -> str:
        """Alterna o destino das notificacoes de forma atomica."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (TELEGRAM_DELIVERY_MODE_KEY,),
            ).fetchone()
            current_mode = str(row["value"]) if row and row["value"] in TELEGRAM_DELIVERY_MODES else TELEGRAM_DELIVERY_CHANNEL
            new_mode = TELEGRAM_DELIVERY_PRIVATE if current_mode == TELEGRAM_DELIVERY_CHANNEL else TELEGRAM_DELIVERY_CHANNEL
            connection.execute(
                """
                INSERT INTO metadata (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (TELEGRAM_DELIVERY_MODE_KEY, new_mode),
            )
        return new_mode

    def has_notified(self, bet_id: int) -> bool:
        """Retorna `True` se a aposta já foi enviada ao Telegram."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM notified_bets WHERE bet_id = ?",
                (bet_id,),
            ).fetchone()
        return row is not None

    def mark_notified(self, bet: Bet) -> None:
        """Registra uma aposta como notificada."""

        payload_json = json.dumps(bet.raw, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO notified_bets (bet_id, tipster_name, payload_json)
                VALUES (?, ?, ?)
                """,
                (bet.id, bet.tipster_name, payload_json),
            )

    def mark_many_as_seen(self, bets: Sequence[Bet]) -> int:
        """Registra várias apostas como vistas sem enviar notificação."""

        inserted = 0
        with self._connect() as connection:
            for bet in bets:
                payload_json = json.dumps(bet.raw, ensure_ascii=False, sort_keys=True)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO notified_bets (bet_id, tipster_name, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (bet.id, bet.tipster_name, payload_json),
                )
                inserted += cursor.rowcount
        return inserted

    def save_telegram_message(self, chat_id: str | int, message_id: int, text: str) -> None:
        """Persiste o texto enviado para permitir parsing quando houver reação."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO telegram_messages (chat_id, message_id, text, sent_at_ts)
                VALUES (?, ?, ?, ?)
                """,
                (str(chat_id), message_id, text, int(time.time())),
            )

    def get_telegram_message(self, chat_id: str | int, message_id: int) -> str | None:
        """Busca o texto de uma mensagem enviada anteriormente pelo bot."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT text FROM telegram_messages
                WHERE chat_id = ? AND message_id = ?
                """,
                (str(chat_id), message_id),
            ).fetchone()
        return str(row["text"]) if row else None

    def update_telegram_message_text(self, chat_id: str | int, message_id: int, text: str) -> None:
        """Atualiza o texto salvo de uma mensagem enviada pelo bot."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE telegram_messages
                SET text = ?,
                    sent_at_ts = ?
                WHERE chat_id = ? AND message_id = ?
                """,
                (text, int(time.time()), str(chat_id), message_id),
            )

    def enqueue_copytrade_job(self, chat_id: str | int, message_id: int, tip: ParsedTelegramTip) -> bool:
        """Coloca uma aposta na fila persistente de CopyTrade."""

        now = int(time.time())
        payload_json = json.dumps(_tip_to_payload(tip), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO copytrade_jobs (
                    source_bet_id,
                    chat_id,
                    message_id,
                    payload_json,
                    status,
                    attempts,
                    next_attempt_ts,
                    created_at_ts,
                    updated_at_ts
                )
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (tip.source_bet_id, str(chat_id), message_id, payload_json, now, now, now),
            )
        return cursor.rowcount > 0

    def get_copytrade_job_by_source_bet_id(self, source_bet_id: int) -> sqlite3.Row | None:
        """Busca um job pelo ID da aposta original enviada no Telegram."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM copytrade_jobs
                WHERE source_bet_id = ?
                """,
                (source_bet_id,),
            ).fetchone()
        return row

    def get_due_copytrade_jobs(self, limit: int) -> list[sqlite3.Row]:
        """Retorna jobs pendentes ou falhos cujo retry já venceu."""

        now = int(time.time())
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM copytrade_jobs
                WHERE status IN ('pending', 'retry') AND next_attempt_ts <= ?
                ORDER BY created_at_ts ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return list(rows)

    def count_copytrade_jobs(self, statuses: Sequence[str] | None = None) -> int:
        """Conta jobs de CopyTrade, opcionalmente filtrando por status."""

        with self._connect() as connection:
            if not statuses:
                row = connection.execute("SELECT COUNT(*) AS count FROM copytrade_jobs").fetchone()
                return int(row["count"])

            placeholders = ",".join("?" for _ in statuses)
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM copytrade_jobs WHERE status IN ({placeholders})",
                tuple(statuses),
            ).fetchone()
            return int(row["count"])

    def claim_due_copytrade_jobs(self, limit: int, stale_processing_seconds: int = 900) -> list[sqlite3.Row]:
        """Reserva jobs vencidos para processamento.

        A reserva evita que dois processos tentem enviar o mesmo job ao mesmo
        tempo. Jobs que ficaram em `processing` após queda do processo voltam a
        ser elegíveis depois de `stale_processing_seconds`.
        """

        now = int(time.time())
        stale_before = now - stale_processing_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT *
                FROM copytrade_jobs
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
                    UPDATE copytrade_jobs
                    SET status = 'processing',
                        updated_at_ts = ?
                    WHERE id IN ({placeholders})
                    """,
                    (now, *job_ids),
                )
        return list(rows)

    def mark_copytrade_job_success(
        self,
        job_id: int,
        bet_analytix_bet_id: int | None,
        response: Any,
    ) -> None:
        """Marca um job como concluído após inserção no Bet-Analytix."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE copytrade_jobs
                SET status = 'done',
                    bet_analytix_bet_id = ?,
                    response_json = ?,
                    last_error = NULL,
                    updated_at_ts = ?
                WHERE id = ?
                """,
                (bet_analytix_bet_id, json.dumps(response, ensure_ascii=False, sort_keys=True), int(time.time()), job_id),
            )

    def schedule_copytrade_job_retry(self, job_id: int, error: str, delay_seconds: float) -> None:
        """Reagenda um job com backoff sem descartá-lo."""

        now = int(time.time())
        next_attempt = now + max(1, int(delay_seconds))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE copytrade_jobs
                SET status = 'retry',
                    attempts = attempts + 1,
                    next_attempt_ts = ?,
                    last_error = ?,
                    updated_at_ts = ?
                WHERE id = ?
                """,
                (next_attempt, error[:1000], now, job_id),
            )

    def update_copytrade_job_payload(self, source_bet_id: int, tip: ParsedTelegramTip) -> None:
        """Atualiza o payload persistido de um job de CopyTrade."""

        payload_json = json.dumps(_tip_to_payload(tip), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE copytrade_jobs
                SET payload_json = ?,
                    updated_at_ts = ?
                WHERE source_bet_id = ?
                """,
                (payload_json, int(time.time()), source_bet_id),
            )

    def update_copytrade_job_response(self, source_bet_id: int, response: Any) -> None:
        """Atualiza a resposta auditavel de um job ja concluido."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE copytrade_jobs
                SET response_json = ?,
                    last_error = NULL,
                    updated_at_ts = ?
                WHERE source_bet_id = ?
                """,
                (json.dumps(response, ensure_ascii=False, sort_keys=True), int(time.time()), source_bet_id),
            )

    def set_pending_bet_datetime_adjustment(self, admin_user_id: int, bet_analytix_bet_id: int) -> None:
        """Grava a aposta que aguarda uma nova data/hora enviada pelo admin."""

        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bet_datetime_adjustment_sessions (
                    admin_user_id,
                    bet_analytix_bet_id,
                    created_at_ts,
                    updated_at_ts
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(admin_user_id) DO UPDATE SET
                    bet_analytix_bet_id = excluded.bet_analytix_bet_id,
                    updated_at_ts = excluded.updated_at_ts
                """,
                (admin_user_id, bet_analytix_bet_id, now, now),
            )

    def get_pending_bet_datetime_adjustment(self, admin_user_id: int) -> sqlite3.Row | None:
        """Retorna a sessao pendente de ajuste de data/hora do admin."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM bet_datetime_adjustment_sessions
                WHERE admin_user_id = ?
                """,
                (admin_user_id,),
            ).fetchone()
        return row

    def clear_pending_bet_datetime_adjustment(self, admin_user_id: int) -> None:
        """Remove a sessao pendente de ajuste de data/hora do admin."""

        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM bet_datetime_adjustment_sessions
                WHERE admin_user_id = ?
                """,
                (admin_user_id,),
            )

    def set_pending_bet_odd_stake_adjustment(
        self,
        admin_user_id: int,
        source_bet_id: int,
        chat_id: str | int,
        message_id: int,
    ) -> None:
        """Grava a mensagem que aguarda novo par odd/stake do admin."""

        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bet_odd_stake_adjustment_sessions (
                    admin_user_id,
                    source_bet_id,
                    chat_id,
                    message_id,
                    created_at_ts,
                    updated_at_ts
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(admin_user_id) DO UPDATE SET
                    source_bet_id = excluded.source_bet_id,
                    chat_id = excluded.chat_id,
                    message_id = excluded.message_id,
                    updated_at_ts = excluded.updated_at_ts
                """,
                (admin_user_id, source_bet_id, str(chat_id), message_id, now, now),
            )

    def get_pending_bet_odd_stake_adjustment(self, admin_user_id: int) -> sqlite3.Row | None:
        """Retorna a sessao pendente de ajuste de odd/stake do admin."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM bet_odd_stake_adjustment_sessions
                WHERE admin_user_id = ?
                """,
                (admin_user_id,),
            ).fetchone()
        return row

    def clear_pending_bet_odd_stake_adjustment(self, admin_user_id: int) -> None:
        """Remove a sessao pendente de ajuste de odd/stake do admin."""

        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM bet_odd_stake_adjustment_sessions
                WHERE admin_user_id = ?
                """,
                (admin_user_id,),
            )

    def create_betano_odd_monitor(
        self,
        chat_id: str | int,
        created_by_user_id: int,
        link: str,
        booking_code: str,
        target_odd: float,
        current_odd: float,
        bet_summary: str,
        betslip: dict[str, Any],
    ) -> int:
        """Cria um monitoramento de odd da Betano."""

        now = int(time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO betano_odd_monitors (
                    chat_id,
                    created_by_user_id,
                    link,
                    booking_code,
                    target_odd,
                    current_odd,
                    bet_summary,
                    betslip_json,
                    status,
                    error_count,
                    next_check_ts,
                    created_at_ts,
                    updated_at_ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
                """,
                (
                    str(chat_id),
                    created_by_user_id,
                    link,
                    booking_code,
                    target_odd,
                    current_odd,
                    bet_summary,
                    json.dumps(betslip, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def claim_due_betano_odd_monitors(self, limit: int, stale_processing_seconds: int = 120) -> list[sqlite3.Row]:
        """Reserva monitoramentos de Betano vencidos para checagem."""

        now = int(time.time())
        stale_before = now - stale_processing_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT *
                FROM betano_odd_monitors
                WHERE (
                    status = 'active' AND next_check_ts <= ?
                ) OR (
                    status = 'processing' AND updated_at_ts <= ?
                )
                ORDER BY next_check_ts ASC
                LIMIT ?
                """,
                (now, stale_before, limit),
            ).fetchall()
            monitor_ids = [int(row["id"]) for row in rows]
            if monitor_ids:
                placeholders = ",".join("?" for _ in monitor_ids)
                connection.execute(
                    f"""
                    UPDATE betano_odd_monitors
                    SET status = 'processing',
                        updated_at_ts = ?
                    WHERE id IN ({placeholders})
                    """,
                    (now, *monitor_ids),
                )
        return list(rows)

    def mark_betano_monitor_checked(
        self,
        monitor_id: int,
        current_odd: float,
        bet_summary: str,
        betslip: dict[str, Any],
        next_check_ts: int,
    ) -> None:
        """Marca uma checagem de odd como concluida sem disparar alerta."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE betano_odd_monitors
                SET status = 'active',
                    current_odd = ?,
                    bet_summary = ?,
                    betslip_json = ?,
                    error_count = 0,
                    last_error = NULL,
                    next_check_ts = ?,
                    updated_at_ts = ?
                WHERE id = ?
                """,
                (
                    current_odd,
                    bet_summary,
                    json.dumps(betslip, ensure_ascii=False, sort_keys=True),
                    next_check_ts,
                    int(time.time()),
                    monitor_id,
                ),
            )

    def mark_betano_monitor_triggered(
        self,
        monitor_id: int,
        current_odd: float,
        bet_summary: str,
        betslip: dict[str, Any],
    ) -> None:
        """Encerra um monitoramento apos alerta enviado."""

        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE betano_odd_monitors
                SET status = 'triggered',
                    current_odd = ?,
                    bet_summary = ?,
                    betslip_json = ?,
                    error_count = 0,
                    last_error = NULL,
                    alert_sent_at_ts = ?,
                    updated_at_ts = ?
                WHERE id = ?
                """,
                (
                    current_odd,
                    bet_summary,
                    json.dumps(betslip, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                    monitor_id,
                ),
            )

    def schedule_betano_monitor_retry(
        self,
        monitor_id: int,
        error: str,
        delay_seconds: int,
        max_error_count: int,
    ) -> None:
        """Reagenda um monitoramento da Betano com backoff."""

        now = int(time.time())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT error_count FROM betano_odd_monitors WHERE id = ?",
                (monitor_id,),
            ).fetchone()
            error_count = int(row["error_count"]) + 1 if row else 1
            status = "error" if error_count >= max_error_count else "active"
            connection.execute(
                """
                UPDATE betano_odd_monitors
                SET status = ?,
                    error_count = ?,
                    last_error = ?,
                    next_check_ts = ?,
                    updated_at_ts = ?
                WHERE id = ?
                """,
                (status, error_count, error[:1000], now + max(1, delay_seconds), now, monitor_id),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._sqlite_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _tip_to_payload(tip: ParsedTelegramTip) -> dict[str, Any]:
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
    }
