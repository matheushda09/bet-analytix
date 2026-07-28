"""Parser das mensagens de tips enviadas ao Telegram."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ParsedTelegramTip:
    """Dados normalizados de uma tip recebida por reação no Telegram."""

    tipster: str
    event_datetime: datetime
    sport: str
    league: str | None
    pick: str
    odd: float
    stake: float
    bookmaker: str
    source_bet_id: int
    event: str | None = None
    extra_note: str | None = None
    is_accumulator: bool = False


class TipParseError(ValueError):
    """Erro levantado quando uma mensagem não segue o formato esperado."""


FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "tipster": re.compile(r"Tipster\s*:\s*(?P<value>.+)", re.IGNORECASE),
    "event_datetime": re.compile(r"Data/Hora\s+do\s+Evento\s*:\s*(?P<value>.+)", re.IGNORECASE),
    "sport_league": re.compile(r"Esporte/Liga\s*:\s*(?P<value>.+)", re.IGNORECASE),
    "pick": re.compile(r"Aposta\s*\(\s*Pick\s*\)\s*:\s*(?P<value>.+)", re.IGNORECASE),
    "odd": re.compile(r"Odd\s*:\s*(?P<value>[\d.,]+)", re.IGNORECASE),
    "stake": re.compile(r"Stake\s*:\s*(?P<value>[\d.,]+)", re.IGNORECASE),
    "bookmaker": re.compile(r"Casa\s*:\s*(?P<value>.+)", re.IGNORECASE),
    "source_bet_id": re.compile(r"Bet\s*ID\s*:\s*(?P<value>\d+)", re.IGNORECASE),
}


def parse_tip_message(message_text: str) -> ParsedTelegramTip:
    """Extrai os campos de uma mensagem de tip do Telegram.

    O parser remove tags HTML, ignora emojis e tolera espaços extras. Ele valida
    explicitamente os campos necessários para não registrar apostas incompletas.
    """

    cleaned = _clean_message(message_text)
    if not is_tip_message(message_text):
        raise TipParseError("Mensagem não parece ser uma tip enviada pelo monitor.")
    fields = {name: _extract_field(cleaned, pattern, name) for name, pattern in FIELD_PATTERNS.items()}
    sport, league = _split_sport_league(fields["sport_league"])

    return ParsedTelegramTip(
        tipster=fields["tipster"],
        event_datetime=_parse_datetime(fields["event_datetime"]),
        sport=sport,
        league=league,
        pick=fields["pick"],
        odd=_parse_decimal(fields["odd"], "Odd"),
        stake=_parse_decimal(fields["stake"], "Stake"),
        bookmaker=fields["bookmaker"],
        source_bet_id=int(fields["source_bet_id"]),
    )


def is_tip_message(message_text: str) -> bool:
    """Retorna `True` somente para mensagens no formato de aposta monitorada."""

    cleaned = _clean_message(message_text)
    return "Nova aposta detectada" in cleaned and "Bet ID" in cleaned and "Aposta" in cleaned


def _clean_message(message_text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "", message_text)
    unescaped = html.unescape(without_tags)
    lines = [" ".join(line.strip().split()) for line in unescaped.splitlines()]
    return "\n".join(line for line in lines if line)


def _extract_field(cleaned_text: str, pattern: re.Pattern[str], field_name: str) -> str:
    match = pattern.search(cleaned_text)
    if not match:
        raise TipParseError(f"Campo obrigatório ausente na mensagem: {field_name}")
    value = match.group("value").strip()
    if not value:
        raise TipParseError(f"Campo obrigatório vazio na mensagem: {field_name}")
    return value


def _split_sport_league(value: str) -> tuple[str, str | None]:
    parts = [part.strip() for part in value.split("/", maxsplit=1)]
    sport = parts[0]
    league = parts[1] if len(parts) > 1 and parts[1] else None
    return sport, league


def _parse_decimal(value: str, field_name: str) -> float:
    normalized = value.replace(",", ".")
    try:
        return float(normalized)
    except ValueError as exc:
        raise TipParseError(f"{field_name} inválida: {value}") from exc


def _parse_datetime(value: str) -> datetime:
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise TipParseError(f"Data/Hora do Evento inválida: {value}")
