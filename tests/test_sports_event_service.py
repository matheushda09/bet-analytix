from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sports_event_config import load_sports_event_settings
from sports_event_models import ExternalSportsEvent
from sports_event_service import SportsEventService
from sports_schedule_providers import SportsProviderError, SportsProviderRateLimited
from sports_schedule_store import SportsScheduleStore


NOW = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)


def settings_for(cache_path: Path):
    with patch.dict(
        os.environ,
        {
            "SPORTS_EVENT_MATCHING_MODE": "shadow",
            "SPORTS_EVENT_CACHE_PATH": str(cache_path),
            "SPORTS_EVENT_TOTAL_TIMEOUT_SECONDS": "5",
            "SPORTS_EVENT_LOCK_WAIT_SECONDS": "0.1",
            "SPORTS_EVENT_REQUEST_MAX_RETRIES": "0",
        },
        clear=True,
    ):
        return load_sports_event_settings("nonexistent-test.env")


class FakeProvider:
    supported_sports = frozenset({"football"})
    cache_scope = "window"

    def __init__(
        self,
        name: str,
        events: list[ExternalSportsEvent] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.events = events or []
        self.error = error
        self.search_calls = 0
        self.get_calls = 0

    def search_events(self, **kwargs):
        self.search_calls += 1
        if self.error:
            raise self.error
        return list(self.events)

    def get_event(self, external_event_id: str, **kwargs):
        self.get_calls += 1
        if self.error:
            raise self.error
        return next(
            (
                event
                for event in self.events
                if event.external_event_id == external_event_id
            ),
            None,
        )


def fixture(provider: str = "football_data") -> ExternalSportsEvent:
    return ExternalSportsEvent(
        provider=provider,
        external_event_id="123",
        sport="football",
        participant_home="Santos",
        participant_away="Chapecoense",
        starts_at_utc=NOW + timedelta(days=1),
        status="SCHEDULED",
    )


class SportsEventServiceTests(unittest.TestCase):
    def test_timeout_in_primary_uses_fallback_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = settings_for(Path(temp_dir) / "sports.sqlite3")
            store = SportsScheduleStore(settings.cache_path)
            store.initialize()
            primary = FakeProvider(
                "api_football",
                error=SportsProviderError("timeout"),
            )
            fallback = FakeProvider("football_data", [fixture()])
            service = SportsEventService(
                settings,
                store,
                {"api_football": primary, "football_data": fallback},
            )

            result = service.resolve_event(
                sport="Futebol",
                event_name="Santos x Chapecoense",
                received_at_utc=NOW,
            )

            self.assertTrue(result.accepted)
            self.assertEqual(result.event.provider, "football_data")
            self.assertEqual(primary.search_calls, 1)
            self.assertEqual(fallback.search_calls, 1)

    def test_rate_limit_in_primary_uses_fallback_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = settings_for(Path(temp_dir) / "sports.sqlite3")
            store = SportsScheduleStore(settings.cache_path)
            store.initialize()
            primary = FakeProvider(
                "api_football",
                error=SportsProviderRateLimited("429"),
            )
            fallback = FakeProvider("football_data", [fixture()])
            service = SportsEventService(
                settings,
                store,
                {"api_football": primary, "football_data": fallback},
            )

            result = service.resolve_event(
                sport="Futebol",
                event_name="Santos x Chapecoense",
                received_at_utc=NOW,
            )

            self.assertTrue(result.accepted)
            self.assertEqual(result.event.provider, "football_data")

    def test_shared_cache_avoids_duplicate_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = settings_for(Path(temp_dir) / "sports.sqlite3")
            store_1 = SportsScheduleStore(settings.cache_path)
            store_1.initialize()
            provider_1 = FakeProvider("football_data", [fixture()])
            service_1 = SportsEventService(
                settings,
                store_1,
                {"football_data": provider_1},
            )
            first = service_1.resolve_event(
                sport="Futebol",
                event_name="Santos x Chapecoense",
                received_at_utc=NOW,
            )

            store_2 = SportsScheduleStore(settings.cache_path)
            store_2.initialize()
            provider_2 = FakeProvider("football_data", [fixture()])
            service_2 = SportsEventService(
                settings,
                store_2,
                {"football_data": provider_2},
            )
            second = service_2.resolve_event(
                sport="Futebol",
                event_name="Santos x Chapecoense",
                received_at_utc=NOW,
            )

            self.assertTrue(first.accepted)
            self.assertTrue(second.accepted)
            self.assertEqual(provider_1.search_calls, 1)
            self.assertEqual(provider_2.search_calls, 0)
            self.assertTrue(second.from_cache)

    def test_no_provider_keeps_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = settings_for(Path(temp_dir) / "sports.sqlite3")
            store = SportsScheduleStore(settings.cache_path)
            store.initialize()
            service = SportsEventService(settings, store, {})

            result = service.resolve_event(
                sport="Futebol",
                event_name="Vasco x Independiente Medellín",
                received_at_utc=NOW,
            )

            self.assertFalse(result.accepted)
            self.assertEqual(result.status, "fallback")

    def test_refresh_is_cached_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = settings_for(Path(temp_dir) / "sports.sqlite3")
            store = SportsScheduleStore(settings.cache_path)
            store.initialize()
            provider = FakeProvider("football_data", [fixture()])
            service = SportsEventService(
                settings,
                store,
                {"football_data": provider},
            )

            first = service.refresh_event(
                provider_name="football_data",
                external_event_id="123",
            )
            second = service.refresh_event(
                provider_name="football_data",
                external_event_id="123",
            )

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertEqual(provider.get_calls, 1)


class SportsScheduleStoreTests(unittest.TestCase):
    def test_shared_lock_and_rate_limit_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SportsScheduleStore(Path(temp_dir) / "sports.sqlite3")
            store.initialize()

            self.assertTrue(store.acquire_fetch_lock("same", "one", 60))
            self.assertFalse(store.acquire_fetch_lock("same", "two", 60))
            store.release_fetch_lock("same", "one")
            self.assertTrue(store.acquire_fetch_lock("same", "two", 60))

            reservation = store.reserve_provider_call(
                "provider",
                minute_limit=1,
                daily_limit=1,
            )
            self.assertIsNotNone(reservation)
            self.assertIsNone(
                store.reserve_provider_call(
                    "provider",
                    minute_limit=1,
                    daily_limit=1,
                )
            )

    def test_new_database_initializes_and_can_be_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sports.sqlite3"
            SportsScheduleStore(path).initialize()
            SportsScheduleStore(path).initialize()
            self.assertTrue(path.exists())


class SportsEventConfigTests(unittest.TestCase):
    def test_feature_is_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = load_sports_event_settings("nonexistent-test.env")
        self.assertEqual(settings.mode, "disabled")
        self.assertFalse(settings.enabled)


if __name__ == "__main__":
    unittest.main()
