"""Configuracao do redirecionador Telegram -> Discord."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True)
class TelegramForwarderSettings:
    """Configuracoes imutaveis do redirecionador."""

    enabled: bool
    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str
    telegram_session_string: str | None
    telegram_session_path: Path
    source_chat_id: int
    source_comments_chat_id: int | None
    sqlite_path: Path
    destiny_discord_token: str
    destiny_discord_channel_id: int
    destiny_discord_guild_id: int | None
    log_level: str
    queue_max_size: int
    media_download_dir: Path
    album_debounce_seconds: float
    rate_limit_messages_per_second: float
    healthcheck_interval_seconds: int
    retry_max_attempts: int
    retry_base_delay_seconds: float
    media_keep_after_send: bool


def load_telegram_forwarder_settings(env_path: str | Path = ".env") -> TelegramForwarderSettings:
    """Carrega variaveis `TF_*` do `.env`, com fallback para variaveis legadas."""

    load_dotenv(env_path)

    api_id = _get_int("TF_TELEGRAM_API_ID", _get_int("TELEGRAM_API_ID", 0))
    api_hash = _get_str("TF_TELEGRAM_API_HASH", _get_str("TELEGRAM_API_HASH", ""))
    phone = _get_str("TF_TELEGRAM_PHONE", _get_str("TELEGRAM_PHONE", ""))
    source_chat_id = _get_int("TF_SOURCE_CHAT_ID", 0)
    discord_token = _get_str("TF_DESTINY_DISCORD_BOT_TOKEN", _get_str("TF_DESTINY_DISCORD_USER_TOKEN", ""))
    discord_channel_id = _get_int("TF_DESTINY_DISCORD_CHANNEL_ID", 0)

    settings = TelegramForwarderSettings(
        enabled=_get_bool("TELEGRAM_FORWARDER_ENABLED", False),
        telegram_api_id=api_id,
        telegram_api_hash=api_hash,
        telegram_phone=phone,
        telegram_session_string=_get_optional_str("TF_TELEGRAM_SESSION_STRING"),
        telegram_session_path=Path(_get_str("TF_TELEGRAM_SESSION_PATH", "data/telegram_forwarder.session")),
        source_chat_id=source_chat_id,
        source_comments_chat_id=_get_optional_int("TF_SOURCE_COMMENTS_CHAT_ID"),
        sqlite_path=Path(_get_str("TF_SQLITE_PATH", "data/telegram_forwarder.sqlite3")),
        destiny_discord_token=discord_token,
        destiny_discord_channel_id=discord_channel_id,
        destiny_discord_guild_id=_get_optional_int("TF_DESTINY_DISCORD_GUILD_ID"),
        log_level=_get_str("TF_LOG_LEVEL", _get_str("LOG_LEVEL", "INFO")).upper(),
        queue_max_size=_get_int("TF_QUEUE_MAX_SIZE", 1000),
        media_download_dir=Path(_get_str("TF_MEDIA_DOWNLOAD_DIR", "data/telegram_forwarder_media")),
        album_debounce_seconds=_get_float("TF_ALBUM_DEBOUNCE_SECONDS", 1.5),
        rate_limit_messages_per_second=_get_float("TF_RATE_LIMIT_MESSAGES_PER_SECOND", 1.0),
        healthcheck_interval_seconds=_get_int("TF_HEALTHCHECK_INTERVAL_SECONDS", 60),
        retry_max_attempts=_get_int("TF_RETRY_MAX_ATTEMPTS", 10),
        retry_base_delay_seconds=_get_float("TF_RETRY_BASE_DELAY_SECONDS", 2.0),
        media_keep_after_send=_get_bool("TF_MEDIA_KEEP_AFTER_SEND", False),
    )

    if settings.enabled:
        validate_telegram_forwarder_settings(settings)

    return settings


def validate_telegram_forwarder_settings(settings: TelegramForwarderSettings) -> None:
    """Valida configuracoes obrigatorias quando o redirecionador esta ativo."""

    missing: list[str] = []
    if not settings.telegram_api_id:
        missing.append("TF_TELEGRAM_API_ID (ou TELEGRAM_API_ID)")
    if not settings.telegram_api_hash:
        missing.append("TF_TELEGRAM_API_HASH (ou TELEGRAM_API_HASH)")
    if not settings.telegram_phone:
        missing.append("TF_TELEGRAM_PHONE (ou TELEGRAM_PHONE)")
    if not settings.source_chat_id:
        missing.append("TF_SOURCE_CHAT_ID")
    if not settings.destiny_discord_token:
        missing.append("TF_DESTINY_DISCORD_BOT_TOKEN (ou TF_DESTINY_DISCORD_USER_TOKEN legado)")
    if not settings.destiny_discord_channel_id:
        missing.append("TF_DESTINY_DISCORD_CHANNEL_ID")

    if missing:
        raise RuntimeError(
            "Variaveis obrigatorias ausentes para TELEGRAM_FORWARDER_ENABLED=true: "
            + ", ".join(missing)
        )


def _get_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip()


def _get_optional_str(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _get_int(name: str, default: int) -> int:
    value = _get_optional_str(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Variavel {name} precisa ser um inteiro. Valor recebido: {value}") from exc


def _get_optional_int(name: str) -> int | None:
    value = _get_optional_str(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Variavel {name} precisa ser um inteiro. Valor recebido: {value}") from exc


def _get_float(name: str, default: float) -> float:
    value = _get_optional_str(name)
    if value is None:
        return default
    try:
        return float(value.replace(",", "."))
    except ValueError as exc:
        raise RuntimeError(f"Variavel {name} precisa ser numerica. Valor recebido: {value}") from exc


def _get_bool(name: str, default: bool) -> bool:
    value = _get_optional_str(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "t", "yes", "y", "sim", "s"}
