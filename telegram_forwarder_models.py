"""Modelos de dados do redirecionador Telegram -> Discord."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TelegramMedia:
    """Midia extraida de uma mensagem do Telegram."""

    file_path: Path
    mime_type: str | None
    file_name: str | None
    is_voice: bool
    is_video_note: bool
    is_sticker: bool
    width: int | None
    height: int | None
    duration_seconds: int | None


@dataclass(frozen=True)
class TelegramMessage:
    """Mensagem do Telegram normalizada para reenvio."""

    telegram_message_id: int
    telegram_chat_id: int
    grouped_id: int | None
    sender_id: int | None
    sender_name: str | None
    sent_at: datetime
    text: str
    is_reply: bool
    reply_to_message_id: int | None
    reply_to_text: str | None
    reply_to_sender_name: str | None
    media: list[TelegramMedia] = field(default_factory=list)
    raw_type_name: str = "message"


@dataclass(frozen=True)
class DiscordPayload:
    """Payload pronto para envio ao Discord."""

    telegram_message_id: int
    telegram_chat_id: int
    grouped_id: int | None
    text: str
    media_files: list[Path]
    reply_to_telegram_message_id: int | None
    reply_to_discord_message_id: int | None
    sent_at: datetime
    is_comment: bool = False
    telegram_channel_message_id: int | None = None


def build_discord_payload(
    message: TelegramMessage,
    media_files: list[Path],
    reply_to_discord_message_id: int | None = None,
) -> DiscordPayload:
    """Monta o payload para o Discord a partir de uma mensagem do Telegram."""

    return DiscordPayload(
        telegram_message_id=message.telegram_message_id,
        telegram_chat_id=message.telegram_chat_id,
        grouped_id=message.grouped_id,
        text=message.text,
        media_files=media_files,
        reply_to_telegram_message_id=message.reply_to_message_id,
        reply_to_discord_message_id=reply_to_discord_message_id,
        sent_at=message.sent_at,
    )


def utc_now() -> datetime:
    """Retorna datetime UTC aware."""

    return datetime.now(timezone.utc)
