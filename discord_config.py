"""Configuracao do listener Discord para sinais externos."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True)
class DiscordReactionSettings:
    """Configuracoes imutaveis do bot Discord."""

    enabled: bool
    bot_token: str | None
    user_token: str | None
    guild_id: int | None
    channel_id: int | None
    admin_user_id: int | None
    destination_tipster_name: str
    sqlite_path: Path
    log_level: str
    queue_batch_size: int
    retry_max_seconds: int
    poll_interval_seconds: int
    notify_on_success: bool
    notify_chat_id: str | None
    notify_dm_on_success: bool
    bookmaker_aliases: dict[str, str]


def load_discord_reaction_settings(env_path: str | Path = ".env") -> DiscordReactionSettings:
    """Carrega variaveis `DISCORD_*` do `.env`."""

    load_dotenv(env_path)
    return DiscordReactionSettings(
        enabled=_get_bool("DISCORD_REACTION_BOT_ENABLED", False),
        bot_token=_get_optional_str("DISCORD_BOT_TOKEN"),
        user_token=_get_optional_str("DISCORD_USER_TOKEN"),
        guild_id=_get_optional_int("DISCORD_GUILD_ID"),
        channel_id=_get_optional_int("DISCORD_CHANNEL_ID"),
        admin_user_id=_get_optional_int("DISCORD_ADMIN_USER_ID"),
        destination_tipster_name=_get_str("DISCORD_BET_ANALYTIX_TIPSTER_NAME", "PeixeEsperto"),
        sqlite_path=Path(_get_str("DISCORD_SQLITE_PATH", "data/discord_signals.sqlite3")),
        log_level=_get_str("DISCORD_LOG_LEVEL", _get_str("LOG_LEVEL", "INFO")).upper(),
        queue_batch_size=_get_int("DISCORD_QUEUE_BATCH_SIZE", 5),
        retry_max_seconds=_get_int("DISCORD_RETRY_MAX_SECONDS", 900),
        poll_interval_seconds=_get_int("DISCORD_POLL_INTERVAL_SECONDS", 5),
        notify_on_success=_get_bool("DISCORD_NOTIFY_ON_SUCCESS", True),
        notify_chat_id=_get_optional_str("DISCORD_NOTIFY_CHAT_ID") or _get_optional_str("TELEGRAM_ADMIN_USER_ID"),
        notify_dm_on_success=_get_bool("DISCORD_NOTIFY_DM_ON_SUCCESS", True),
        bookmaker_aliases=_load_string_map("DISCORD_BOOKMAKER_ALIASES_JSON"),
    )


def validate_discord_reaction_settings(settings: DiscordReactionSettings) -> None:
    """Valida configuracoes obrigatorias quando o bot Discord esta ativo."""

    if not settings.enabled:
        return

    missing: list[str] = []
    if not settings.bot_token and not settings.user_token:
        missing.append("DISCORD_BOT_TOKEN ou DISCORD_USER_TOKEN")
    if settings.guild_id is None:
        missing.append("DISCORD_GUILD_ID")
    if settings.channel_id is None:
        missing.append("DISCORD_CHANNEL_ID")
    if settings.admin_user_id is None:
        missing.append("DISCORD_ADMIN_USER_ID")
    if settings.notify_on_success and not settings.notify_chat_id:
        missing.append("DISCORD_NOTIFY_CHAT_ID ou TELEGRAM_ADMIN_USER_ID")
    if missing:
        raise RuntimeError("Variaveis obrigatorias ausentes para DISCORD_REACTION_BOT_ENABLED=true: " + ", ".join(missing))


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


def _get_bool(name: str, default: bool) -> bool:
    value = _get_optional_str(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "t", "yes", "y", "sim", "s"}


def _load_string_map(name: str) -> dict[str, str]:
    raw = _get_optional_str(name)
    if raw is None:
        return {}
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} precisa ser JSON valido. Erro: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{name} precisa ser um objeto JSON.")
    return {str(key): str(value) for key, value in data.items()}


@dataclass(frozen=True)
class BankrollSettings:
    """Configuracoes do modulo de controle de bankroll."""

    enabled: bool
    report_dm_on: bool
    report_time_utc: str
    command_prefix: str
    currency_symbol: str
    pending_bets_max_pages: int
    green_cutoff_utc: str | None


def load_bankroll_settings(env_path: str | Path = ".env") -> BankrollSettings:
    """Carrega variaveis `BANKROLL_*` do `.env`."""

    load_dotenv(env_path)
    return BankrollSettings(
        enabled=_get_bool("BANKROLL_MODULE_ENABLED", False),
        report_dm_on=_get_bool("BANKROLL_REPORT_DM_ON", True),
        report_time_utc=_get_str("BANKROLL_REPORT_TIME_UTC", "03:00"),
        command_prefix=_get_str("BANKROLL_COMMAND_PREFIX", "!b"),
        currency_symbol=_get_str("BANKROLL_CURRENCY_SYMBOL", "R$"),
        pending_bets_max_pages=_get_int("BANKROLL_PENDING_BETS_MAX_PAGES", 10),
        green_cutoff_utc=_get_optional_str("BANKROLL_GREEN_CUTOFF_UTC"),
    )


@dataclass(frozen=True)
class PeixeEspertoSettings:
    """Configuracoes da sincronizacao de resultados PeixeEsperto."""

    enabled: bool
    group_slug: str
    sync_interval_seconds: int
    sync_max_age_hours: int
    sync_per_page: int
    sync_max_pages: int
    timezone_name: str


def load_peixeesperto_settings(env_path: str | Path = ".env") -> PeixeEspertoSettings:
    """Carrega variaveis `PEIXEESPERTO_*` do `.env`."""

    load_dotenv(env_path)
    return PeixeEspertoSettings(
        enabled=_get_bool("PEIXEESPERTO_RESULT_SYNC_ENABLED", False),
        group_slug=_get_str("PEIXEESPERTO_GROUP_SLUG", "aguas-profundas"),
        sync_interval_seconds=_get_int("PEIXEESPERTO_SYNC_INTERVAL_SECONDS", 300),
        sync_max_age_hours=_get_int("PEIXEESPERTO_SYNC_MAX_AGE_HOURS", 72),
        sync_per_page=_get_int("PEIXEESPERTO_SYNC_PER_PAGE", 50),
        sync_max_pages=_get_int("PEIXEESPERTO_SYNC_MAX_PAGES", 10),
        timezone_name=_get_str("APP_TIMEZONE", "America/Sao_Paulo"),
    )


def _get_float(name: str, default: float) -> float:
    value = _get_optional_str(name)
    if value is None:
        return default
    try:
        return float(value.replace(",", "."))
    except ValueError as exc:
        raise RuntimeError(f"Variavel {name} precisa ser um numero decimal. Valor recebido: {value}") from exc
