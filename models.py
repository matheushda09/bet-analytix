"""Modelos e funções de parsing para as apostas do Bet-Analytix."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


RawBet = dict[str, Any]


@dataclass(frozen=True)
class Tipster:
    """Representa um tipster cadastrado na bankroll."""

    id: int
    name: str


@dataclass(frozen=True)
class Bet:
    """Aposta normalizada para persistência e notificação."""

    id: int
    tipster_id: int | None
    tipster_name: str
    event_timestamp: int | None
    event_datetime: str
    sport: str
    league: str | None
    event: str | None
    pick: str
    odd: str
    stake: str
    bookmaker: str | None
    raw: RawBet


def build_tipster_map(all_data_payload: dict[str, Any]) -> dict[int, Tipster]:
    """Monta um mapa `id -> Tipster` a partir do endpoint `/bankroll/all-data/{id_user}`."""

    tipsters = all_data_payload.get("tipsters")
    if not isinstance(tipsters, list):
        raise ValueError("Payload de referências não contém a lista 'tipsters'.")

    result: dict[int, Tipster] = {}
    for item in tipsters:
        if not isinstance(item, dict):
            continue
        tipster_id = to_int(item.get("id"))
        name = as_text(item.get("name"))
        if tipster_id is not None and name:
            result[tipster_id] = Tipster(id=tipster_id, name=name)
    return result


def build_bookmaker_map(bookmakers_payload: Any) -> dict[str, str]:
    """Monta um mapa `id -> nome` a partir do endpoint `/bookmakers`."""

    if not isinstance(bookmakers_payload, list):
        raise ValueError("Payload de bookmakers não é uma lista.")

    result: dict[str, str] = {}
    for item in bookmakers_payload:
        if not isinstance(item, dict):
            continue
        bookmaker_id = as_text(item.get("id"))
        name = as_text(item.get("name"))
        if bookmaker_id and name:
            result[bookmaker_id] = name
    return result


def resolve_tipster_id(tipsters_by_id: dict[int, Tipster], target_name: str) -> int | None:
    """Resolve o ID de um tipster pelo nome exato normalizado em Unicode."""

    normalized_target = normalize_text(target_name)
    for tipster in tipsters_by_id.values():
        if normalize_text(tipster.name) == normalized_target:
            return tipster.id
    return None


def filter_bets_by_tipster(
    raw_bets: list[RawBet],
    tipsters_by_id: dict[int, Tipster],
    target_name: str,
    fallback_target_id: int | None = None,
) -> list[RawBet]:
    """Filtra apostas cujo tipster resolvido seja exatamente o alvo configurado.

    O endpoint de apostas traz `tipster` como ID numérico. Por isso, a comparação
    principal é feita contra o nome obtido em `all-data`. Se esse mapa não estiver
    disponível temporariamente, o ID conhecido no HAR pode ser usado como fallback.
    """

    normalized_target = normalize_text(target_name)
    filtered: list[RawBet] = []

    for raw_bet in raw_bets:
        tipster_id = to_int(raw_bet.get("tipster"))
        tipster_name = tipsters_by_id.get(tipster_id).name if tipster_id in tipsters_by_id else None

        if tipster_name is not None and normalize_text(tipster_name) == normalized_target:
            filtered.append(raw_bet)
            continue

        raw_tipster_name = as_text(raw_bet.get("tipster_name") or raw_bet.get("tipsterName"))
        if raw_tipster_name and normalize_text(raw_tipster_name) == normalized_target:
            filtered.append(raw_bet)
            continue

        if fallback_target_id is not None and tipster_id == fallback_target_id and not tipsters_by_id:
            filtered.append(raw_bet)

    return filtered


def filter_bets_by_tipsters(
    raw_bets: list[RawBet],
    tipsters_by_id: dict[int, Tipster],
    target_names: tuple[str, ...],
    fallback_target_ids: tuple[int, ...] = (),
) -> list[RawBet]:
    """Filtra apostas de qualquer tipster alvo configurado."""

    normalized_targets = {normalize_text(name) for name in target_names}
    fallback_ids = set(fallback_target_ids)
    filtered: list[RawBet] = []

    for raw_bet in raw_bets:
        tipster_id = to_int(raw_bet.get("tipster"))
        tipster_name = tipsters_by_id.get(tipster_id).name if tipster_id in tipsters_by_id else None

        if tipster_name is not None and normalize_text(tipster_name) in normalized_targets:
            filtered.append(raw_bet)
            continue

        raw_tipster_name = as_text(raw_bet.get("tipster_name") or raw_bet.get("tipsterName"))
        if raw_tipster_name and normalize_text(raw_tipster_name) in normalized_targets:
            filtered.append(raw_bet)
            continue

        if tipster_id in fallback_ids and not tipsters_by_id:
            filtered.append(raw_bet)

    return filtered


def parse_bet(
    raw_bet: RawBet,
    tipsters_by_id: dict[int, Tipster],
    sport_names: dict[int, str],
    bookmaker_names: dict[str, str],
    timezone_name: str,
    default_tipster_name: str,
) -> Bet:
    """Converte uma aposta bruta da API em uma estrutura pronta para notificação."""

    bet_id = to_int(raw_bet.get("id"))
    if bet_id is None:
        raise ValueError(f"Aposta sem ID válido: {raw_bet!r}")

    tipster_id = to_int(raw_bet.get("tipster"))
    tipster_name = _resolve_tipster_name(raw_bet, tipster_id, tipsters_by_id, default_tipster_name)
    sport_id = to_int(raw_bet.get("sport"))
    sport = _resolve_sport_name(sport_id, sport_names)
    bookmaker = _resolve_bookmaker_name(raw_bet.get("bookmaker"), bookmaker_names)
    timestamp = to_int(raw_bet.get("date"))

    return Bet(
        id=bet_id,
        tipster_id=tipster_id,
        tipster_name=tipster_name,
        event_timestamp=timestamp,
        event_datetime=format_epoch(timestamp, timezone_name),
        sport=sport,
        league=first_text(raw_bet, "competition", "category"),
        event=first_text(raw_bet, "event", "match", "game"),
        pick=first_text(raw_bet, "label", "pick", "selection") or "Não informado",
        odd=as_text(raw_bet.get("odds")) or "Não informado",
        stake=as_text(raw_bet.get("stake") or raw_bet.get("unique_stake")) or "Não informado",
        bookmaker=bookmaker,
        raw=raw_bet,
    )


def normalize_text(value: str | None) -> str:
    """Normaliza texto para comparação estável de nomes com acentos."""

    if value is None:
        return ""
    return unicodedata.normalize("NFC", value.strip())


def as_text(value: Any) -> str | None:
    """Converte valores simples para texto, preservando `None` como ausente."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_int(value: Any) -> int | None:
    """Converte um valor para inteiro quando possível."""

    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def first_text(raw_bet: RawBet, *keys: str) -> str | None:
    """Retorna o primeiro campo textual preenchido entre as chaves informadas."""

    for key in keys:
        value = as_text(raw_bet.get(key))
        if value:
            return value
    return None


def format_epoch(timestamp: int | None, timezone_name: str) -> str:
    """Formata um timestamp Unix em segundos na timezone configurada."""

    if timestamp is None:
        return "Não informado"
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    return datetime.fromtimestamp(timestamp, tz=timezone).strftime("%d/%m/%Y %H:%M")


def _resolve_tipster_name(
    raw_bet: RawBet,
    tipster_id: int | None,
    tipsters_by_id: dict[int, Tipster],
    default_tipster_name: str,
) -> str:
    if tipster_id is not None and tipster_id in tipsters_by_id:
        return tipsters_by_id[tipster_id].name
    return first_text(raw_bet, "tipster_name", "tipsterName") or default_tipster_name


def _resolve_sport_name(sport_id: int | None, sport_names: dict[int, str]) -> str:
    if sport_id is None:
        return "Não informado"
    return sport_names.get(sport_id, f"ID {sport_id}")


def _resolve_bookmaker_name(value: Any, bookmaker_names: dict[str, str]) -> str | None:
    bookmaker_id = as_text(value)
    if bookmaker_id is None:
        return None
    return bookmaker_names.get(bookmaker_id, f"ID {bookmaker_id}")
