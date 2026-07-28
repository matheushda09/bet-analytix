from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from bet_analytix_writer import BetAnalytixWriter
from discord_database import DiscordSignalStore
from message_parser import ParsedTelegramTip
from peixeesperto_result_sync import (
    PeixeEspertoBet,
    PeixeEspertoResultSync,
    _LocalCandidate,
    _bookmaker_match_keys,
    _build_bookmaker_equivalence,
    _narrow_by_odd_and_stake,
    _parse_datetime,
    _source_tipster_from_job,
    _tipsters_compatible,
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
                source_tipster_name="xSuarez",
            )

            self.assertTrue(inserted)
            connection = sqlite3.connect(database_path)
            try:
                row = connection.execute(
                    "SELECT source_bookmaker_name, source_tipster_name FROM discord_signal_jobs"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row[0], "VERSUS.BET")
            self.assertEqual(row[1], "xSuarez")

    def test_source_tipster_falls_back_to_legacy_raw_message(self) -> None:
        job = {
            "source_tipster_name": None,
            "raw_message": "🔮\n👨‍💻 ADM: xSuarez\n\nOdd justa: 1.80",
        }

        self.assertEqual(_source_tipster_from_job(job), "xSuarez")


class ResultMatchingSafetyTests(unittest.TestCase):
    def _candidate(self, job_id: int, odd: float, stake: float) -> _LocalCandidate:
        return _LocalCandidate(
            job_id=job_id,
            source_bet_id=job_id,
            bet_analytix_bet_id=job_id,
            tip=None,
            odd=odd,
            stake=stake,
            event_norm="evento",
            pick_norm="pick",
            bookmaker_norm="versus",
            source_bookmaker="VERSUSBET",
            source_tipster="xSuarez",
        )

    def test_tipster_comparison_is_case_and_at_sign_insensitive(self) -> None:
        self.assertTrue(_tipsters_compatible("@XSUAREZ", "xSuarez"))
        self.assertFalse(_tipsters_compatible("xSuarez", "Peixe"))

    def test_api_datetime_uses_configured_timezone(self) -> None:
        parsed = _parse_datetime("2026-07-28 01:00:00", "America/Sao_Paulo")

        self.assertEqual(parsed.utcoffset().total_seconds(), -3 * 3600)
        self.assertEqual(parsed.astimezone(timezone.utc).hour, 4)

    def test_odd_and_stake_only_resolve_unique_candidate(self) -> None:
        peixe_bet = PeixeEspertoBet(
            message_id=10,
            event="Evento",
            pick="Pick",
            bookmaker="Versus",
            tipster="xSuarez",
            sport="Futebol",
            odd=1.88,
            stake=100.0,
            profit=88.0,
            estado="Ganha",
            raw={},
        )
        matches = [
            self._candidate(1, 1.883, 100.0),
            self._candidate(2, 1.90, 100.0),
        ]

        narrowed = _narrow_by_odd_and_stake(matches, peixe_bet)

        self.assertEqual([candidate.job_id for candidate in narrowed], [1])

    def test_api_query_is_filtered_and_synced_page_does_not_stop_pagination(self) -> None:
        sync = object.__new__(PeixeEspertoResultSync)
        sync._settings = SimpleNamespace(
            sync_max_age_hours=72,
            sync_max_pages=10,
            sync_per_page=50,
            group_slug="aguas-profundas",
        )
        sync._store = SimpleNamespace(get_synced_message_ids=lambda: {100})
        requested_urls: list[str] = []

        def fake_get(url: str) -> dict:
            requested_urls.append(url)
            page = int(parse_qs(urlparse(url).query)["page"][0])
            message_id = 100 if page == 1 else 101
            return {
                "apostas": [
                    {
                        "message_id": message_id,
                        "Jogo": "Evento",
                        "Aposta": "Pick",
                        "Casa": "Versus",
                        "Tipster": "xSuarez",
                        "Esporte": "Futebol",
                        "Odd": 1.88,
                        "Valor": 100,
                        "Lucro": 88,
                        "Estado": "Ganha",
                        "Data": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                ],
                "current_page": page,
                "pages": 2,
            }

        sync._get_with_retry = fake_get

        results = sync._fetch_recent_results({"xSuarez"})

        self.assertEqual([result.message_id for result in results], [101])
        self.assertEqual(len(requested_urls), 2)
        query = parse_qs(urlparse(requested_urls[0]).query)
        self.assertEqual(query["tipster"], ["xSuarez"])
        self.assertIn("Ganha", query["estado"][0])

    def test_unknown_legacy_jobs_keep_unfiltered_api_fallback(self) -> None:
        sync = object.__new__(PeixeEspertoResultSync)
        sync._settings = SimpleNamespace(
            sync_max_age_hours=72,
            sync_max_pages=1,
            sync_per_page=50,
            group_slug="aguas-profundas",
            timezone_name="America/Sao_Paulo",
        )
        sync._store = SimpleNamespace(get_synced_message_ids=lambda: set())
        requested_urls: list[str] = []

        def fake_get(url: str) -> dict:
            requested_urls.append(url)
            return {"apostas": [], "current_page": 1, "pages": 1}

        sync._get_with_retry = fake_get

        sync._fetch_recent_results({"xSuarez"}, include_unfiltered=True)

        queries = [parse_qs(urlparse(url).query) for url in requested_urls]
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0]["tipster"], ["xSuarez"])
        self.assertNotIn("tipster", queries[1])


if __name__ == "__main__":
    unittest.main()
