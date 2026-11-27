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

    def test_unknown_bookmaker_still_raises(self) -> None:
        writer = self._writer(public=[{"id": 1, "name": "Bet365"}], account={})

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
        prompt = {"bet_analytix_bet_id": 555, "raw_message": FISHBOT_USD_SIGNAL, "dm_message_id": 999, "job_id": 1, "source_bet_id": 123}

        asyncio.run(client._handle_stake_reply(message, prompt))

        client._writer.update_bet_stake.assert_called_once_with(555, 50.0)
        client._store.record_stake_adjustment.assert_called_once_with(999, 1, 50.0)
        self.assertIn("R$50,00", message.reply.call_args.args[0])

    def test_invalid_reply_does_not_touch_bet(self) -> None:
        client = self._client()
        message = self._message("valeu")
        prompt = {"bet_analytix_bet_id": 555, "raw_message": None, "dm_message_id": 999, "job_id": 1, "source_bet_id": 123}

        asyncio.run(client._handle_stake_reply(message, prompt))

        client._writer.update_bet_stake.assert_not_called()
        self.assertIn("Não entendi", message.reply.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
