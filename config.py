"""Carregamento e validação das configurações do bot."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

DEFAULT_SPORT_NAMES: dict[int, str] = {
    1: "Futebol",
    2: "Tenis",
    3: "Basquete",
    26: "MMA",
}


@dataclass(frozen=True)
class Settings:
    """Configurações imutáveis carregadas do ambiente."""

    bankroll_id: int
    api_base_url: str
    poll_interval_seconds: int
    request_timeout_seconds: float
    request_max_retries: int
    backoff_initial_seconds: float
    reference_refresh_seconds: int
    max_pages: int
    target_tipster_name: str
    target_tipster_id: int | None
    target_tipster_names: tuple[str, ...]
    target_tipster_ids: tuple[int, ...]
    telegram_bot_token: str
    telegram_chat_id: str | None
    sqlite_path: Path
    user_agent: str
    sid: str | None
    app_header: str
    timezone: str
    notify_existing_on_first_run: bool
    log_level: str
    sport_names: dict[int, str]
    bookmaker_names: dict[str, str]
    copytrade_enabled: bool
    telegram_admin_user_id: int | None
    telegram_admin_user_ids: tuple[int, ...]
    telegram_reaction_poll_timeout_seconds: int
    copytrade_queue_batch_size: int
    copytrade_retry_max_seconds: int
    copytrade_duplicate_check_max_pages: int
    copytrade_bankroll_id: int
    copytrade_bankroll_internal_id: int
    copytrade_use_source_tipster: bool
    copytrade_auto_create_tipsters: bool
    copytrade_tipster_mapping: dict[str, str]
    copytrade_destination_tipster_name: str
    copytrade_destination_tipster_id: int | None
    bet_analytix_email: str | None
    bet_analytix_password: str | None
    bet_analytix_access_token: str | None
    betano_monitor_enabled: bool
    betano_monitor_interval_seconds: int
    betano_monitor_max_error_count: int
    betano_browser_fallback_enabled: bool
    betano_browser_headless: bool
    betano_browser_channel: str | None
    betano_browser_profile_dir: Path
    betano_browser_navigation_timeout_seconds: float


def load_settings(env_path: str | Path = ".env") -> Settings:
    """Carrega o arquivo `.env` e retorna as configurações validadas."""

    load_dotenv(env_path)

    telegram_bot_token = _get_str("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = _get_str("TELEGRAM_CHAT_ID", "")

    target_tipster_names = _get_list("TARGET_TIPSTER_NAMES")
    if not target_tipster_names:
        target_tipster_names = (_get_str("TARGET_TIPSTER_NAME", "Águas Profundas"),)

    target_tipster_ids = _get_int_list("TARGET_TIPSTER_IDS")
    legacy_target_tipster_id = _get_optional_int("TARGET_TIPSTER_ID", 265474)
    if not target_tipster_ids and legacy_target_tipster_id is not None:
        target_tipster_ids = (legacy_target_tipster_id,)

    return Settings(
        bankroll_id=_get_int("BET_ANALYTIX_BANKROLL_ID", 1827037),
        api_base_url=_get_str("BET_ANALYTIX_API_BASE_URL", "https://api-v2.bet-analytix.com").rstrip("/"),
        poll_interval_seconds=_get_int("POLL_INTERVAL_SECONDS", 60),
        request_timeout_seconds=_get_float("REQUEST_TIMEOUT_SECONDS", 20.0),
        request_max_retries=_get_int("REQUEST_MAX_RETRIES", 4),
        backoff_initial_seconds=_get_float("BACKOFF_INITIAL_SECONDS", 2.0),
        reference_refresh_seconds=_get_int("REFERENCE_REFRESH_SECONDS", 3600),
        max_pages=max(1, _get_int("BET_ANALYTIX_MAX_PAGES", 1)),
        target_tipster_name=target_tipster_names[0],
        target_tipster_id=target_tipster_ids[0] if target_tipster_ids else None,
        target_tipster_names=target_tipster_names,
        target_tipster_ids=target_tipster_ids,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        sqlite_path=Path(_get_str("SQLITE_PATH", "data/notified_bets.sqlite3")),
        user_agent=_get_str("BET_ANALYTIX_USER_AGENT", DEFAULT_USER_AGENT),
        sid=_get_optional_str("BET_ANALYTIX_SID", "152120"),
        app_header=_get_str("BET_ANALYTIX_APP_HEADER", "appBax"),
        timezone=_get_str("APP_TIMEZONE", "America/Sao_Paulo"),
        notify_existing_on_first_run=_get_bool("NOTIFY_EXISTING_ON_FIRST_RUN", False),
        log_level=_get_str("LOG_LEVEL", "INFO").upper(),
        sport_names=_load_sport_names(),
        bookmaker_names=_load_string_map("BOOKMAKER_NAMES_JSON"),
        copytrade_enabled=_get_bool("COPYTRADE_ENABLED", False),
        telegram_admin_user_id=_first_admin_user_id(),
        telegram_admin_user_ids=_get_admin_user_ids(),
        telegram_reaction_poll_timeout_seconds=_get_int("TELEGRAM_REACTION_POLL_TIMEOUT_SECONDS", 25),
        copytrade_queue_batch_size=_get_int("COPYTRADE_QUEUE_BATCH_SIZE", 5),
        copytrade_retry_max_seconds=_get_int("COPYTRADE_RETRY_MAX_SECONDS", 900),
        copytrade_duplicate_check_max_pages=max(1, _get_int("COPYTRADE_DUPLICATE_CHECK_MAX_PAGES", 10)),
        copytrade_bankroll_id=_get_int("COPYTRADE_BANKROLL_ID", 1829516),
        copytrade_bankroll_internal_id=_get_int("COPYTRADE_BANKROLL_INTERNAL_ID", 3),
        copytrade_use_source_tipster=_get_bool("COPYTRADE_USE_SOURCE_TIPSTER", True),
        copytrade_auto_create_tipsters=_get_bool("COPYTRADE_AUTO_CREATE_TIPSTERS", True),
        copytrade_tipster_mapping=_load_string_map("COPYTRADE_TIPSTER_MAPPING_JSON"),
        copytrade_destination_tipster_name=_get_str("COPYTRADE_DESTINATION_TIPSTER_NAME", "Matheus"),
        copytrade_destination_tipster_id=_get_optional_int("COPYTRADE_DESTINATION_TIPSTER_ID", 263913),
        bet_analytix_email=_get_optional_str("BET_ANALYTIX_EMAIL"),
        bet_analytix_password=_get_optional_str("BET_ANALYTIX_PASSWORD"),
        bet_analytix_access_token=_get_optional_str("BET_ANALYTIX_ACCESS_TOKEN"),
        betano_monitor_enabled=_get_bool("BETANO_MONITOR_ENABLED", False),
        betano_monitor_interval_seconds=max(2, _get_int("BETANO_MONITOR_INTERVAL_SECONDS", 5)),
        betano_monitor_max_error_count=max(3, _get_int("BETANO_MONITOR_MAX_ERROR_COUNT", 20)),
        betano_browser_fallback_enabled=_get_bool("BETANO_BROWSER_FALLBACK_ENABLED", False),
        betano_browser_headless=_get_bool("BETANO_BROWSER_HEADLESS", False),
        betano_browser_channel=_get_optional_str("BETANO_BROWSER_CHANNEL", "chrome"),
        betano_browser_profile_dir=Path(_get_str("BETANO_BROWSER_PROFILE_DIR", "data/betano_browser_profile")),
        betano_browser_navigation_timeout_seconds=max(
            5.0,
            _get_float("BETANO_BROWSER_NAVIGATION_TIMEOUT_SECONDS", 45.0),
        ),
    )


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Variável obrigatória ausente no .env: {name}")
    return value.strip()


def _get_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip()


def _get_optional_str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or None


def _get_int(name: str, default: int) -> int:
    value = _get_optional_str(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Variável {name} precisa ser um inteiro. Valor recebido: {value}") from exc


def _get_optional_int(name: str, default: int | None = None) -> int | None:
    value = _get_optional_str(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Variável {name} precisa ser um inteiro. Valor recebido: {value}") from exc


def _get_float(name: str, default: float) -> float:
    value = _get_optional_str(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"Variável {name} precisa ser numérica. Valor recebido: {value}") from exc


def _get_bool(name: str, default: bool) -> bool:
    value = _get_optional_str(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "t", "yes", "y", "sim", "s"}


def _get_list(name: str) -> tuple[str, ...]:
    value = _get_optional_str(name)
    if value is None:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _get_int_list(name: str) -> tuple[int, ...]:
    values = _get_list(name)
    result: list[int] = []
    for value in values:
        try:
            result.append(int(value))
        except ValueError as exc:
            raise RuntimeError(f"Variável {name} contém item não inteiro: {value}") from exc
    return tuple(result)


def _get_admin_user_ids() -> tuple[int, ...]:
    """Le TELEGRAM_ADMIN_USER_ID suportando multiplos IDs separados por virgula ou espaco."""

    value = _get_optional_str("TELEGRAM_ADMIN_USER_ID")
    if value is None:
        return ()
    cleaned = value.replace(" ", ",")
    values = [item.strip() for item in cleaned.split(",") if item.strip()]
    result: list[int] = []
    for item in values:
        try:
            result.append(int(item))
        except ValueError as exc:
            raise RuntimeError(f"TELEGRAM_ADMIN_USER_ID contém item não inteiro: {item}") from exc
    return tuple(result)


def _first_admin_user_id() -> int | None:
    ids = _get_admin_user_ids()
    return ids[0] if ids else None


def _load_sport_names() -> dict[int, str]:
    custom = _load_json_map("SPORT_NAMES_JSON")
    merged = dict(DEFAULT_SPORT_NAMES)
    for key, value in custom.items():
        try:
            merged[int(key)] = str(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"SPORT_NAMES_JSON contém chave inválida: {key!r}") from exc
    return merged


def _load_string_map(name: str) -> dict[str, str]:
    data = _load_json_map(name)
    return {str(key): str(value) for key, value in data.items()}


def _load_json_map(name: str) -> dict[str, Any]:
    raw = _get_optional_str(name)
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} precisa ser um JSON válido. Erro: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{name} precisa ser um objeto JSON, por exemplo: {{\"835\":\"Bet365\"}}")
    return data
