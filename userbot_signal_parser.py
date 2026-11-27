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
    source_bookmaker: str
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
    league: str | None = None
    usd_brl_rate: float | None = None

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
TROPHY = "\U0001F3C6"
VARIATION_SELECTOR = "\ufe0f"

# Valor monetario: "R$27,00", "27,00" ou o formato em dolar com o valor em
# reais entre parenteses, "US$4,59 (R$27,00)". A bankroll e em reais, entao o
# valor usado e sempre o R$ (ver `_parse_brl_amount`).
MONEY_NUMBER = r"[\d.,]+"
STAKE_VALUE = (
    rf"(?:US\$|U\$|\$)\s*{MONEY_NUMBER}\s*\(\s*R\$\s*{MONEY_NUMBER}\s*\)"
    rf"|R\$\s*{MONEY_NUMBER}"
    rf"|{MONEY_NUMBER}"
)
BRL_AMOUNT_PATTERN = re.compile(rf"R\$\s*(?P<value>{MONEY_NUMBER})", re.IGNORECASE)
USD_BRL_PAIR_PATTERN = re.compile(
    rf"(?:US\$|U\$|\$)\s*(?P<usd>{MONEY_NUMBER})\s*\(\s*R\$\s*(?P<brl>{MONEY_NUMBER})\s*\)",
    re.IGNORECASE,
)
# "🏆 Liga Europa". Usa [ \t]* para um 🏆 vazio nao capturar a linha seguinte.
LEAGUE_PATTERN = re.compile(rf"^[ \t]*{re.escape(TROPHY)}{VARIATION_SELECTOR}?[ \t]*(?P<value>[^\n\r]*)$", re.MULTILINE)

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
    "stake": re.compile(rf"^\s*{re.escape(MONEY_BAG)}\s*(?P<value>{STAKE_VALUE})\s*$", re.MULTILINE),
    "freebet": re.compile(rf"^\s*{re.escape(FREE_BUTTON)}\s*(?P<value>[^\n\r]+)", re.MULTILINE),
    "admin": re.compile(r"ADM\s*:\s*(?P<value>[^\n\r]+)", re.IGNORECASE),
    # Aceita "📆 26/11/2026 14:45", "📆 26/11/2026 14h45" e a mesma linha sem
    # o emoji (layout sem emojis). Exige que a data abra a linha para nao
    # capturar textos como "Ja temos aposta enviada dia 26/11/2026".
    "event_datetime": re.compile(
        rf"^\s*(?:{re.escape(CALENDAR)}{VARIATION_SELECTOR}?\s*)?"
        r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+(?:[àa]s\s+)?"
        r"(?P<hour>\d{1,2})\s*(?:h|:)\s*(?P<minute>\d{2})?(?::(?P<second>\d{2}))?\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
}

SIGNAL_MARKER_PATTERN = re.compile(r"Jogue\s+com\s+responsabilidade", re.IGNORECASE)
ODD_CHANGED_MARKER_PATTERN = re.compile(r"Odd\s+mudou\?\s+\[?Clique\s+AQUI\]?", re.IGNORECASE)
FAIR_ODD_PATTERN = re.compile(r"Odd\s+justa\s*:\s*(?P<value>\d+(?:[.,]\d+)?)", re.IGNORECASE)
OVERLOAD_TITLE_PATTERN = re.compile(
    r"^\s*(?:SOBRECARGA|\*\*SOBRECARGA\*\*|__SOBRECARGA__)\s*$",
    re.IGNORECASE,
)
# Layout posicional (sem emojis). Indices relativos a linha da casa; quando ha
# o titulo SOBRECARGA ele ocupa a linha 0 e tudo e deslocado em uma linha.
OVERLOAD_FIELD_LINES: dict[str, int] = {
    "bookmaker": 0,
    "event": 1,
    "sport": 2,
    "pick": 3,
    "odd": 4,
    "limit": 5,
    "edge": 6,
    "stake": 7,
    "freebet": 8,
}
OVERLOAD_ADMIN_START_LINE = 9
OVERLOAD_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "odd": re.compile(r"^\s*(?P<value>\d+(?:[.,]\d+)?)\s*$"),
    "limit": re.compile(
        r"^\s*Limite da aposta\s*:\s*(?P<value>[^\n\r]+?)\s*$",
        re.IGNORECASE,
    ),
    "edge": re.compile(r"^\s*(?P<value>[\d.,]+\s*%)\s*$"),
    "stake": re.compile(rf"^\s*(?P<value>{STAKE_VALUE})\s*$", re.IGNORECASE),
    "freebet": re.compile(r"^\s*(?P<value>Sim|N[aã]o)\s*$", re.IGNORECASE),
    "admin": re.compile(r"^\s*ADM\s*:\s*(?P<value>[^\n\r]+?)\s*$", re.IGNORECASE),
}
OVERLOAD_ATTRIBUTION_PATTERN = re.compile(
    r"^\s*(?:@\S+|<@!?\d+>)\s*$",
    re.IGNORECASE,
)


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

    source_bookmaker = _required(header, "bookmaker")
    event = _required_event(header)
    bookmaker = _resolve_bookmaker_alias(source_bookmaker, bookmaker_aliases or {})

    return ExternalBetSignal(
        bookmaker=bookmaker,
        source_bookmaker=source_bookmaker,
        event=event,
        sport=_required(header, "sport"),
        pick=_required(header, "pick"),
        odd=_parse_odd(_required(header, "odd")),
        stake=_parse_brl_amount(_required(header, "stake")),
        fair_odd=_optional_fair_odd(text),
        limit=_normalize_limit(_required(header, "limit")),
        edge=_required(header, "edge"),
        freebet=_required(header, "freebet"),
        admin=_required(header, "admin"),
        link=None,
        raw_text=text,
        event_datetime=_parse_event_datetime(text),
        league=_parse_league(text),
        usd_brl_rate=_usd_brl_rate(header),
    )


def _parse_league(text: str) -> str | None:
    """Campeonato: linha 🏆 ou, no layout sem emojis, a linha apos a data.

    No layout sem emojis o campeonato so e aceito quando vem logo depois da
    linha de data/hora que segue o ADM, para nao confundir com comentarios.
    """

    match = LEAGUE_PATTERN.search(text)
    if match:
        value = " ".join(match.group("value").split())
        return value or None

    lines = text.splitlines()
    if not lines or not _is_positional_header(text):
        return None
    admin_index = _line_index(lines, PATTERNS["admin"])
    if admin_index is None or admin_index + 1 >= len(lines):
        return None
    candidate_index = admin_index + 1
    if PATTERNS["event_datetime"].fullmatch(lines[candidate_index]):
        candidate_index += 1
    if candidate_index >= len(lines):
        return None
    candidate = " ".join(lines[candidate_index].split())
    if not candidate or len(candidate) > 50 or not _looks_like_overload_value(candidate):
        return None
    if any(char in candidate for char in "?!@:") or candidate.casefold().startswith(("ja temos", "já temos")):
        return None
    return candidate


def _usd_brl_rate(header: str) -> float | None:
    """Cotacao implicita do sinal (R$ / US$), usada para converter respostas em dolar."""

    for match in USD_BRL_PAIR_PATTERN.finditer(header):
        try:
            usd = _parse_money(match.group("usd"))
            brl = _parse_money(match.group("brl"))
        except UserbotSignalParseError:
            continue
        if usd > 0 and brl > 0:
            return round(brl / usd, 4)
    return None


def _resolve_bookmaker_alias(bookmaker: str, aliases: dict[str, str]) -> str:
    """Resolve alias sem diferenciar maiusculas, minusculas, acentos ou pontuacao."""

    normalized_bookmaker = _normalize_bookmaker_name(bookmaker)
    for original, mapped in aliases.items():
        if _normalize_bookmaker_name(original) == normalized_bookmaker:
            return mapped
    return bookmaker


def _normalize_bookmaker_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value).casefold())
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return "".join(char for char in without_marks if char.isalnum())


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
    try:
        day, month, year = (int(part) for part in match.group("date").split("/"))
        return datetime(
            year,
            month,
            day,
            int(match.group("hour")),
            int(match.group("minute") or 0),
            int(match.group("second") or 0),
        )
    except ValueError:
        return None


def _required_header(text: str) -> str:
    lines = text.splitlines()
    admin_index = _line_index(lines, PATTERNS["admin"])
    if admin_index is None:
        raise UserbotSignalParseError("Cabecalho obrigatorio ausente: ADM")
    return "\n".join(lines[: admin_index + 1])


def _validate_header_layout(header: str) -> None:
    if _is_positional_header(header):
        _validate_overload_header_layout(header)
        return

    lines = header.splitlines()
    if len(lines) < 10:
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
    )
    for index, field in expected_patterns:
        if index >= len(lines) or not PATTERNS[field].search(lines[index]):
            raise UserbotSignalParseError(f"Cabecalho fora do padrao na linha {index + 1}: {field}")

    if not _optional_sport(header):
        raise UserbotSignalParseError("Cabecalho fora do padrao na linha 3: sport")

    # O ADM vem logo apos o 🆓 (sinais do FishBot) ou apos uma linha 🔮.
    if len(lines) == 10:
        admin_index = 9
    elif len(lines) == 11 and lines[9].strip().startswith(CRYSTAL_BALL):
        admin_index = 10
    else:
        raise UserbotSignalParseError("Cabecalho fora do padrao na linha 10: bloco de ADM")
    if not PATTERNS["admin"].search(lines[admin_index]):
        raise UserbotSignalParseError(f"Cabecalho fora do padrao na linha {admin_index + 1}: admin")


def _optional(text: str, field: str) -> str | None:
    if _is_positional_header(text):
        return _optional_overload_field(text, field)

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

    if _is_positional_header(text):
        return _optional_overload_field(text, "sport")

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


def _is_overload_header(text: str) -> bool:
    lines = text.splitlines()
    return bool(lines and OVERLOAD_TITLE_PATTERN.fullmatch(lines[0]))


def _is_positional_header(text: str) -> bool:
    """Layout sem emojis: com titulo SOBRECARGA ou comecando direto pela casa.

    Sinais sem emoji tambem chegam sem o titulo (ex.: tips convergentes do
    FishBot). Nesse caso a validacao posicional estrita de todos os campos
    (odd, limite, %, stake, freebet e ADM) e o que garante a seguranca.
    """

    if _is_overload_header(text):
        return True
    lines = text.splitlines()
    return bool(lines) and not PATTERNS["bookmaker"].search(lines[0])


def _positional_offset(lines: list[str]) -> int:
    return 1 if lines and OVERLOAD_TITLE_PATTERN.fullmatch(lines[0]) else 0


def _validate_overload_header_layout(header: str) -> None:
    """Valida o layout sem emojis identificado pelo titulo SOBRECARGA."""

    lines = header.splitlines()
    offset = _positional_offset(lines)
    if len(lines) < OVERLOAD_ADMIN_START_LINE + offset + 1:
        raise UserbotSignalParseError("Cabecalho sem emojis incompleto para sinal.")

    for field, line_index in OVERLOAD_FIELD_LINES.items():
        if not _optional_overload_field(header, field):
            raise UserbotSignalParseError(
                f"Cabecalho sem emojis fora do padrao na linha {line_index + offset + 1}: {field}"
            )

    admin_index = _overload_admin_line_index(lines)
    if admin_index is None:
        raise UserbotSignalParseError("Cabecalho sem emojis fora do padrao: admin")

    for line_index in range(OVERLOAD_ADMIN_START_LINE + offset, admin_index):
        if not OVERLOAD_ATTRIBUTION_PATTERN.fullmatch(lines[line_index]):
            raise UserbotSignalParseError(
                "Cabecalho sem emojis contem metadado invalido "
                f"antes do ADM na linha {line_index + 1}"
            )


def _optional_overload_field(text: str, field: str) -> str | None:
    lines = text.splitlines()
    if field == "admin":
        line_index = _overload_admin_line_index(lines)
        if line_index is None:
            return None
    else:
        line_index = OVERLOAD_FIELD_LINES.get(field)
        if line_index is not None:
            line_index += _positional_offset(lines)

    if line_index is None:
        return None

    if line_index >= len(lines):
        return None

    line = lines[line_index]
    pattern = OVERLOAD_FIELD_PATTERNS.get(field)
    if pattern is not None:
        match = pattern.fullmatch(line)
        if not match:
            return None
        value = " ".join(match.group("value").strip().split())
    else:
        value = " ".join(line.strip().split())

    if not value:
        return None
    if field == "sport":
        return value if _looks_like_sport(value) else None
    if field in {"bookmaker", "event", "pick", "freebet"}:
        return value if _looks_like_overload_value(value) else None
    return value


def _overload_admin_line_index(lines: list[str]) -> int | None:
    pattern = OVERLOAD_FIELD_PATTERNS["admin"]
    for line_index in range(OVERLOAD_ADMIN_START_LINE + _positional_offset(lines), len(lines)):
        if pattern.fullmatch(lines[line_index]):
            return line_index
    return None


def _looks_like_overload_value(value: str) -> bool:
    """Evita aceitar rotulos ou marcadores posteriores como campo posicional."""

    normalized = value.casefold()
    blocked_fragments = (
        "sobrecarga",
        "adm:",
        "limite da aposta",
        "odd justa",
        "odd mudou",
        "clique aqui",
        "planilhar com shark track",
        "http://",
        "https://",
    )
    return any(char.isalnum() for char in value) and not any(
        fragment in normalized for fragment in blocked_fragments
    )


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


def _parse_brl_amount(value: str) -> float:
    """Converte o valor para reais.

    A bankroll e em reais: em "US$4,59 (R$27,00)" o valor usado e o R$ entre
    parenteses. Um valor somente em dolar e rejeitado para nao planilhar a
    stake na moeda errada.
    """

    brl = BRL_AMOUNT_PATTERN.search(value)
    if brl:
        return _parse_money(brl.group("value"))
    if "$" in value:
        raise UserbotSignalParseError(f"Stake sem valor em reais: {value}")
    return _parse_money(value)


def _normalize_limit(value: str) -> str:
    """Mantem so o limite em reais quando vier "US$x (R$y)"."""

    brl = BRL_AMOUNT_PATTERN.search(value)
    if brl and "(" in value:
        return f"R${brl.group('value')}"
    return value


def _parse_money(value: str) -> float:
    normalized = value.upper().replace("R$", "").replace(" ", "").strip()
    if "," in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError as exc:
        raise UserbotSignalParseError(f"Stake invalida: {value}") from exc
