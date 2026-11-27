from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from discord_signal_policy import (
    compute_stake,
    event_started,
    filter_reasons,
    is_accumulator_event,
    is_probably_truncated_pick,
    selection_key,
)
from ev_report import build_ev_stats, format_ev_report
from stake_reply import StakeReplyError, parse_stake_reply
from userbot_signal_parser import ODD_CHANGED_MARKER_PATTERN, parse_external_signal


TAIL = (
    "\n\nhttps://wncopa.com/\n\nOdd justa: 8.714\n\n"
    "📊 Odd mudou? [Clique AQUI](https://calc.peixeesperto.com.br/?justa=8.714) e calcule quanto vale."
)

OVERLOAD_LEAGUE_AFTER_ADMIN = (
    "SOBRECARGA\n\nCopaWin\n"
    "Olympiacos Piraeus x AC Milan / Din. Zagreb x Bayer Leverkusen / Celta Vigo x Bournemouth\n"
    "Futebol\nAC Milan - Resultado final / Bayer Leverkusen - Resultado final / Bournemouth - Resultado final\n"
    "9.23\nLimite da aposta: R$1.000,00\n0,18%\nR$18,00\nNão\n\nADM: Peixe\nEuropa League" + TAIL
)

OVERLOAD_COMMENT_AFTER_LINK = (
    "SOBRECARGA\n\nStake.com\nNew York Knicks x Miami Heat\nBasquete\n"
    "Miami Heat - Vencedor (incluindo prorrogação)\n3.09\nLimite da aposta: R$114,00\n3,23%\nR$114,00\nNão\n\n"
    "ADM: Matheus Pulga\n\nhttps://stake.com/pt/sports/basketball\n\nQuem preferir ir no Heat ai" + TAIL
)


class StakeModeTests(unittest.TestCase):
    def test_fixed_keeps_signal_stake(self) -> None:
        decision = compute_stake(27.0, "0,27%", "fixo")
        self.assertEqual(decision.stake, 27.0)
        self.assertIsNone(decision.note)

    def test_percent_uses_user_bankroll(self) -> None:
        decision = compute_stake(27.0, "0,27%", "percentual", bankroll_capital=2000.0)
        self.assertEqual(decision.stake, 5.4)
        self.assertIn("0.27%", decision.note)

    def test_percent_without_data_falls_back_to_signal(self) -> None:
        self.assertEqual(compute_stake(27.0, "0,27%", "percentual", bankroll_capital=None).stake, 27.0)
        self.assertEqual(compute_stake(27.0, None, "percentual", bankroll_capital=5000).stake, 27.0)

    def test_multiplier(self) -> None:
        self.assertEqual(compute_stake(27.0, "0,27%", "multiplicador", multiplier=0.5).stake, 13.5)


class AccumulatorAndPickTests(unittest.TestCase):
    def test_accumulator_events(self) -> None:
        for event in (
            "Borac Banja Luka x Atalanta / Torreense x Celtic / Freiburg x Twente",
            "Ajax x Thun / Salzburg x Ararat-Armenia",
            "Múltipla",
            "Vários",
        ):
            with self.subTest(event=event):
                self.assertTrue(is_accumulator_event(event))

    def test_single_events(self) -> None:
        for event in ("Olympiacos x Milan", "DAL Cowboys x PHI Eagles", "Lyon x Lillestrom SC"):
            with self.subTest(event=event):
                self.assertFalse(is_accumulator_event(event))

    def test_truncated_pick(self) -> None:
        self.assertTrue(
            is_probably_truncated_pick(
                "Atalanta - Resultado final / Celtic - Resultado final / Freiburg - Resultado final / Crystal Palace"
            )
        )
        self.assertFalse(is_probably_truncated_pick("Total de escanteios - Menos de 8,5"))

    def test_selection_key_ignores_case_and_accents(self) -> None:
        self.assertEqual(
            selection_key("FC Iberia 1999 x Getafe", "1º tempo - total - Menos de 1"),
            selection_key("fc iberia 1999 x getafe", "1º Tempo - Total - menos de 1"),
        )


class FilterTests(unittest.TestCase):
    def test_no_filters_configured(self) -> None:
        self.assertEqual(
            filter_reasons(
                source_bookmaker="99play",
                resolved_bookmaker="99play",
                odd=2.0,
                fair_odd="1.9",
                allowed_bookmakers=(),
                min_ev_percent=None,
            ),
            [],
        )

    def test_bookmaker_and_ev_filters(self) -> None:
        reasons = filter_reasons(
            source_bookmaker="Stake.com",
            resolved_bookmaker="Stake.com",
            odd=2.0,
            fair_odd="1.98",
            allowed_bookmakers=("99play", "Bet365"),
            min_ev_percent=3,
        )
        self.assertEqual(len(reasons), 2)

        self.assertEqual(
            filter_reasons(
                source_bookmaker="Stake.com",
                resolved_bookmaker="Stake",
                odd=2.0,
                fair_odd="1.8",
                allowed_bookmakers=("stake",),
                min_ev_percent=3,
            ),
            [],
        )

    def test_event_started(self) -> None:
        now = datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc)  # 15:00 em Sao Paulo
        self.assertTrue(event_started(datetime(2026, 11, 26, 14, 45), "America/Sao_Paulo", now))
        self.assertFalse(event_started(datetime(2026, 11, 26, 17, 0), "America/Sao_Paulo", now))
        self.assertFalse(event_started(None, "America/Sao_Paulo", now))


class ParserExtraTests(unittest.TestCase):
    def test_overload_league_right_after_admin(self) -> None:
        signal = parse_external_signal(OVERLOAD_LEAGUE_AFTER_ADMIN, signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN)
        self.assertEqual(signal.league, "Europa League")
        self.assertEqual(signal.stake, 18.0)

    def test_overload_link_or_comment_is_not_league(self) -> None:
        signal = parse_external_signal(OVERLOAD_COMMENT_AFTER_LINK, signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN)
        self.assertIsNone(signal.league)

    def test_reply_with_odd(self) -> None:
        reply = parse_stake_reply("R$43 @2.05")
        self.assertEqual((reply.amount, reply.currency, reply.odd), (43.0, "BRL", 2.05))
        self.assertEqual(parse_stake_reply("$8 @ 1,95").odd, 1.95)
        with self.assertRaises(StakeReplyError):
            parse_stake_reply("43 @0.5")


class EvReportTests(unittest.TestCase):
    def _row(self, admin, stake, odd, fair, state=None, profit_cents=0):
        return {
            "payload_json": json.dumps({"stake": stake, "odd": odd, "extra_note": f"Odd justa: {fair}"}),
            "raw_message": "",
            "source_tipster_name": admin,
            "bet_state": state,
            "bet_profit_cents": profit_cents,
        }

    def test_groups_by_admin(self) -> None:
        rows = [
            self._row("FishBot", 100, 2.1, "2.0", state=1, profit_cents=11000),
            self._row("FishBot", 100, 2.1, "2.0", state=2, profit_cents=-10000),
            self._row("Julio Bucci", 50, 1.9, "1.8"),
        ]
        stats = build_ev_stats(rows)

        self.assertAlmostEqual(stats["fishbot"].expected_profit, 10.0)
        self.assertEqual(stats["fishbot"].settled, 2)
        self.assertAlmostEqual(stats["fishbot"].real_profit, 10.0)
        self.assertEqual(stats["julio bucci"].settled, 0)
        text = format_ev_report(stats, 30)
        self.assertIn("FishBot", text)
        self.assertIn("Julio Bucci", text)

    def test_empty_report(self) -> None:
        self.assertIn("Nenhuma aposta", format_ev_report({}, 7))


class ReactionPolicyIntegrationTests(unittest.TestCase):
    def test_dm_warns_repeated_selection(self) -> None:
        from discord_reaction_bot import DiscordSignalClient
        from message_parser import ParsedTelegramTip

        client = object.__new__(DiscordSignalClient)
        client._base_settings = mock.Mock(timezone="America/Sao_Paulo")
        client._store = mock.Mock()
        tip = ParsedTelegramTip(
            tipster="PeixeEsperto",
            event_datetime=datetime(2026, 11, 26, 14, 45),
            sport="Futebol",
            league="Liga Conferência",
            pick="FC Iberia 1999 x Getafe: 1º tempo - total - Menos de 1",
            odd=1.82,
            stake=49.0,
            bookmaker="99play",
            source_bet_id=3,
            event="FC Iberia 1999 x Getafe",
        )
        previous = {"id": 1, "payload_json": json.dumps({"event": tip.event, "pick": tip.pick, "stake": 28.0, "odd": 1.9})}
        current = {"id": 2, "payload_json": json.dumps({"event": tip.event, "pick": tip.pick, "stake": 49.0, "odd": 1.82})}
        client._store.list_done_jobs_since.return_value = [previous, current]

        warnings = client._dm_warnings(tip, job_id=2, raw_message="")

        self.assertEqual(len(warnings), 1)
        self.assertIn("1x", warnings[0])
        self.assertIn("R$77,00", warnings[0])


if __name__ == "__main__":
    unittest.main()
