from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import requests

from sports_event_config import load_sports_event_settings
from sports_schedule_providers import (
    ApiBasketballProvider,
    ApiFootballProvider,
    FootballDataProvider,
    LiveTennisProvider,
    SportsProviderRateLimited,
    TheSportsDbProvider,
)
from sports_schedule_store import SportsScheduleStore


def settings_for(cache_path: Path, retries: int = 0):
    with patch.dict(
        os.environ,
        {
            "SPORTS_EVENT_MATCHING_MODE": "shadow",
            "SPORTS_EVENT_CACHE_PATH": str(cache_path),
            "SPORTS_EVENT_REQUEST_MAX_RETRIES": str(retries),
        },
        clear=True,
    ):
        return load_sports_event_settings("nonexistent-test.env")


class ResponseSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.requests = []

    def get(self, *args, **kwargs):
        self.calls += 1
        self.requests.append((args, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(status: int, payload=None) -> requests.Response:
    item = requests.Response()
    item.status_code = status
    item.headers = {}
    if payload is not None:
        import json

        item._content = json.dumps(payload).encode("utf-8")
    else:
        item._content = b""
    return item


class ProviderPayloadParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = settings_for(Path(self.temp_dir.name) / "sports.sqlite3")
        self.store = SportsScheduleStore(self.settings.cache_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_api_football_payload_is_normalized_in_utc(self) -> None:
        provider = ApiFootballProvider(self.settings, self.store, "key")
        parsed = provider._parse_event(
            {
                "fixture": {
                    "id": 10,
                    "timestamp": 1785450600,
                    "status": {"short": "NS"},
                },
                "teams": {
                    "home": {"name": "Corinthians"},
                    "away": {"name": "Athletico-PR"},
                },
                "league": {"name": "Serie A", "country": "Brazil"},
            }
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.starts_at_utc.tzinfo, timezone.utc)
        self.assertEqual(parsed.sport, "football")

    def test_football_data_payload_is_normalized(self) -> None:
        provider = FootballDataProvider(self.settings, self.store, "key")
        parsed = provider._parse_event(
            {
                "id": 11,
                "utcDate": "2026-07-30T22:30:00Z",
                "status": "SCHEDULED",
                "homeTeam": {"name": "Corinthians"},
                "awayTeam": {"name": "Athletico Paranaense"},
                "competition": {"name": "Série A"},
                "area": {"name": "Brazil"},
            }
        )
        self.assertEqual(parsed.starts_at_utc.hour, 22)
        self.assertEqual(parsed.country, "Brazil")

    def test_api_basketball_payload_is_normalized(self) -> None:
        provider = ApiBasketballProvider(self.settings, self.store, "key")
        parsed = provider._parse_event(
            {
                "id": 12,
                "date": "2026-07-30T23:00:00+00:00",
                "status": {"short": "NS"},
                "teams": {
                    "home": {"name": "Boston Celtics"},
                    "away": {"name": "Miami Heat"},
                },
                "league": {"name": "NBA"},
                "country": {"name": "USA"},
            }
        )
        self.assertEqual(parsed.sport, "basketball")
        self.assertEqual(parsed.participant_home, "Boston Celtics")

    def test_live_tennis_payload_is_normalized(self) -> None:
        provider = LiveTennisProvider(self.settings, self.store, "key")
        parsed = provider._parse_event(
            {
                "id": 13,
                "scheduled_time": "2026-07-30T14:00:00Z",
                "status": "upcoming",
                "tournament": "ATP",
                "players": {
                    "p1": {"name": "Carlos Alcaraz"},
                    "p2": {"name": "Jannik Sinner"},
                },
            }
        )
        self.assertEqual(parsed.sport, "tennis")
        self.assertEqual(parsed.starts_at_utc.hour, 14)

    def test_thesportsdb_payload_without_time_is_rejected(self) -> None:
        provider = TheSportsDbProvider(self.settings, self.store)
        parsed = provider._parse_event(
            {
                "idEvent": "14",
                "strSport": "Soccer",
                "strHomeTeam": "Santos",
                "strAwayTeam": "Chapecoense",
                "dateEvent": "2026-07-30",
                "strTime": "",
            }
        )
        self.assertIsNone(parsed)

    def test_thesportsdb_searches_both_participant_orders(self) -> None:
        direct = response(200, {"event": None})
        reverse = response(
            200,
            {
                "event": [
                    {
                        "idEvent": "15",
                        "strSport": "Soccer",
                        "strHomeTeam": "Chapecoense",
                        "strAwayTeam": "Santos",
                        "strTimestamp": "2026-07-30T22:30:00Z",
                    }
                ]
            },
        )
        session = ResponseSession([direct, reverse])
        provider = TheSportsDbProvider(self.settings, self.store, session=session)

        events = provider.search_events(
            sport="football",
            participants=("Santos", "Chapecoense"),
            start_at_utc=datetime(2026, 7, 29, tzinfo=timezone.utc),
            end_at_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
            deadline=9999999999.0,
        )

        self.assertEqual(session.calls, 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].participant_home, "Chapecoense")

    def test_thesportsdb_uses_team_schedule_when_name_search_returns_old_match(self) -> None:
        old_direct = response(
            200,
            {
                "event": [
                    {
                        "idEvent": "old-1",
                        "strSport": "Soccer",
                        "strHomeTeam": "Fluminense",
                        "idHomeTeam": "134296",
                        "strAwayTeam": "Bahia",
                        "idAwayTeam": "134293",
                        "strTimestamp": "2026-07-12T19:00:00",
                    }
                ]
            },
        )
        old_reverse = response(
            200,
            {
                "event": [
                    {
                        "idEvent": "old-2",
                        "strSport": "Soccer",
                        "strHomeTeam": "Bahia",
                        "idHomeTeam": "134293",
                        "strAwayTeam": "Fluminense",
                        "idAwayTeam": "134296",
                        "strTimestamp": "2026-02-05T22:00:00",
                    }
                ]
            },
        )
        next_home_event = response(
            200,
            {
                "events": [
                    {
                        "idEvent": "2398396",
                        "strSport": "Soccer",
                        "strHomeTeam": "Fluminense",
                        "idHomeTeam": "134296",
                        "strAwayTeam": "Bahia",
                        "idAwayTeam": "134293",
                        "strTimestamp": "2026-07-30T00:30:00",
                    }
                ]
            },
        )
        session = ResponseSession([old_direct, old_reverse, next_home_event])
        provider = TheSportsDbProvider(self.settings, self.store, session=session)

        events = provider.search_events(
            sport="football",
            participants=("Fluminense", "Bahia"),
            start_at_utc=datetime(2026, 7, 28, 22, tzinfo=timezone.utc),
            end_at_utc=datetime(2026, 8, 5, 22, tzinfo=timezone.utc),
            deadline=9999999999.0,
        )

        self.assertEqual(session.calls, 3)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].external_event_id, "2398396")
        self.assertEqual(
            events[0].starts_at_utc,
            datetime(2026, 7, 30, 0, 30, tzinfo=timezone.utc),
        )

    def test_thesportsdb_resolves_team_id_when_event_search_has_no_history(self) -> None:
        direct = response(200, {"event": None})
        reverse = response(200, {"event": None})
        team_search = response(
            200,
            {
                "teams": [
                    {
                        "idTeam": "134284",
                        "strTeam": "Corinthians",
                        "strSport": "Soccer",
                    }
                ]
            },
        )
        opponent_search = response(
            200,
            {
                "teams": [
                    {
                        "idTeam": "134297",
                        "strTeam": "Athletico Paranaense",
                        "strSport": "Soccer",
                    }
                ]
            },
        )
        next_home_event = response(
            200,
            {
                "events": [
                    {
                        "idEvent": "2398394",
                        "strSport": "Soccer",
                        "strHomeTeam": "Corinthians",
                        "strAwayTeam": "Athletico Paranaense",
                        "strTimestamp": "2026-07-30T22:30:00Z",
                    }
                ]
            },
        )
        session = ResponseSession(
            [direct, reverse, team_search, opponent_search, next_home_event]
        )
        provider = TheSportsDbProvider(self.settings, self.store, session=session)

        events = provider.search_events(
            sport="football",
            participants=("Corinthians SP", "Athletico Paranaense PR"),
            start_at_utc=datetime(2026, 7, 29, tzinfo=timezone.utc),
            end_at_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
            deadline=9999999999.0,
        )

        self.assertEqual(session.calls, 5)
        self.assertEqual([event.external_event_id for event in events], ["2398394"])

    def test_thesportsdb_normalizes_state_and_corporate_suffixes_before_search(self) -> None:
        direct = response(200, {"event": None})
        reverse = response(200, {"event": None})
        home_search = response(
            200,
            {
                "teams": [
                    {
                        "idTeam": "134291",
                        "strTeam": "Coritiba",
                        "strSport": "Soccer",
                    }
                ]
            },
        )
        away_search = response(
            200,
            {
                "teams": [
                    {
                        "idTeam": "134294",
                        "strTeam": "Cruzeiro",
                        "strSport": "Soccer",
                    }
                ]
            },
        )
        next_home_event = response(
            200,
            {
                "events": [
                    {
                        "idEvent": "2398395",
                        "strSport": "Soccer",
                        "strHomeTeam": "Coritiba",
                        "strAwayTeam": "Cruzeiro",
                        "strTimestamp": "2026-07-31T00:30:00Z",
                    }
                ]
            },
        )
        session = ResponseSession(
            [direct, reverse, home_search, away_search, next_home_event]
        )
        provider = TheSportsDbProvider(self.settings, self.store, session=session)

        events = provider.search_events(
            sport="football",
            participants=("Coritiba SAF PR", "Cruzeiro MG"),
            start_at_utc=datetime(2026, 7, 29, tzinfo=timezone.utc),
            end_at_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
            deadline=9999999999.0,
        )

        requested_params = [
            request_kwargs["params"]
            for _request_args, request_kwargs in session.requests
        ]
        self.assertEqual(requested_params[0], {"e": "coritiba_vs_cruzeiro"})
        self.assertEqual(requested_params[1], {"e": "cruzeiro_vs_coritiba"})
        self.assertEqual(requested_params[2], {"t": "coritiba"})
        self.assertEqual(requested_params[3], {"t": "cruzeiro"})
        self.assertEqual([event.external_event_id for event in events], ["2398395"])

    def test_thesportsdb_uses_date_filter_when_free_next_event_is_another_game(self) -> None:
        old_direct = response(
            200,
            {
                "event": [
                    {
                        "idEvent": "old-1",
                        "strSport": "Soccer",
                        "strHomeTeam": "Bragantino",
                        "idHomeTeam": "134736",
                        "strAwayTeam": "Corinthians",
                        "idAwayTeam": "134284",
                        "strTimestamp": "2026-01-15T22:30:00Z",
                    }
                ]
            },
        )
        reverse = response(200, {"event": None})
        other_schedule_responses = [
            response(
                200,
                {
                    "events": [
                        {
                            "idEvent": f"other-{index}",
                            "strSport": "Soccer",
                            "strHomeTeam": "Outro",
                            "strAwayTeam": "Adversário",
                            "strTimestamp": "2026-08-09T18:00:00Z",
                        }
                    ]
                },
            )
            for index in range(4)
        ]
        date_filtered = response(
            200,
            {
                "event": [
                    {
                        "idEvent": "2398403",
                        "strSport": "Soccer",
                        "strHomeTeam": "Bragantino",
                        "strAwayTeam": "Corinthians",
                        "strTimestamp": "2026-08-09T21:30:00Z",
                    }
                ]
            },
        )
        session = ResponseSession(
            [old_direct, reverse, *other_schedule_responses, date_filtered]
        )
        provider = TheSportsDbProvider(self.settings, self.store, session=session)

        events = provider.search_events(
            sport="football",
            participants=("Red Bull Bragantino SP", "Corinthians SP"),
            start_at_utc=datetime(2026, 8, 9, tzinfo=timezone.utc),
            end_at_utc=datetime(2026, 8, 9, 23, 59, tzinfo=timezone.utc),
            deadline=9999999999.0,
        )

        self.assertEqual(session.calls, 7)
        self.assertEqual(
            session.requests[-1][1]["params"],
            {"e": "bragantino_vs_corinthians", "d": "2026-08-09"},
        )
        self.assertIn(
            "2398403",
            {event.external_event_id for event in events},
        )

    def test_thesportsdb_default_window_stays_below_free_minute_limit(self) -> None:
        home_search = response(
            200,
            {
                "teams": [
                    {
                        "idTeam": "134736",
                        "strTeam": "Bragantino",
                        "strSport": "Soccer",
                    }
                ]
            },
        )
        away_search = response(
            200,
            {
                "teams": [
                    {
                        "idTeam": "134284",
                        "strTeam": "Corinthians",
                        "strSport": "Soccer",
                    }
                ]
            },
        )
        session = ResponseSession(
            [
                response(200, {"event": None}),
                response(200, {"event": None}),
                home_search,
                away_search,
                *[response(200, {"events": None}) for _index in range(4)],
                *[response(200, {"event": None}) for _index in range(18)],
            ]
        )
        provider = TheSportsDbProvider(self.settings, self.store, session=session)

        events = provider.search_events(
            sport="football",
            participants=("Bragantino", "Corinthians"),
            start_at_utc=datetime(2026, 8, 6, 12, tzinfo=timezone.utc),
            end_at_utc=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
            deadline=9999999999.0,
        )

        self.assertEqual(events, [])
        self.assertEqual(session.calls, 26)
        self.assertLessEqual(
            session.calls,
            self.settings.provider_minute_limits["thesportsdb"],
        )


class ProviderResilienceTests(unittest.TestCase):
    def test_http_429_does_not_retry_forever(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = settings_for(Path(temp_dir) / "sports.sqlite3", retries=3)
            store = SportsScheduleStore(settings.cache_path)
            store.initialize()
            fake_session = ResponseSession([response(429)])
            provider = ApiFootballProvider(
                settings,
                store,
                "secret",
                session=fake_session,
            )

            with self.assertRaises(SportsProviderRateLimited):
                provider._request_json(
                    "https://example.invalid",
                    deadline=9999999999.0,
                )

            self.assertEqual(fake_session.calls, 1)

    def test_timeout_retries_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = settings_for(Path(temp_dir) / "sports.sqlite3", retries=1)
            store = SportsScheduleStore(settings.cache_path)
            store.initialize()
            fake_session = ResponseSession(
                [requests.Timeout("one"), requests.Timeout("two")]
            )
            provider = ApiFootballProvider(
                settings,
                store,
                "secret",
                session=fake_session,
            )

            with self.assertRaises(Exception):
                provider._request_json(
                    "https://example.invalid",
                    deadline=9999999999.0,
                )

            self.assertEqual(fake_session.calls, 2)


if __name__ == "__main__":
    unittest.main()
