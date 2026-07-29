"""Configurações centralizadas da identificação de eventos esportivos."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


SUPPORTED_MODES = {"disabled", "shadow", "enabled"}


@dataclass(frozen=True)
class SportsEventSettings:
    mode: str
    cache_path: Path
    lookback_hours: int
    lookahead_days: int
    min_confidence: float
    min_score_gap: float
    participant_min_score: float
    time_tolerance_minutes: int
    cache_ttl_seconds: int
    negative_cache_ttl_seconds: int
    request_timeout_seconds: float
    request_max_retries: int
    backoff_initial_seconds: float
    total_timeout_seconds: float
    lock_ttl_seconds: int
    lock_wait_seconds: float
    recheck_scheduler_interval_seconds: int
    recheck_within_24h_seconds: int
    recheck_within_7d_seconds: int
    recheck_far_seconds: int
    store_raw_payload: bool
    participant_aliases: dict[str, str]
    provider_daily_limits: dict[str, int]
    provider_minute_limits: dict[str, int]
    football_providers: tuple[str, ...]
    basketball_providers: tuple[str, ...]
    tennis_providers: tuple[str, ...]
    api_football_enabled: bool
    api_football_key: str | None
    api_football_base_url: str
    api_basketball_enabled: bool
    api_basketball_key: str | None
    api_basketball_base_url: str
    football_data_enabled: bool
    football_data_api_key: str | None
    football_data_base_url: str
    live_tennis_enabled: bool
    live_tennis_api_key: str | None
    live_tennis_base_url: str
    thesportsdb_enabled: bool
    thesportsdb_api_key: str
    thesportsdb_base_url: str

    @property
    def enabled(self) -> bool:
        return self.mode != "disabled"

    def providers_for_sport(self, sport: str) -> tuple[str, ...]:
        if sport == "football":
            return self.football_providers
        if sport == "basketball":
            return self.basketball_providers
        if sport == "tennis":
            return self.tennis_providers
        return ()


def load_sports_event_settings(env_path: str | Path = ".env") -> SportsEventSettings:
    load_dotenv(env_path)

    explicit_mode = _optional_str("SPORTS_EVENT_MATCHING_MODE")
    if explicit_mode is None:
        mode = "enabled" if _get_bool("SPORTS_EVENT_MATCHING_ENABLED", False) else "disabled"
    else:
        mode = explicit_mode.casefold()
    if mode not in SUPPORTED_MODES:
        raise RuntimeError(
            "SPORTS_EVENT_MATCHING_MODE deve ser disabled, shadow ou enabled."
        )

    settings = SportsEventSettings(
        mode=mode,
        cache_path=Path(_get_str("SPORTS_EVENT_CACHE_PATH", "data/sports_schedule.sqlite3")),
        lookback_hours=max(0, _get_int("SPORTS_EVENT_LOOKBACK_HOURS", 24)),
        lookahead_days=max(1, _get_int("SPORTS_EVENT_LOOKAHEAD_DAYS", 7)),
        min_confidence=_get_float("SPORTS_EVENT_MIN_CONFIDENCE", 0.90),
        min_score_gap=_get_float("SPORTS_EVENT_MIN_SCORE_GAP", 0.10),
        participant_min_score=_get_float("SPORTS_EVENT_PARTICIPANT_MIN_SCORE", 0.86),
        time_tolerance_minutes=max(0, _get_int("SPORTS_EVENT_TIME_TOLERANCE_MINUTES", 15)),
        cache_ttl_seconds=max(300, _get_int("SPORTS_EVENT_CACHE_TTL_SECONDS", 21600)),
        negative_cache_ttl_seconds=max(
            60,
            _get_int("SPORTS_EVENT_NEGATIVE_CACHE_TTL_SECONDS", 900),
        ),
        request_timeout_seconds=max(
            1.0,
            _get_float("SPORTS_EVENT_REQUEST_TIMEOUT_SECONDS", 5.0),
        ),
        request_max_retries=max(0, _get_int("SPORTS_EVENT_REQUEST_MAX_RETRIES", 1)),
        backoff_initial_seconds=max(
            0.1,
            _get_float("SPORTS_EVENT_BACKOFF_INITIAL_SECONDS", 0.5),
        ),
        total_timeout_seconds=max(
            2.0,
            _get_float("SPORTS_EVENT_TOTAL_TIMEOUT_SECONDS", 15.0),
        ),
        lock_ttl_seconds=max(10, _get_int("SPORTS_EVENT_LOCK_TTL_SECONDS", 60)),
        lock_wait_seconds=max(
            0.0,
            _get_float("SPORTS_EVENT_LOCK_WAIT_SECONDS", 2.0),
        ),
        recheck_scheduler_interval_seconds=max(
            60,
            _get_int("SPORTS_EVENT_RECHECK_INTERVAL_SECONDS", 900),
        ),
        recheck_within_24h_seconds=max(
            300,
            _get_int("SPORTS_EVENT_RECHECK_WITHIN_24H_SECONDS", 1800),
        ),
        recheck_within_7d_seconds=max(
            1800,
            _get_int("SPORTS_EVENT_RECHECK_WITHIN_7D_SECONDS", 21600),
        ),
        recheck_far_seconds=max(
            3600,
            _get_int("SPORTS_EVENT_RECHECK_FAR_SECONDS", 86400),
        ),
        store_raw_payload=_get_bool("SPORTS_EVENT_STORE_RAW_PAYLOAD", True),
        participant_aliases=_load_aliases("SPORTS_EVENT_PARTICIPANT_ALIASES_JSON"),
        provider_daily_limits=_load_int_map(
            "SPORTS_EVENT_PROVIDER_DAILY_LIMITS_JSON",
            {
                "api_football": 100,
                "api_basketball": 100,
                "football_data": 5000,
                "live_tennis": 1000,
                "thesportsdb": 10000,
            },
        ),
        provider_minute_limits=_load_int_map(
            "SPORTS_EVENT_PROVIDER_MINUTE_LIMITS_JSON",
            {
                "api_football": 300,
                "api_basketball": 300,
                "football_data": 10,
                "live_tennis": 30,
                "thesportsdb": 30,
            },
        ),
        football_providers=_get_list(
            "SPORTS_EVENT_FOOTBALL_PROVIDERS",
            ("api_football", "football_data", "thesportsdb"),
        ),
        basketball_providers=_get_list(
            "SPORTS_EVENT_BASKETBALL_PROVIDERS",
            ("api_basketball", "thesportsdb"),
        ),
        tennis_providers=_get_list(
            "SPORTS_EVENT_TENNIS_PROVIDERS",
            ("live_tennis", "thesportsdb"),
        ),
        api_football_enabled=_get_bool("API_FOOTBALL_ENABLED", True),
        api_football_key=_optional_str("API_FOOTBALL_KEY"),
        api_football_base_url=_get_str(
            "API_FOOTBALL_BASE_URL",
            "https://v3.football.api-sports.io",
        ).rstrip("/"),
        api_basketball_enabled=_get_bool("API_BASKETBALL_ENABLED", True),
        api_basketball_key=_optional_str("API_BASKETBALL_KEY"),
        api_basketball_base_url=_get_str(
            "API_BASKETBALL_BASE_URL",
            "https://v1.basketball.api-sports.io",
        ).rstrip("/"),
        football_data_enabled=_get_bool("FOOTBALL_DATA_ENABLED", True),
        football_data_api_key=_optional_str("FOOTBALL_DATA_API_KEY"),
        football_data_base_url=_get_str(
            "FOOTBALL_DATA_BASE_URL",
            "https://api.football-data.org/v4",
        ).rstrip("/"),
        live_tennis_enabled=_get_bool("LIVE_TENNIS_API_ENABLED", True),
        live_tennis_api_key=_optional_str("LIVE_TENNIS_API_KEY"),
        live_tennis_base_url=_get_str(
            "LIVE_TENNIS_API_BASE_URL",
            "https://api.livetennisapi.com/api/public/v1",
        ).rstrip("/"),
        thesportsdb_enabled=_get_bool("THESPORTSDB_ENABLED", True),
        thesportsdb_api_key=_get_str("THESPORTSDB_API_KEY", "123"),
        thesportsdb_base_url=_get_str(
            "THESPORTSDB_BASE_URL",
            "https://www.thesportsdb.com/api/v1/json",
        ).rstrip("/"),
    )
    _validate(settings)
    return settings


def _validate(settings: SportsEventSettings) -> None:
    if not settings.enabled:
        return
    if not 0.5 <= settings.min_confidence <= 1.0:
        raise RuntimeError("SPORTS_EVENT_MIN_CONFIDENCE deve estar entre 0.5 e 1.0.")
    if not 0.0 <= settings.min_score_gap <= 1.0:
        raise RuntimeError("SPORTS_EVENT_MIN_SCORE_GAP deve estar entre 0 e 1.")
    if not 0.5 <= settings.participant_min_score <= 1.0:
        raise RuntimeError(
            "SPORTS_EVENT_PARTICIPANT_MIN_SCORE deve estar entre 0.5 e 1.0."
        )


def _get_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


def _optional_str(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _get_bool(name: str, default: bool) -> bool:
    value = _optional_str(name)
    if value is None:
        return default
    return value.casefold() in {"1", "true", "t", "yes", "y", "sim", "s"}


def _get_int(name: str, default: int) -> int:
    value = _optional_str(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} precisa ser inteiro: {value}") from exc


def _get_float(name: str, default: float) -> float:
    value = _optional_str(name)
    if value is None:
        return default
    try:
        return float(value.replace(",", "."))
    except ValueError as exc:
        raise RuntimeError(f"{name} precisa ser decimal: {value}") from exc


def _get_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = _optional_str(name)
    if value is None:
        return default
    return tuple(item.strip().casefold() for item in value.split(",") if item.strip())


def _load_json_object(name: str) -> dict[str, Any]:
    value = _optional_str(name)
    if value is None:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} precisa ser JSON válido: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{name} precisa ser um objeto JSON.")
    return parsed


def _load_aliases(name: str) -> dict[str, str]:
    raw = _load_json_object(name)
    result: dict[str, str] = {}
    for alias, canonical_or_aliases in raw.items():
        if isinstance(canonical_or_aliases, str):
            result[str(alias)] = canonical_or_aliases
            continue
        if isinstance(canonical_or_aliases, list):
            canonical = str(alias)
            for item in canonical_or_aliases:
                result[str(item)] = canonical
            continue
        raise RuntimeError(
            f"{name} aceita valores string ou listas de aliases; inválido em {alias!r}."
        )
    return result


def _load_int_map(name: str, default: dict[str, int]) -> dict[str, int]:
    merged = dict(default)
    for key, value in _load_json_object(name).items():
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{name} contém limite inválido em {key!r}.") from exc
        merged[str(key).casefold()] = max(0, parsed)
    return merged
