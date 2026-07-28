"""Parser regex para sinais recebidos pelo userbot MTProto."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

class UserbotSignalParseError(ValueError):
    """Erro quando a mensagem de sinal nao segue o formato esperado."""


@dataclass(frozen=True)
class ExternalBetSignal:
    """Sinal externo normalizado antes de virar aposta Bet-Analytix."""

    bookmaker: str
    event: str
    sport: str
    pick: str
    odd: float
    stake: float
    fair_odd: str | None
    limit: str | None
    edge: str | None
    freebet: str | None
    admin: str | None
    link: str | None
    raw_text: str
    event_datetime: datetime | None = None

    def note(self, chat_id: str, message_id: int) -> str:
        """Monta o comentario limpo para a aposta no Bet-Analytix."""

        if self.fair_odd:
            return f"Odd justa: {self.fair_odd}"
        return ""


HOUSE = "\U0001F3E0"
VERSUS = "\U0001F19A"
PIN = "\U0001F4CC"
TAG = "\U0001F3F7"
TRAFFIC_LIGHT = "\U0001F6A6"
STOP_SIGN = "\U0001F6D1"
MONEY_BAG = "\U0001F4B0"
FREE_BUTTON = "\U0001F193"
CRYSTAL_BALL = "\U0001F52E"
CALENDAR = "\U0001F4C6"
VARIATION_SELECTOR = "\ufe0f"

PATTERNS: dict[str, re.Pattern[str]] = {
    "bookmaker": re.compile(rf"^\s*{re.escape(HOUSE)}\s*(?P<value>[^\n\r]+)", re.MULTILINE),
    "event": re.compile(rf"^\s*{re.escape(VERSUS)}\s*(?P<value>[^\n\r]+)", re.MULTILINE),
    "pick": re.compile(rf"^\s*{re.escape(PIN)}\s*(?P<value>[^\n\r]+)", re.MULTILINE),
    "odd": re.compile(rf"^\s*{re.escape(TAG)}{VARIATION_SELECTOR}?\s*(?P<value>\d+(?:[.,]\d+)?)", re.MULTILINE),
    "limit": re.compile(
        rf"^\s*{re.escape(TRAFFIC_LIGHT)}\s*Limite da aposta\s*:\s*(?P<value>[^\n\r]+)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "edge": re.compile(rf"^\s*{re.escape(STOP_SIGN)}\s*(?P<value>[\d.,]+%)", re.MULTILINE),
    "stake": re.compile(rf"^\s*{re.escape(MONEY_BAG)}\s*(?P<value>R\$\s*[\d.,]+|[\d.,]+)", re.MULTILINE),
    "freebet": re.compile(rf"^\s*{re.escape(FREE_BUTTON)}\s*(?P<value>[^\n\r]+)", re.MULTILINE),
    "admin": re.compile(r"ADM\s*:\s*(?P<value>[^\n\r]+)", re.IGNORECASE),
    "event_datetime": re.compile(
        rf"^\s*{re.escape(CALENDAR)}{VARIATION_SELECTOR}?\s*(?P<value>\d{{2}}/\d{{2}}/\d{{4}}\s+\d{{2}}:\d{{2}}(?::\d{{2}})?)",
        re.MULTILINE,
    ),
}

SIGNAL_MARKER_PATTERN = re.compile(r"Jogue\s+com\s+responsabilidade", re.IGNORECASE)
ODD_CHANGED_MARKER_PATTERN = re.compile(r"Odd\s+mudou\?\s+\[?Clique\s+AQUI\]?", re.IGNORECASE)
FAIR_ODD_PATTERN = re.compile(r"Odd\s+justa\s*:\s*(?P<value>\d+(?:[.,]\d+)?)", re.IGNORECASE)


def parse_external_signal(
    message_text: str,
    bookmaker_aliases: dict[str, str] | None = None,
    signal_marker_pattern: re.Pattern[str] = SIGNAL_MARKER_PATTERN,
) -> ExternalBetSignal:
    """Extrai campos do formato de sinal externo.

    Campos obrigatorios: marcador de responsabilidade e cabecalho fixo ate
    ADM. Tudo que vier depois de ADM e ignorado para fins de mapeamento.
    """

    text = _clean_text(message_text)
    if not _has_signal_marker(text, signal_marker_pattern):
        raise UserbotSignalParseError("Marcador obrigatorio ausente na mensagem de sinal")

    header = _required_header(text)
    _validate_header_layout(header)

    bookmaker = _required(header, "bookmaker")
    event = _required_event(header)
    bookmaker = (bookmaker_aliases or {}).get(bookmaker, bookmaker)

    return ExternalBetSignal(
        bookmaker=bookmaker,
        event=event,
        sport=_required(header, "sport"),
        pick=_required(header, "pick"),
        odd=_parse_odd(_required(header, "odd")),
        stake=_parse_money(_required(header, "stake")),
        fair_odd=_optional_fair_odd(text),
        limit=_required(header, "limit"),
        edge=_required(header, "edge"),
        freebet=_required(header, "freebet"),
        admin=_required(header, "admin"),
        link=None,
        raw_text=text,
        event_datetime=_parse_event_datetime(text),
    )


def is_external_signal_message(
    message_text: str,
    signal_marker_pattern: re.Pattern[str] = SIGNAL_MARKER_PATTERN,
) -> bool:
    """Retorna `True` apenas para mensagens com marcador e campos seguros."""

    text = _clean_text(message_text)
    if not _has_signal_marker(text, signal_marker_pattern):
        return False

    try:
        header = _required_header(text)
        _validate_header_layout(header)
    except UserbotSignalParseError:
        return False

    return True


def _clean_text(message_text: str) -> str:
    lines = [line.rstrip() for line in message_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line.strip())


def _required(text: str, field: str) -> str:
    value = _optional(text, field)
    if not value:
        raise UserbotSignalParseError(f"Campo obrigatorio ausente: {field}")
    return value


def _required_event(text: str) -> str:
    return _required(text, "event")


def _has_signal_marker(text: str, pattern: re.Pattern[str]) -> bool:
    return pattern.search(text) is not None


def _optional_fair_odd(text: str) -> str | None:
    match = FAIR_ODD_PATTERN.search(text)
    if not match:
        return None
    return match.group("value").replace(",", ".").strip()


def _parse_event_datetime(text: str) -> datetime | None:
    """Extrai a data/hora do evento informada pelo administrador, se houver."""

    match = PATTERNS["event_datetime"].search(text)
    if not match:
        return None
    value = match.group("value").strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _required_header(text: str) -> str:
    lines = text.splitlines()
    admin_index = _line_index(lines, PATTERNS["admin"])
    if admin_index is None:
        raise UserbotSignalParseError("Cabecalho obrigatorio ausente: ADM")
    return "\n".join(lines[: admin_index + 1])


def _validate_header_layout(header: str) -> None:
    lines = header.splitlines()
    if len(lines) < 11:
        raise UserbotSignalParseError("Cabecalho incompleto para sinal.")

    expected_patterns: tuple[tuple[int, str], ...] = (
        (0, "bookmaker"),
        (1, "event"),
        (3, "pick"),
        (4, "odd"),
        (5, "limit"),
        (6, "edge"),
        (7, "stake"),
        (8, "freebet"),
        (10, "admin"),
    )
    for index, field in expected_patterns:
        if index >= len(lines) or not PATTERNS[field].search(lines[index]):
            raise UserbotSignalParseError(f"Cabecalho fora do padrao na linha {index + 1}: {field}")

    if not _optional_sport(header):
        raise UserbotSignalParseError("Cabecalho fora do padrao na linha 3: sport")

    if not lines[9].strip().startswith(CRYSTAL_BALL):
        raise UserbotSignalParseError("Cabecalho fora do padrao na linha 10: bloco de ADM")


def _optional(text: str, field: str) -> str | None:
    if field == "sport":
        return _optional_sport(text)

    pattern = PATTERNS[field]
    match = pattern.search(text)
    if not match:
        return None
    value = " ".join(match.group("value").strip().split())
    return value or None


def _optional_sport(text: str) -> str | None:
    """Extrai o esporte pela posicao estrutural, sem mapear emojis."""

    lines = text.splitlines()
    event_index = _line_index(lines, PATTERNS["event"])
    pick_index = _line_index(lines, PATTERNS["pick"])
    if event_index is None or pick_index is None or pick_index <= event_index + 1:
        return None

    candidate = lines[event_index + 1].strip()
    sport = _strip_leading_symbols(candidate)
    if not _looks_like_sport(sport):
        return None
    return sport


def _line_index(lines: list[str], pattern: re.Pattern[str]) -> int | None:
    for index, line in enumerate(lines):
        if pattern.search(line):
            return index
    return None


def _strip_leading_symbols(value: str) -> str:
    chars: list[str] = []
    started = False
    for char in value.strip():
        if not started:
            category = unicodedata.category(char)
            if char.isspace() or category[0] in {"M", "P", "S"}:
                continue
            started = True
        chars.append(char)
    return " ".join("".join(chars).strip().split())


def _looks_like_sport(value: str) -> bool:
    if not value or len(value) > 80:
        return False
    if not any(char.isalnum() for char in value):
        return False

    normalized = value.strip().lower()
    blocked_fragments = (
        "adm:",
        "clique aqui",
        "jogue com responsabilidade",
        "limite da aposta",
        "link",
        "odd justa",
        "odd mudou",
    )
    return not any(fragment in normalized for fragment in blocked_fragments)


def _parse_odd(value: str) -> float:
    try:
        return float(value.replace(",", "."))
    except ValueError as exc:
        raise UserbotSignalParseError(f"Odd invalida: {value}") from exc


def _parse_money(value: str) -> float:
    normalized = value.upper().replace("R$", "").replace(" ", "").strip()
    if "," in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError as exc:
        raise UserbotSignalParseError(f"Stake invalida: {value}") from exc
