from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from bet_analytix_writer import BetAnalytixWriter
from discord_database import DiscordSignalStore
from discord_reaction_bot import DiscordSignalClient
from message_parser import ParsedTelegramTip
from sports_event_models import ExternalSportsEvent


NOW = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)


class SportsEventPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "discord.sqlite3"
        self.store = DiscordSignalStore(self.path)
        self.store.initialize()
        self.tip = ParsedTelegramTip(
            tipster="Joao",
            event_datetime=NOW + timedelta(days=1),
            sport="Futebol",
            league=None,
            pick="Santos - Resultado final",
            odd=1.80,
            stake=50.0,
            bookmaker="Betano",
            source_bet_id=9001,
            event="Santos x Chapecoense",
        )
        inserted = self.store.enqueue_signal(
            guild_id="1",
            channel_id="2",
            message_id=3,
            signal_sender_id=4,
            reacting_user_id=5,
            tip=self.tip,
            raw_message="sinal",
        )
        self.assertTrue(inserted)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _record_accepted_match(self, starts_at: datetime) -> sqlite3.Row:
        self.store.record_sports_event_match(
            {
                "source_bet_id": self.tip.source_bet_id,
                "mode": "enabled",
                "match_status": "accepted",
                "match_reason": "unique_high_confidence_match",
                "sport": "football",
                "provider": "api_football",
                "external_event_id": "fixture-123",
                "starts_at_utc": starts_at.isoformat(),
                "event_status": "NS",
                "fallback_datetime_utc": NOW.isoformat(),
            }
        )
        self.store.mark_sports_event_applied(
            self.tip.source_bet_id,
            bet_analytix_bet_id=777,
            next_check_at_ts=int(time.time()) - 1,
        )
        return self.store.get_due_sports_event_matches()[0]

    def test_audit_application_and_reschedule_are_persisted(self) -> None:
        starts_at = NOW + timedelta(days=1)
        audit = {
            "source_bet_id": self.tip.source_bet_id,
            "mode": "enabled",
            "match_status": "accepted",
            "match_reason": "unique_high_confidence_match",
            "sport": "football",
            "signal_participants": ["Santos", "Chapecoense"],
            "normalized_signal_participants": ["santos", "chapecoense"],
            "provider": "api_football",
            "external_event_id": "fixture-123",
            "participant_home": "Santos",
            "participant_away": "Chapecoense",
            "normalized_event_participants": ["santos", "chapecoense"],
            "competition": "Serie A",
            "country": "Brazil",
            "starts_at_utc": starts_at.isoformat(),
            "event_status": "NS",
            "confidence": 0.99,
            "participant_1_score": 1.0,
            "participant_2_score": 1.0,
            "second_best_confidence": None,
            "candidate_count": 1,
            "providers_consulted": ["api_football"],
            "reasons": ["both_participants_strong"],
            "from_cache": False,
            "fallback_datetime_utc": NOW.isoformat(),
            "raw_payload": {"fixture": {"id": 123}},
        }
        self.store.record_sports_event_match(audit)
        self.store.mark_sports_event_applied(
            self.tip.source_bet_id,
            bet_analytix_bet_id=777,
            next_check_at_ts=int(time.time()) - 1,
        )

        due = self.store.get_due_sports_event_matches()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["bet_analytix_bet_id"], 777)
        self.assertEqual(due[0]["applied_starts_at_utc"], starts_at.isoformat())
        self.assertEqual(
            json.loads(str(due[0]["payload_json"]))["event"],
            "Santos x Chapecoense",
        )

        changed_start = starts_at + timedelta(hours=2)
        self.store.record_sports_event_refresh(
            source_bet_id=self.tip.source_bet_id,
            starts_at_utc=changed_start.isoformat(),
            event_status="NS",
            next_check_at_ts=None,
            action="bet_datetime_updated",
            applied_datetime_updated=True,
            details={"changed": True},
        )
        self.store.record_sports_event_refresh(
            source_bet_id=self.tip.source_bet_id,
            starts_at_utc=changed_start.isoformat(),
            event_status="NS",
            next_check_at_ts=None,
            action="checked_no_change",
            applied_datetime_updated=False,
        )

        connection = sqlite3.connect(self.path)
        try:
            connection.row_factory = sqlite3.Row
            current = connection.execute(
                "SELECT * FROM sports_event_matches WHERE source_bet_id = ?",
                (self.tip.source_bet_id,),
            ).fetchone()
            history = connection.execute(
                "SELECT * FROM sports_event_match_history WHERE source_bet_id = ?",
                (self.tip.source_bet_id,),
            ).fetchall()
        finally:
            connection.close()

        self.assertEqual(current["starts_at_utc"], changed_start.isoformat())
        self.assertEqual(current["applied_starts_at_utc"], changed_start.isoformat())
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["previous_starts_at_utc"], starts_at.isoformat())
        self.assertEqual(history[0]["new_starts_at_utc"], changed_start.isoformat())

    def test_observed_blocked_change_does_not_claim_it_was_applied(self) -> None:
        starts_at = NOW + timedelta(days=1)
        self.store.record_sports_event_match(
            {
                "source_bet_id": self.tip.source_bet_id,
                "mode": "enabled",
                "match_status": "accepted",
                "match_reason": "unique_high_confidence_match",
                "sport": "football",
                "provider": "api_football",
                "external_event_id": "fixture-123",
                "starts_at_utc": starts_at.isoformat(),
                "event_status": "NS",
                "fallback_datetime_utc": NOW.isoformat(),
            }
        )
        self.store.mark_sports_event_applied(
            self.tip.source_bet_id,
            bet_analytix_bet_id=777,
            next_check_at_ts=int(time.time()) - 1,
        )
        postponed_start = starts_at + timedelta(days=1)
        self.store.record_sports_event_refresh(
            source_bet_id=self.tip.source_bet_id,
            starts_at_utc=postponed_start.isoformat(),
            event_status="PST",
            next_check_at_ts=int(time.time()) + 300,
            action="status_change_not_applied",
            applied_datetime_updated=False,
        )

        due_later = self.store.get_due_sports_event_matches()
        self.assertEqual(due_later, [])
        connection = sqlite3.connect(self.path)
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT starts_at_utc, applied_starts_at_utc FROM sports_event_matches"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row["starts_at_utc"], postponed_start.isoformat())
        self.assertEqual(row["applied_starts_at_utc"], starts_at.isoformat())

    def test_recheck_updates_a_simple_bet_when_official_time_changes(self) -> None:
        starts_at = NOW + timedelta(days=1)
        row = self._record_accepted_match(starts_at)
        changed_start = starts_at + timedelta(hours=2)
        event = ExternalSportsEvent(
            provider="api_football",
            external_event_id="fixture-123",
            sport="football",
            participant_home="Santos",
            participant_away="Chapecoense",
            starts_at_utc=changed_start,
            status="NS",
        )

        class FakeService:
            def refresh_event(self, **kwargs):
                return event

            def next_recheck_timestamp(self, starts_at_utc, status):
                return None

        class FakeWriter:
            def __init__(self) -> None:
                self.calls: list[tuple[int, datetime]] = []

            def update_bet_datetime(self, bet_id: int, value: datetime):
                self.calls.append((bet_id, value))

        writer = FakeWriter()
        client = SimpleNamespace(
            _sports_event_service=FakeService(),
            _sports_event_settings=SimpleNamespace(
                recheck_within_24h_seconds=1800,
            ),
            _store=self.store,
            _writer=writer,
        )

        asyncio.run(DiscordSignalClient._refresh_sports_event_match(client, row))

        self.assertEqual(writer.calls, [(777, changed_start)])

    def test_recheck_does_not_update_a_finished_event(self) -> None:
        starts_at = NOW + timedelta(days=1)
        row = self._record_accepted_match(starts_at)
        event = ExternalSportsEvent(
            provider="api_football",
            external_event_id="fixture-123",
            sport="football",
            participant_home="Santos",
            participant_away="Chapecoense",
            starts_at_utc=starts_at + timedelta(hours=2),
            status="FT",
        )

        class FakeService:
            def refresh_event(self, **kwargs):
                return event

            def next_recheck_timestamp(self, starts_at_utc, status):
                return None

        class FakeWriter:
            def __init__(self) -> None:
                self.calls = 0

            def update_bet_datetime(self, bet_id: int, value: datetime):
                self.calls += 1

        writer = FakeWriter()
        client = SimpleNamespace(
            _sports_event_service=FakeService(),
            _sports_event_settings=SimpleNamespace(
                recheck_within_24h_seconds=1800,
            ),
            _store=self.store,
            _writer=writer,
        )

        asyncio.run(DiscordSignalClient._refresh_sports_event_match(client, row))

        self.assertEqual(writer.calls, 0)


class SportsEventTimezoneTests(unittest.TestCase):
    def test_aware_provider_datetime_is_not_converted_twice(self) -> None:
        writer = object.__new__(BetAnalytixWriter)
        writer._settings = SimpleNamespace(timezone="America/Sao_Paulo")
        provider_datetime = datetime(2026, 7, 30, 22, 30, tzinfo=timezone.utc)

        date_field, time_field = writer._bet_analytix_datetime_fields(
            provider_datetime
        )

        self.assertEqual(date_field, "2026-07-30")
        self.assertEqual(time_field, "22:30")


if __name__ == "__main__":
    unittest.main()
