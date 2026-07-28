from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bet_analytix_writer import BetAnalytixWriter
from discord_database import DiscordSignalStore
from message_parser import ParsedTelegramTip
from peixeesperto_result_sync import (
    _bookmaker_match_keys,
    _build_bookmaker_equivalence,
)
from userbot_signal_parser import _resolve_bookmaker_alias


class BookmakerResolutionTests(unittest.TestCase):
    def _writer(self, bookmakers: dict[str, int]) -> BetAnalytixWriter:
        writer = object.__new__(BetAnalytixWriter)
        writer._bookmakers_by_name = bookmakers
        writer._bookmaker_ids = set(bookmakers.values())
        return writer

    def test_exact_match_has_priority_over_bet_variant(self) -> None:
        writer = self._writer({"versus": 1, "versusbet": 2})

        self.assertEqual(writer._resolve_bookmaker_id("VERSUS.BET"), 2)

    def test_removes_bet_suffix_before_fuzzy(self) -> None:
        writer = self._writer({"versus": 1})

        for signal_name in ("VERSUSBET", "VERSUS BET", "VERSUS.BET"):
            with self.subTest(signal_name=signal_name):
                self.assertEqual(writer._resolve_bookmaker_id(signal_name), 1)

    def test_adds_bet_suffix_before_fuzzy(self) -> None:
        writer = self._writer({"versusbet": 2})

        for signal_name in ("versus", "Versus", "vErSuS", "VERSUS"):
            with self.subTest(signal_name=signal_name):
                self.assertEqual(writer._resolve_bookmaker_id(signal_name), 2)

    def test_alias_is_case_and_punctuation_insensitive(self) -> None:
        aliases = {"VERSUS.BET": "Versus"}

        for signal_name in ("versusbet", "Versus Bet", "vErSuS.bEt", "VERSUSBET"):
            with self.subTest(signal_name=signal_name):
                self.assertEqual(
                    _resolve_bookmaker_alias(signal_name, aliases),
                    "Versus",
                )

    def test_result_sync_accepts_alias_and_bet_variations(self) -> None:
        equivalence = _build_bookmaker_equivalence({"VERSUSBET": "Versus"})

        self.assertEqual(
            _bookmaker_match_keys("VERSUS.BET", equivalence),
            {"versus", "versusbet"},
        )
        self.assertEqual(
            _bookmaker_match_keys("Versus", equivalence),
            {"versus", "versusbet"},
        )


class LegacyBookmakerPersistenceTests(unittest.TestCase):
    def test_source_bookmaker_is_persisted_with_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "signals.sqlite3"
            store = DiscordSignalStore(database_path)
            store.initialize()
            tip = ParsedTelegramTip(
                tipster="Joao",
                event_datetime=datetime.now(timezone.utc),
                sport="Futebol",
                league=None,
                pick="Time A x Time B: Time A",
                odd=1.8,
                stake=100,
                bookmaker="Versus",
                source_bet_id=123,
                event="Time A x Time B",
            )

            inserted = store.enqueue_signal(
                guild_id=1,
                channel_id=2,
                message_id=3,
                signal_sender_id=4,
                reacting_user_id=5,
                tip=tip,
                raw_message="sinal",
                source_bookmaker_name="VERSUS.BET",
            )

            self.assertTrue(inserted)
            connection = sqlite3.connect(database_path)
            try:
                row = connection.execute(
                    "SELECT source_bookmaker_name FROM discord_signal_jobs"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row[0], "VERSUS.BET")


if __name__ == "__main__":
    unittest.main()
