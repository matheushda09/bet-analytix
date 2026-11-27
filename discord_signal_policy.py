"""Regras por usuario aplicadas aos sinais do Discord antes/depois de planilhar.

Tudo aqui e funcao pura (sem rede/Discord) para ser testavel:
- calculo da stake conforme o modo da instancia (fixo/percentual/multiplicador);
- deteccao de multiplas pelo texto do evento;
- filtros opcionais (casas permitidas, EV minimo);
- avisos para a DM (jogo iniciado, pick truncado, selecao repetida).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


STAKE_MODE_FIXED = "fixo"
STAKE_MODE_PERCENT = "percentual"
STAKE_MODE_MULTIPLIER = "multiplicador"
STAKE_MODES = {STAKE_MODE_FIXED, STAKE_MODE_PERCENT, STAKE_MODE_MULTIPLIER}

FILTER_MODE_WARN = "avisar"
FILTER_MODE_BLOCK = "bloquear"

# Picks cortados na origem (Discord/app do tipster) costumam parar perto de
# 100 caracteres, no meio de uma palavra ou selecao.
TRUNCATED_PICK_MIN_LENGTH = 95

_ACCUMULATOR_EVENT_NAMES = {"multipla", "multiplas", "varios", "varias", "combinada"}


@dataclass(frozen=True)
class StakeDecision:
    stake: float
    note: str | None = None


def normalize(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_marks).split())


def parse_percent(value: str | None) -> float | None:
    """'0,27%' -> 0.27 (em pontos percentuais)."""

    if not value:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", value)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def compute_stake(
    signal_stake: float,
    edge: str | None,
    mode: str,
    multiplier: float = 1.0,
    bankroll_capital: float | None = None,
) -> StakeDecision:
    """Stake final em reais para a instancia.

    - fixo: o R$ do sinal (comportamento original);
    - percentual: % da banca do sinal aplicado a banca do usuario;
    - multiplicador: R$ do sinal x fator.
    Se o modo percentual nao tiver dados, cai para o R$ do sinal e avisa.
    """

    if mode == STAKE_MODE_MULTIPLIER and multiplier > 0:
        stake = round(signal_stake * multiplier, 2)
        return StakeDecision(stake, f"stake do sinal R${signal_stake:.2f} x {multiplier:g}")
    if mode == STAKE_MODE_PERCENT:
        percent = parse_percent(edge)
        if percent is None or not bankroll_capital or bankroll_capital <= 0:
            return StakeDecision(
                round(signal_stake, 2),
                "modo percentual sem % ou banca disponivel; usada a stake do sinal",
            )
        stake = round(bankroll_capital * percent / 100, 2)
        return StakeDecision(stake, f"{percent:g}% da sua banca (R${bankroll_capital:,.2f})")
    return StakeDecision(round(signal_stake, 2))


def is_accumulator_event(event: str | None) -> bool:
    """Multipla de jogos diferentes: 'A x B / C x D', 'Multipla' ou 'Varios'."""

    text = str(event or "")
    if normalize(text) in _ACCUMULATOR_EVENT_NAMES:
        return True
    parts = [part for part in text.split("/") if part.strip()]
    return len(parts) >= 2 and sum(1 for part in parts if re.search(r"\sx\s", part, re.IGNORECASE)) >= 2


def is_probably_truncated_pick(pick: str | None) -> bool:
    return len(str(pick or "").strip()) >= TRUNCATED_PICK_MIN_LENGTH


def ev_percent(odd: float, fair_odd: str | float | None) -> float | None:
    try:
        fair = float(str(fair_odd).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if fair <= 1:
        return None
    return (odd / fair - 1) * 100


def filter_reasons(
    *,
    source_bookmaker: str,
    resolved_bookmaker: str,
    odd: float,
    fair_odd: str | None,
    allowed_bookmakers: tuple[str, ...],
    min_ev_percent: float | None,
) -> list[str]:
    """Motivos pelos quais o sinal nao passa nos filtros do usuario."""

    reasons: list[str] = []
    if allowed_bookmakers:
        allowed = {normalize(name).replace(" ", "") for name in allowed_bookmakers}
        candidates = {
            normalize(source_bookmaker).replace(" ", ""),
            normalize(resolved_bookmaker).replace(" ", ""),
        }
        if not candidates & allowed:
            reasons.append(f"casa {source_bookmaker} fora da sua lista de casas")
    if min_ev_percent is not None:
        ev = ev_percent(odd, fair_odd)
        if ev is not None and ev < min_ev_percent:
            reasons.append(f"EV {ev:.1f}% abaixo do minimo de {min_ev_percent:g}%")
    return reasons


def event_started(event_datetime: datetime | None, timezone_name: str, now: datetime | None = None) -> bool:
    """True se a data/hora informada no sinal ja passou."""

    if event_datetime is None:
        return False
    try:
        local_timezone = ZoneInfo(timezone_name)
    except Exception:
        local_timezone = ZoneInfo("America/Sao_Paulo")
    aware = event_datetime.replace(tzinfo=local_timezone) if event_datetime.tzinfo is None else event_datetime
    return aware <= (now or datetime.now(timezone.utc))


def selection_key(event: str | None, pick: str | None) -> str:
    """Chave para achar a mesma selecao postada de novo (convergentes/repostagens)."""

    return f"{normalize(event)}|{normalize(pick)}"
