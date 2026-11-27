from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bet_analytix_writer import (
    BetAnalytixWriter,
    BetAnalytixWriterError,
    _extract_bookmaker_entries,
)
from discord_database import DiscordSignalStore
from message_parser import ParsedTelegramTip
from stake_reply import BRL, USD, StakeReply, StakeReplyError, parse_stake_reply, stake_in_brl


class StakeReplyParsingTests(unittest.TestCase):
    def test_accepts_tolerant_formats(self) -> None:
        cases = {
            "R$43": (43.0, BRL),
            "R$          43": (43.0, BRL),
            "R 43": (43.0, BRL),
            "r41": (41.0, BRL),
            "41r": (41.0, BRL),
            "41 reais": (41.0, BRL),
            "43": (43.0, BRL),
            "43,50": (43.5, BRL),
            "R$1.250,00": (1250.0, BRL),
            "**R$43**": (43.0, BRL),
            "$ 41": (41.0, USD),
            "41$": (41.0, USD),
            "$41": (41.0, USD),
            "US$ 8,50": (8.5, USD),
            "usd 10": (10.0, USD),
            "8 dólares": (8.0, USD),
        }
        for text, (amount, currency) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_stake_reply(text), StakeReply(amount, currency))

    def test_rejects_ambiguous_replies(self) -> None:
        for text in ("oi", "43 50", "R$0", "$41 reais", "valeu, 43", ""):
            with self.subTest(text=text):
                with self.assertRaises(StakeReplyError):
                    parse_stake_reply(text)

    def test_dollar_uses_signal_rate_first(self) -> None:
        provider = mock.Mock()
        provider.get_rate.return_value = 5.0

        stake, rate = stake_in_brl(StakeReply(8.5, USD), 5.8824, provider)

        self.assertEqual(stake, 50.0)
        self.assertEqual(rate, 5.8824)
        provider.get_rate.assert_not_called()

    def test_dollar_falls_back_to_provider_and_reais_skip_conversion(self) -> None:
        provider = mock.Mock()
        provider.get_rate.return_value = 5.5

        self.assertEqual(stake_in_brl(StakeReply(10, USD), None, provider), (55.0, 5.5))
        self.assertEqual(stake_in_brl(StakeReply(43, BRL), 5.88, provider), (43.0, None))

    def test_dollar_without_any_rate_is_rejected(self) -> None:
        provider = mock.Mock()
        provider.get_rate.return_value = None

        with self.assertRaises(StakeReplyError):
            stake_in_brl(StakeReply(10, USD), None, provider)


class BookmakerCatalogTests(unittest.TestCase):
    def test_extracts_account_bookmakers_from_all_data_shapes(self) -> None:
        all_data = {
            "tipsters": [{"id": 1, "name": "PeixeEsperto"}],
            "bookmakers": [{"id": 900, "name": "99play"}],
            "userBookmakers": [
                {"id": 5, "id_bookmaker": 901, "name": "CopaWin"},
                {"id": 6, "bookmaker": {"id": 902, "name": "Bingo em Casa"}},
            ],
            "settings": {"customBookmakers": [{"bookmaker_id": 903, "bookmaker_name": "Finta"}]},
            "bookmakerAccounts": [{"id": 12, "bookmaker": 904, "name": "LowVig"}],
        }

        entries = _extract_bookmaker_entries(all_data, only_bookmaker_keys=True)

        self.assertEqual(
            sorted(entries),
            [(900, "99play"), (901, "CopaWin"), (902, "Bingo em Casa"), (903, "Finta"), (904, "LowVig")],
        )

    def _writer(self, public: list[dict], account: dict) -> BetAnalytixWriter:
        writer = object.__new__(BetAnalytixWriter)
        writer._settings = SimpleNamespace(bet_analytix_email="e", bet_analytix_password="p")
        writer._access_token = "token"
        writer._bookmakers_by_name = None
        writer._bookmaker_ids = None
        writer._bookmakers_loaded_at = 0.0
        writer._destination_user_id = 77
        calls: list[str] = []

        def fake_request(method, path, json_payload=None, authenticated=False, **_kwargs):
            calls.append(path)
            if path == "/bookmakers":
                return public
            if path == "/bankroll/all-data/77":
                return account
            raise AssertionError(path)

        writer._request_json = fake_request
        writer.calls = calls
        return writer

    def test_account_bookmakers_resolve_exact_names(self) -> None:
        writer = self._writer(
            public=[{"id": 1, "name": "Bet365"}, {"id": 2, "name": "Stake"}],
            account={"bookmakers": [{"id": 900, "name": "99play"}, {"id": 901, "name": "CopaWin"}]},
        )

        self.assertEqual(writer._resolve_bookmaker_id("99play"), 900)
        self.assertEqual(writer._resolve_bookmaker_id("COPAWIN"), 901)
        self.assertEqual(writer._resolve_bookmaker_id("Copa Win"), 901)
        self.assertEqual(writer._resolve_bookmaker_id("Stake.com"), 2)

    def test_missing_bookmaker_reloads_catalog_before_failing(self) -> None:
        account = {"bookmakers": []}
        writer = self._writer(public=[{"id": 1, "name": "Bet365"}], account=account)
        writer._load_bookmakers()
        writer._bookmakers_loaded_at -= 3600
        account["bookmakers"].append({"id": 900, "name": "99play"})

        self.assertEqual(writer._resolve_bookmaker_id("99play"), 900)
        self.assertEqual(writer.calls.count("/bookmakers"), 2)

    def test_public_catalog_wins_name_collisions(self) -> None:
        writer = self._writer(
            public=[{"id": 2, "name": "Stake"}],
            account={"bookmakers": [{"id": 50, "name": "Stake"}]},
        )

        self.assertEqual(writer._resolve_bookmaker_id("Stake"), 2)

    def test_unknown_bookmaker_gets_best_effort_match_flagged(self) -> None:
        writer = self._writer(
            public=[{"id": 1, "name": "Bet365"}, {"id": 2, "name": "Stake"}, {"id": 3, "name": "Superbet"}],
            account={},
        )

        match = writer.match_bookmaker("Super Bet Brasil")

        self.assertEqual(match.bookmaker_id, 3)
        self.assertTrue(match.is_approximate)
        self.assertIs(writer.last_bookmaker_match("Super Bet Brasil"), match)

    def test_exact_match_is_not_flagged_and_keeps_display_name(self) -> None:
        writer = self._writer(public=[{"id": 1135, "name": "99Play"}], account={})

        match = writer.match_bookmaker("99play")

        self.assertEqual((match.bookmaker_id, match.bookmaker_name, match.is_approximate), (1135, "99Play", False))

    def test_domain_names_match_exactly(self) -> None:
        writer = self._writer(
            public=[{"id": 2, "name": "Stake"}, {"id": 7, "name": "Jogajunto"}, {"id": 1135, "name": "99play"}],
            account={},
        )
        for name, expected in (("Stake.com", 2), ("jogajunto.bet.br", 7), ("www.99play.me", 1135)):
            with self.subTest(name=name):
                match = writer.match_bookmaker(name)
                self.assertEqual(match.bookmaker_id, expected)
                self.assertFalse(match.is_approximate)

    def test_new_bookmaker_added_after_start_is_found(self) -> None:
        public = [{"id": 1, "name": "Bet365"}]
        writer = self._writer(public=public, account={})
        writer._load_bookmakers()
        writer._bookmakers_loaded_at -= 120
        public.append({"id": 1135, "name": "99play"})

        match = writer.match_bookmaker("99play")

        self.assertEqual(match.bookmaker_id, 1135)
        self.assertFalse(match.is_approximate)

    def test_periodic_refresh_without_miss(self) -> None:
        public = [{"id": 1, "name": "Bet365"}]
        writer = self._writer(public=public, account={})
        writer._load_bookmakers()
        writer._bookmakers_loaded_at -= 7 * 3600

        writer.match_bookmaker("Bet365")

        self.assertEqual(writer.calls.count("/bookmakers"), 2)

    def test_empty_catalog_still_raises(self) -> None:
        writer = self._writer(public=[], account={})

        with self.assertRaises(BetAnalytixWriterError):
            writer._resolve_bookmaker_id("Casa Inexistente")

    def test_explicit_id_is_accepted_outside_catalog(self) -> None:
        writer = self._writer(public=[{"id": 1, "name": "Bet365"}], account={})

        self.assertEqual(writer._resolve_bookmaker_id("id 4321"), 4321)

    def test_account_failure_keeps_public_catalog(self) -> None:
        writer = self._writer(public=[{"id": 1, "name": "Bet365"}], account={})
        original = writer._request_json

        def failing(method, path, **kwargs):
            if "all-data" in path:
                raise BetAnalytixWriterError("HTTP 500")
            return original(method, path, **kwargs)

        writer._request_json = failing
        self.assertEqual(writer._resolve_bookmaker_id("bet365"), 1)


class CompetitionFallbackTests(unittest.TestCase):
    def test_retries_without_competition_on_client_error(self) -> None:
        writer = object.__new__(BetAnalytixWriter)
        payloads: list[dict] = []

        def fake_request(method, path, json_payload=None, authenticated=False, **_kwargs):
            payloads.append(json_payload)
            if json_payload["selections"][0]["competition"]:
                raise BetAnalytixWriterError('Bet-Analytix retornou HTTP 400: {"error":"competition"}')
            return [{"id": 10}]

        writer._request_json = fake_request
        result = writer._post_bet({"selections": [{"label": "x", "competition": "Liga Europa"}]})

        self.assertEqual(result, [{"id": 10}])
        self.assertEqual(payloads[0]["selections"][0]["competition"], "Liga Europa")
        self.assertIsNone(payloads[1]["selections"][0]["competition"])


class StakePromptStoreTests(unittest.TestCase):
    def test_prompt_roundtrip_and_adjustment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DiscordSignalStore(Path(temp_dir) / "signals.sqlite3")
            store.initialize()
            tip = ParsedTelegramTip(
                tipster="PeixeEsperto",
                event_datetime=datetime.now(timezone.utc),
                sport="Futebol",
                league="Liga Europa",
                pick="A x B: A",
                odd=2.0,
                stake=27,
                bookmaker="99play",
                source_bet_id=123,
                event="A x B",
            )
            store.enqueue_signal(1, 2, 3, 4, 5, tip, raw_message="sinal")
            job_id = store.get_job_by_message(1, 2, 3)["id"]

            store.record_stake_prompt(999, job_id, 123, 555)
            prompt = store.get_stake_prompt(999)
            self.assertEqual(prompt["bet_analytix_bet_id"], 555)
            self.assertEqual(prompt["raw_message"], "sinal")
            self.assertIsNone(store.get_stake_prompt(1000))

            store.record_stake_adjustment(999, job_id, 43.0)
            self.assertEqual(store.get_stake_prompt(999)["adjusted_stake"], 43.0)
            connection = sqlite3.connect(Path(temp_dir) / "signals.sqlite3")
            try:
                payload = json.loads(
                    connection.execute("SELECT payload_json FROM discord_signal_jobs").fetchone()[0]
                )
            finally:
                connection.close()
            self.assertEqual(payload["stake"], 43.0)


class StakeReplyHandlerTests(unittest.TestCase):
    def _client(self):
        from discord_reaction_bot import DiscordSignalClient

        client = object.__new__(DiscordSignalClient)
        client._writer = mock.Mock()
        client._store = mock.Mock()
        client._alerter = None
        client._usd_brl_rate_provider = mock.Mock()
        client._usd_brl_rate_provider.get_rate.return_value = 5.0
        return client

    def _message(self, content: str):
        message = mock.Mock()
        message.content = content
        message.reply = mock.AsyncMock()
        return message

    def test_dollar_reply_uses_rate_from_original_signal(self) -> None:
        from tests.test_signal_parser_usd_layouts import FISHBOT_USD_SIGNAL

        client = self._client()
        message = self._message("$ 8,50")
        prompt = {"bet_analytix_bet_id": 555, "raw_message": FISHBOT_USD_SIGNAL, "dm_message_id": 999, "job_id": 1, "source_bet_id": 123, "cancelled_at_ts": None}

        asyncio.run(client._handle_stake_reply(message, prompt))

        client._writer.update_bet_stake.assert_called_once_with(555, 50.0)
        client._store.record_stake_adjustment.assert_called_once_with(999, 1, 50.0)
        self.assertIn("R$50,00", message.reply.call_args.args[0])

    def test_invalid_reply_does_not_touch_bet(self) -> None:
        client = self._client()
        message = self._message("valeu")
        prompt = {"bet_analytix_bet_id": 555, "raw_message": None, "dm_message_id": 999, "job_id": 1, "source_bet_id": 123, "cancelled_at_ts": None}

        asyncio.run(client._handle_stake_reply(message, prompt))

        client._writer.update_bet_stake.assert_not_called()
        self.assertIn("Não entendi", message.reply.call_args.args[0])


if __name__ == "__main__":
    unittest.main()


class CancelBetTests(unittest.TestCase):
    def _writer(self, raw_bet, delete_error=None, still_exists=False):
        writer = object.__new__(BetAnalytixWriter)
        writer._settings = SimpleNamespace(copytrade_bankroll_internal_id=3, copytrade_bankroll_id=1829516)
        writer._access_token = "token"
        calls: list[tuple[str, str]] = []
        state = {"deleted": False, "payload": None}

        def fake_request(method, path, json_payload=None, authenticated=False, **kwargs):
            writer._assert_non_destructive_request(
                method,
                path,
                allow_existing_bet_read=kwargs.get("allow_existing_bet_read", False),
                allow_existing_bet_update=kwargs.get("allow_existing_bet_update", False),
                allow_existing_bet_delete=kwargs.get("allow_existing_bet_delete", False),
            )
            calls.append((method, path))
            if method == "DELETE":
                if delete_error:
                    raise BetAnalytixWriterError(delete_error)
                state["deleted"] = True
                state["payload"] = json_payload
                return None
            if method == "GET" and state["deleted"] and not still_exists:
                raise BetAnalytixWriterError("Bet-Analytix retornou HTTP 404: not found")
            return raw_bet

        writer._request_json = fake_request
        writer.calls = calls
        writer.delete_state = state
        return writer

    def test_deletes_pending_bet_of_this_bankroll(self) -> None:
        writer = self._writer({"state": 0, "bankroll": 3})
        writer.delete_pending_bet(555)
        self.assertIn(("DELETE", "/bet/delete"), writer.calls)
        self.assertEqual(writer.delete_state["payload"], {"bets": [555], "bankroll": 1829516})

    def test_refuses_settled_or_foreign_bets(self) -> None:
        for raw in ({"state": 1, "bankroll": 3}, {"state": 0, "bankroll": 99}):
            with self.subTest(raw=raw):
                writer = self._writer(raw)
                with self.assertRaises(BetAnalytixWriterError):
                    writer.delete_pending_bet(555)
                self.assertNotIn(("DELETE", "/bet/delete"), writer.calls)

    def test_delete_still_blocked_without_explicit_flag(self) -> None:
        writer = object.__new__(BetAnalytixWriter)
        with self.assertRaises(BetAnalytixWriterError):
            writer._assert_non_destructive_request("DELETE", "/bet/delete")
        with self.assertRaises(BetAnalytixWriterError):
            writer._assert_non_destructive_request("DELETE", "/bet/555", allow_existing_bet_delete=True)
        with self.assertRaises(BetAnalytixWriterError):
            writer._assert_non_destructive_request("DELETE", "/bankroll/1", allow_existing_bet_delete=True)

    def test_store_cancel_frees_message_for_new_reaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DiscordSignalStore(Path(temp_dir) / "signals.sqlite3")
            store.initialize()
            tip = ParsedTelegramTip(
                tipster="PeixeEsperto",
                event_datetime=datetime.now(timezone.utc),
                sport="Futebol",
                league=None,
                pick="A x B: A",
                odd=2.0,
                stake=27,
                bookmaker="99play",
                source_bet_id=123,
                event="A x B",
            )
            store.enqueue_signal(1, 2, 3, 4, 5, tip, raw_message="sinal")
            job_id = store.get_job_by_message(1, 2, 3)["id"]
            store.record_stake_prompt(999, job_id, 123, 555)

            store.cancel_planilhada_bet(999, job_id)

            self.assertIsNotNone(store.get_stake_prompt(999)["cancelled_at_ts"])
            self.assertIsNone(store.get_job_by_message(1, 2, 3))
            self.assertTrue(store.enqueue_signal(1, 2, 3, 4, 5, tip, raw_message="sinal"))

    def test_dm_cancel_reaction_only_from_admin(self) -> None:
        from discord_reaction_bot import DiscordSignalClient

        client = object.__new__(DiscordSignalClient)
        client._settings = SimpleNamespace(admin_user_id=42)
        client._connection = mock.Mock(user=SimpleNamespace(id=7))
        client._writer = mock.Mock()
        client._store = mock.Mock()
        client._store.get_stake_prompt.return_value = {
            "bet_analytix_bet_id": 555,
            "dm_message_id": 999,
            "job_id": 1,
            "source_bet_id": 123,
            "cancelled_at_ts": None,
        }
        channel = mock.Mock()
        channel.fetch_message = mock.AsyncMock(return_value=mock.Mock(reply=mock.AsyncMock()))
        client.get_channel = mock.Mock(return_value=channel)

        bot_payload = SimpleNamespace(user_id=7, emoji="❌", message_id=999, channel_id=5)
        asyncio.run(client._handle_dm_reaction(bot_payload))
        client._writer.delete_pending_bet.assert_not_called()

        admin_payload = SimpleNamespace(user_id=42, emoji="❌", message_id=999, channel_id=5)
        asyncio.run(client._handle_dm_reaction(admin_payload))
        client._writer.delete_pending_bet.assert_called_once_with(555)
        client._store.cancel_planilhada_bet.assert_called_once_with(999, 1)


class CompetitionResolutionTests(unittest.TestCase):
    def _writer(self, competitions, create_fails=False):
        writer = object.__new__(BetAnalytixWriter)
        writer._destination_user_id = 690942
        calls: list[tuple[str, str, object]] = []

        def fake_request(method, path, json_payload=None, authenticated=False, **_kwargs):
            calls.append((method, path, json_payload))
            if method == "GET" and path == "/bankroll/all-data/690942":
                return {"tipsters": [], "competitions": list(competitions)}
            if method == "POST" and path == "/competition":
                if create_fails:
                    raise BetAnalytixWriterError("Bet-Analytix retornou HTTP 404: not found")
                competitions.append({"id": 900, "label": json_payload["label"], "sport": json_payload["sport"]})
                return {"id": 900}
            raise AssertionError((method, path))

        writer._request_json = fake_request
        writer.calls = calls
        return writer

    def test_uses_existing_competition_id(self) -> None:
        writer = self._writer([{"id": 231365, "label": "Brasileirão", "sport": 1, "deleted_at": None}])
        self.assertEqual(writer._resolve_competition_id_safely("Brasileirao", 1), 231365)
        self.assertFalse(any(method == "POST" for method, _, _ in writer.calls))

    def test_creates_missing_competition_once(self) -> None:
        writer = self._writer([])
        self.assertEqual(writer._resolve_competition_id_safely("Liga Europa", 1), 900)
        self.assertEqual(writer._resolve_competition_id_safely("Liga Europa", 1), 900)
        posts = [payload for method, _, payload in writer.calls if method == "POST"]
        self.assertEqual(posts, [{"label": "Liga Europa", "sport": 1, "country": None}])

    def test_failure_returns_none_and_is_not_retried(self) -> None:
        writer = self._writer([], create_fails=True)
        self.assertIsNone(writer._resolve_competition_id_safely("NFL", 1))
        self.assertIsNone(writer._resolve_competition_id_safely("NFL", 1))
        posts = [payload for method, _, payload in writer.calls if method == "POST"]
        self.assertEqual(len(posts), 1)

    def test_empty_league(self) -> None:
        writer = self._writer([])
        self.assertIsNone(writer._resolve_competition_id_safely(None, 1))
        self.assertEqual(writer.calls, [])
