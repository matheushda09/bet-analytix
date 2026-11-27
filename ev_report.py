"""Relatorio de EV esperado x resultado real por ADM (quem mandou o sinal)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from discord_signal_policy import ev_percent


_FAIR_ODD_PATTERN = re.compile(r"Odd\s+justa\s*:\s*(?P<value>\d+(?:[.,]\d+)?)", re.IGNORECASE)
_ADM_PATTERN = re.compile(r"ADM\s*:\s*(?P<value>[^\n\r]+)", re.IGNORECASE)
_SETTLED_STATES = {1, 2, 6}


@dataclass
class AdminStats:
    bets: int = 0
    stake: float = 0.0
    expected_profit: float = 0.0
    settled: int = 0
    settled_stake: float = 0.0
    settled_expected_profit: float = 0.0
    real_profit: float = 0.0
    without_fair_odd: int = 0
    names: set[str] = field(default_factory=set)


def build_ev_stats(rows: Iterable[Mapping[str, Any]]) -> dict[str, AdminStats]:
    stats: dict[str, AdminStats] = {}
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
            stake = float(payload["stake"])
            odd = float(payload["odd"])
        except (KeyError, TypeError, ValueError):
            continue
        admin = _admin_name(row)
        item = stats.setdefault(admin.casefold(), AdminStats())
        item.names.add(admin)
        item.bets += 1
        item.stake += stake

        fair = _fair_odd(payload, row)
        ev = ev_percent(odd, fair) if fair else None
        expected = stake * ev / 100 if ev is not None else 0.0
        if ev is None:
            item.without_fair_odd += 1
        item.expected_profit += expected

        state = row["bet_state"]
        if state is not None and int(state) in _SETTLED_STATES:
            item.settled += 1
            item.settled_stake += stake
            item.settled_expected_profit += expected
            item.real_profit += int(row["bet_profit_cents"] or 0) / 100
    return stats


def format_ev_report(stats: dict[str, AdminStats], days: int) -> str:
    if not stats:
        return f"📊 Nenhuma aposta planilhada pelo Discord nos últimos {days} dias."

    lines = [f"📊 **EV esperado x real — últimos {days} dias** (por ADM)"]
    ordered = sorted(stats.values(), key=lambda item: item.stake, reverse=True)
    for item in ordered:
        name = sorted(item.names)[0]
        ev_pct = item.expected_profit / item.stake * 100 if item.stake else 0.0
        line = (
            f"\n**{name}** — {item.bets} apostas, R${item.stake:,.2f} apostados\n"
            f"EV esperado: R${item.expected_profit:+,.2f} ({ev_pct:+.1f}%)"
        )
        if item.settled:
            real_pct = item.real_profit / item.settled_stake * 100 if item.settled_stake else 0.0
            line += (
                f"\nResolvidas: {item.settled} — real R${item.real_profit:+,.2f} ({real_pct:+.1f}%) "
                f"vs esperado R${item.settled_expected_profit:+,.2f}"
            )
        if item.without_fair_odd:
            line += f"\n({item.without_fair_odd} sem odd justa, fora do EV)"
        lines.append(line)
    lines.append("\nResultado real depende da sincronização de greens/reds da banca.")
    return "\n".join(lines)


def _admin_name(row: Mapping[str, Any]) -> str:
    name = row["source_tipster_name"] if "source_tipster_name" in row.keys() else None
    if not name and row["raw_message"]:
        match = _ADM_PATTERN.search(str(row["raw_message"]))
        if match:
            name = match.group("value").strip()
    return str(name or "Sem ADM").strip()


def _fair_odd(payload: Mapping[str, Any], row: Mapping[str, Any]) -> str | None:
    for text in (payload.get("extra_note"), row["raw_message"]):
        if text:
            match = _FAIR_ODD_PATTERN.search(str(text))
            if match:
                return match.group("value")
    return None
