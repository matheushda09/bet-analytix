"""Cache SQLite compartilhado, locks e controle de consumo dos providers."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sports_event_models import ExternalSportsEvent


@dataclass(frozen=True)
class ProviderCallReservation:
    call_id: int
    provider: str


class SportsScheduleStore:
    """Agenda persistente compartilhável por todos os subprocessos."""

    def __init__(self, sqlite_path: Path) -> None:
        self._sqlite_path = sqlite_path

    def initialize(self) -> None:
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sports_events (
                    provider TEXT NOT NULL,
                    external_event_id TEXT NOT NULL,
                    sport TEXT NOT NULL,
                    participant_home TEXT NOT NULL,
                    participant_away TEXT NOT NULL,
                    starts_at_ts INTEGER NOT NULL,
                    competition TEXT,
                    country TEXT,
                    status TEXT,
                    raw_payload_json TEXT,
                    fetched_at_ts INTEGER NOT NULL,
                    expires_at_ts INTEGER NOT NULL,
                    PRIMARY KEY(provider, external_event_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sports_events_window
                ON sports_events (sport, starts_at_ts, expires_at_ts)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sports_query_cache (
                    provider TEXT NOT NULL,
                    query_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    fetched_at_ts INTEGER NOT NULL,
                    expires_at_ts INTEGER NOT NULL,
                    error TEXT,
                    PRIMARY KEY(provider, query_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sports_fetch_locks (
                    lock_key TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at_ts REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sports_provider_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    called_at_ts REAL NOT NULL,
                    completed_at_ts REAL,
                    success INTEGER,
                    status_code INTEGER,
                    duration_ms INTEGER,
                    error TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sports_provider_calls_usage
                ON sports_provider_calls (provider, called_at_ts)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sports_provider_state (
                    provider TEXT PRIMARY KEY,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    blocked_until_ts REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at_ts REAL NOT NULL
                )
                """
            )
            # A limpeza só pode ocorrer depois que todas as tabelas existirem.
            # Isso mantém a inicialização compatível com um banco totalmente novo.
            self._prune_locked(connection)

    def list_events(
        self,
        *,
        sport: str,
        start_at_utc: datetime,
        end_at_utc: datetime,
        providers: tuple[str, ...] | None = None,
    ) -> list[ExternalSportsEvent]:
        now = time.time()
        conditions = [
            "sport = ?",
            "starts_at_ts BETWEEN ? AND ?",
            "expires_at_ts > ?",
        ]
        params: list[Any] = [
            sport,
            int(_utc(start_at_utc).timestamp()),
            int(_utc(end_at_utc).timestamp()),
            now,
        ]
        if providers:
            placeholders = ",".join("?" for _ in providers)
            conditions.append(f"provider IN ({placeholders})")
            params.extend(providers)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM sports_events
                WHERE {' AND '.join(conditions)}
                ORDER BY starts_at_ts ASC
                """,
                params,
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def get_fresh_event(
        self,
        provider: str,
        external_event_id: str,
    ) -> ExternalSportsEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM sports_events
                WHERE provider = ?
                  AND external_event_id = ?
                  AND expires_at_ts > ?
                """,
                (provider, external_event_id, time.time()),
            ).fetchone()
        return self._row_to_event(row) if row is not None else None

    def upsert_events(
        self,
        events: list[ExternalSportsEvent],
        *,
        ttl_seconds: int,
        include_raw_payload: bool,
    ) -> None:
        if not events:
            return
        now = int(time.time())
        expires_at = now + max(1, ttl_seconds)
        with self._connect() as connection:
            for event in events:
                starts_at = _utc(event.starts_at_utc)
                raw_payload = event.raw_payload if include_raw_payload else {}
                connection.execute(
                    """
                    INSERT INTO sports_events (
                        provider, external_event_id, sport,
                        participant_home, participant_away, starts_at_ts,
                        competition, country, status, raw_payload_json,
                        fetched_at_ts, expires_at_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, external_event_id) DO UPDATE SET
                        sport = excluded.sport,
                        participant_home = excluded.participant_home,
                        participant_away = excluded.participant_away,
                        starts_at_ts = excluded.starts_at_ts,
                        competition = excluded.competition,
                        country = excluded.country,
                        status = excluded.status,
                        raw_payload_json = excluded.raw_payload_json,
                        fetched_at_ts = excluded.fetched_at_ts,
                        expires_at_ts = excluded.expires_at_ts
                    """,
                    (
                        event.provider,
                        event.external_event_id,
                        event.sport,
                        event.participant_home,
                        event.participant_away,
                        int(starts_at.timestamp()),
                        event.competition,
                        event.country,
                        event.status,
                        json.dumps(raw_payload, ensure_ascii=False, sort_keys=True),
                        now,
                        expires_at,
                    ),
                )

    def get_fresh_query(
        self,
        provider: str,
        query_key: str,
    ) -> sqlite3.Row | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM sports_query_cache
                WHERE provider = ? AND query_key = ? AND expires_at_ts > ?
                """,
                (provider, query_key, time.time()),
            ).fetchone()
        return row

    def record_query(
        self,
        *,
        provider: str,
        query_key: str,
        status: str,
        event_count: int,
        ttl_seconds: int,
        error: str | None = None,
    ) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sports_query_cache (
                    provider, query_key, status, event_count,
                    fetched_at_ts, expires_at_ts, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, query_key) DO UPDATE SET
                    status = excluded.status,
                    event_count = excluded.event_count,
                    fetched_at_ts = excluded.fetched_at_ts,
                    expires_at_ts = excluded.expires_at_ts,
                    error = excluded.error
                """,
                (
                    provider,
                    query_key,
                    status,
                    event_count,
                    now,
                    now + max(1, ttl_seconds),
                    error[:1000] if error else None,
                ),
            )

    def acquire_fetch_lock(
        self,
        lock_key: str,
        owner: str,
        ttl_seconds: int,
    ) -> bool:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM sports_fetch_locks WHERE expires_at_ts <= ?",
                (now,),
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO sports_fetch_locks(lock_key, owner, expires_at_ts)
                VALUES (?, ?, ?)
                """,
                (lock_key, owner, now + max(1, ttl_seconds)),
            )
        return cursor.rowcount > 0

    def release_fetch_lock(self, lock_key: str, owner: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sports_fetch_locks WHERE lock_key = ? AND owner = ?",
                (lock_key, owner),
            )

    def reserve_provider_call(
        self,
        provider: str,
        *,
        minute_limit: int,
        daily_limit: int,
    ) -> ProviderCallReservation | None:
        now = time.time()
        minute_start = now - 60
        day_start = now - 86400
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT blocked_until_ts FROM sports_provider_state WHERE provider = ?",
                (provider,),
            ).fetchone()
            if state is not None and float(state["blocked_until_ts"]) > now:
                return None

            minute_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM sports_provider_calls
                    WHERE provider = ? AND called_at_ts >= ?
                    """,
                    (provider, minute_start),
                ).fetchone()["count"]
            )
            daily_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM sports_provider_calls
                    WHERE provider = ? AND called_at_ts >= ?
                    """,
                    (provider, day_start),
                ).fetchone()["count"]
            )
            if minute_limit > 0 and minute_count >= minute_limit:
                return None
            if daily_limit > 0 and daily_count >= daily_limit:
                return None
            cursor = connection.execute(
                """
                INSERT INTO sports_provider_calls(provider, called_at_ts)
                VALUES (?, ?)
                """,
                (provider, now),
            )
        return ProviderCallReservation(int(cursor.lastrowid), provider)

    def complete_provider_call(
        self,
        reservation: ProviderCallReservation,
        *,
        success: bool,
        status_code: int | None,
        duration_ms: int,
        error: str | None = None,
        block_seconds: float = 0,
    ) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE sports_provider_calls
                SET completed_at_ts = ?, success = ?, status_code = ?,
                    duration_ms = ?, error = ?
                WHERE id = ?
                """,
                (
                    now,
                    1 if success else 0,
                    status_code,
                    duration_ms,
                    error[:1000] if error else None,
                    reservation.call_id,
                ),
            )
            current = connection.execute(
                """
                SELECT consecutive_failures FROM sports_provider_state
                WHERE provider = ?
                """,
                (reservation.provider,),
            ).fetchone()
            failures = 0 if success else (int(current["consecutive_failures"]) if current else 0) + 1
            connection.execute(
                """
                INSERT INTO sports_provider_state (
                    provider, consecutive_failures, blocked_until_ts,
                    last_error, updated_at_ts
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    consecutive_failures = excluded.consecutive_failures,
                    blocked_until_ts = excluded.blocked_until_ts,
                    last_error = excluded.last_error,
                    updated_at_ts = excluded.updated_at_ts
                """,
                (
                    reservation.provider,
                    failures,
                    now + max(0, block_seconds) if not success else 0,
                    error[:1000] if error else None,
                    now,
                ),
            )

    def provider_metrics(self) -> list[dict[str, Any]]:
        day_start = time.time() - 86400
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT provider,
                       COUNT(*) AS calls,
                       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successes,
                       SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS errors,
                       AVG(duration_ms) AS avg_duration_ms
                FROM sports_provider_calls
                WHERE called_at_ts >= ?
                GROUP BY provider
                ORDER BY provider
                """,
                (day_start,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _prune_locked(connection: sqlite3.Connection) -> None:
        now = time.time()
        connection.execute(
            "DELETE FROM sports_fetch_locks WHERE expires_at_ts <= ?",
            (now,),
        )
        connection.execute(
            "DELETE FROM sports_query_cache WHERE expires_at_ts <= ?",
            (now - 86400,),
        )
        connection.execute(
            "DELETE FROM sports_events WHERE expires_at_ts <= ?",
            (now - 7 * 86400,),
        )
        connection.execute(
            "DELETE FROM sports_provider_calls WHERE called_at_ts <= ?",
            (now - 7 * 86400,),
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> ExternalSportsEvent:
        raw_payload: dict[str, Any] = {}
        if row["raw_payload_json"]:
            try:
                parsed = json.loads(str(row["raw_payload_json"]))
                if isinstance(parsed, dict):
                    raw_payload = parsed
            except json.JSONDecodeError:
                pass
        return ExternalSportsEvent(
            provider=str(row["provider"]),
            external_event_id=str(row["external_event_id"]),
            sport=str(row["sport"]),
            participant_home=str(row["participant_home"]),
            participant_away=str(row["participant_away"]),
            starts_at_utc=datetime.fromtimestamp(
                int(row["starts_at_ts"]),
                tz=timezone.utc,
            ),
            competition=str(row["competition"]) if row["competition"] else None,
            country=str(row["country"]) if row["country"] else None,
            status=str(row["status"]) if row["status"] else None,
            raw_payload=raw_payload,
            fetched_at_utc=datetime.fromtimestamp(
                int(row["fetched_at_ts"]),
                tz=timezone.utc,
            ),
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


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Datetime do cache esportivo precisa possuir timezone.")
    return value.astimezone(timezone.utc)
