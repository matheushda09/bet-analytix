"""Persistencia do mapeamento Telegram <-> Discord para replies e threads."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class TelegramForwarderStore:
    """SQLite simples para mapear mensagens Telegram -> Discord e threads."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._ensure_directory()

    def _ensure_directory(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        """Cria as tabelas necessarias."""

        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS telegram_discord_message_map (
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_message_id INTEGER NOT NULL,
                    discord_message_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (telegram_chat_id, telegram_message_id)
                );

                CREATE TABLE IF NOT EXISTS telegram_discord_thread_map (
                    telegram_channel_message_id INTEGER NOT NULL PRIMARY KEY,
                    discord_thread_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_message_map_chat
                    ON telegram_discord_message_map(telegram_chat_id, telegram_message_id);
                """
            )
            conn.commit()
        logger.info("Banco de mapeamento inicializado: %s", self._db_path)

    def save_message_mapping(
        self,
        telegram_chat_id: int,
        telegram_message_id: int,
        discord_message_id: int,
    ) -> None:
        """Persiste o mapeamento de uma mensagem Telegram para Discord."""

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_discord_message_map
                    (telegram_chat_id, telegram_message_id, discord_message_id)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_chat_id, telegram_message_id)
                DO UPDATE SET discord_message_id=excluded.discord_message_id
                """,
                (telegram_chat_id, telegram_message_id, discord_message_id),
            )
            conn.commit()

    def get_discord_message_id(
        self,
        telegram_chat_id: int,
        telegram_message_id: int,
    ) -> int | None:
        """Recupera o discord_message_id de uma mensagem Telegram."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT discord_message_id
                FROM telegram_discord_message_map
                WHERE telegram_chat_id = ? AND telegram_message_id = ?
                """,
                (telegram_chat_id, telegram_message_id),
            ).fetchone()
            return int(row["discord_message_id"]) if row else None

    def save_thread_mapping(
        self,
        telegram_channel_message_id: int,
        discord_thread_id: int,
    ) -> None:
        """Persiste o mapeamento de uma mensagem de canal para thread do Discord."""

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_discord_thread_map
                    (telegram_channel_message_id, discord_thread_id)
                VALUES (?, ?)
                ON CONFLICT(telegram_channel_message_id)
                DO UPDATE SET discord_thread_id=excluded.discord_thread_id
                """,
                (telegram_channel_message_id, discord_thread_id),
            )
            conn.commit()

    def get_discord_thread_id(
        self,
        telegram_channel_message_id: int,
    ) -> int | None:
        """Recupera o discord_thread_id de uma mensagem de canal."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT discord_thread_id
                FROM telegram_discord_thread_map
                WHERE telegram_channel_message_id = ?
                """,
                (telegram_channel_message_id,),
            ).fetchone()
            return int(row["discord_thread_id"]) if row else None
