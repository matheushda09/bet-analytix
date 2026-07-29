from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sports_event_matching import (
    EventMatcher,
    ParticipantNormalizer,
    canonical_sport,
)
from sports_event_models import ExternalSportsEvent


NOW = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)


def event(
    home: str,
    away: str,
    *,
    sport: str = "football",
    starts_at: datetime | None = None,
    provider: str = "api_football",
    event_id: str = "1",
    status: str = "NS",
) -> ExternalSportsEvent:
    return ExternalSportsEvent(
        provider=provider,
        external_event_id=event_id,
        sport=sport,
        participant_home=home,
        participant_away=away,
        starts_at_utc=starts_at or NOW + timedelta(days=1),
        competition="Teste",
        country="Brasil",
        status=status,
    )


class ParticipantNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = ParticipantNormalizer()

    def test_sao_paulo_variations_are_equivalent(self) -> None:
        values = (
            "São Paulo",
            "Sao Paulo",
            "São Paulo FC",
            "SÃO PAULO FUTEBOL CLUBE",
        )
        self.assertEqual(
            {self.normalizer.normalize(value) for value in values},
            {"sao paulo"},
        )

    def test_athletico_variations_are_equivalent(self) -> None:
        values = (
            "Athletico-PR",
            "Athletico Paranaense",
            "Club Athletico Paranaense",
            "CAP",
        )
        self.assertEqual(
            {self.normalizer.normalize(value) for value in values},
            {"athletico paranaense"},
        )

    def test_vasco_variations_are_equivalent(self) -> None:
        values = ("Vasco", "Vasco da Gama", "CR Vasco da Gama")
        self.assertEqual(
            {self.normalizer.normalize(value) for value in values},
            {"vasco da gama"},
        )

    def test_incompatible_modalities_are_not_canonicalized(self) -> None:
        self.assertIsNone(canonical_sport("Futsal"))
        self.assertIsNone(canonical_sport("Tênis de mesa"))
        self.assertIsNone(canonical_sport("eSoccer"))
        self.assertEqual(canonical_sport("Futebol"), "football")


class ConservativeEventMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = EventMatcher(
            min_confidence=0.90,
            min_score_gap=0.10,
            participant_min_score=0.86,
            time_tolerance_minutes=15,
        )

    def match(self, event_name: str, candidates: list[ExternalSportsEvent], sport: str = "Futebol"):
        return self.matcher.match(
            sport=sport,
            event_name=event_name,
            window_start_utc=NOW - timedelta(hours=24),
            window_end_utc=NOW + timedelta(days=7),
            events=candidates,
        )

    def test_reversed_participant_order_is_accepted(self) -> None:
        result = self.match(
            "Santos x Chapecoense",
            [event("Chapecoense", "Santos")],
        )
        self.assertTrue(result.accepted)
        self.assertIn("participants_reversed", result.reasons)

    def test_two_similar_events_are_rejected_as_ambiguous(self) -> None:
        result = self.match(
            "Santos x Chapecoense",
            [
                event("Santos", "Chapecoense", event_id="1"),
                event(
                    "Santos",
                    "Chapecoense",
                    event_id="2",
                    starts_at=NOW + timedelta(days=3),
                ),
            ],
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "ambiguous_candidates")

    def test_wrong_sport_is_never_accepted(self) -> None:
        result = self.match(
            "Santos x Chapecoense",
            [event("Santos", "Chapecoense", sport="basketball")],
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.candidate_count, 0)

    def test_womens_and_main_team_are_not_mixed(self) -> None:
        result = self.match(
            "Corinthians x Santos",
            [event("Corinthians Feminino", "Santos Feminino")],
        )
        self.assertFalse(result.accepted)

    def test_youth_and_professional_are_not_mixed(self) -> None:
        result = self.match(
            "São Paulo x Palmeiras",
            [event("São Paulo U20", "Palmeiras U20")],
        )
        self.assertFalse(result.accepted)

    def test_team_b_and_main_team_are_not_mixed(self) -> None:
        result = self.match(
            "Barcelona x Sevilla",
            [event("Barcelona B", "Sevilla B")],
        )
        self.assertFalse(result.accepted)

    def test_short_women_marker_and_main_team_are_not_mixed(self) -> None:
        result = self.match(
            "Corinthians x Santos",
            [event("Corinthians W", "Santos W")],
        )
        self.assertFalse(result.accepted)

    def test_tennis_initial_and_surname_variations_are_accepted(self) -> None:
        result = self.match(
            "C. Alcaraz x Sinner",
            [
                event(
                    "Carlos Alcaraz",
                    "Jannik Sinner",
                    sport="tennis",
                    provider="live_tennis",
                )
            ],
            sport="Tênis",
        )
        self.assertTrue(result.accepted)

    def test_tennis_w_initial_is_not_mistaken_for_womens_team_marker(self) -> None:
        result = self.match(
            "W. Zhang x Sinner",
            [
                event(
                    "Wang Zhang",
                    "Jannik Sinner",
                    sport="tennis",
                    provider="live_tennis",
                )
            ],
            sport="Tênis",
        )
        self.assertTrue(result.accepted)

    def test_basketball_city_abbreviation_is_accepted(self) -> None:
        result = self.match(
            "LA Lakers x Miami Heat",
            [
                event(
                    "Los Angeles Lakers",
                    "Miami Heat",
                    sport="basketball",
                    provider="api_basketball",
                )
            ],
            sport="Basquete",
        )
        self.assertTrue(result.accepted)

    def test_dangerous_fuzzy_names_are_rejected(self) -> None:
        cases = (
            (
                "Manchester United x Arsenal",
                event("Manchester City", "Arsenal"),
            ),
            (
                "Inter Miami x Orlando City",
                event("Internacional", "Orlando City"),
            ),
            (
                "Inter de Milão x Juventus",
                event("Inter Miami", "Juventus"),
            ),
        )
        for signal, candidate in cases:
            with self.subTest(signal=signal):
                self.assertFalse(self.match(signal, [candidate]).accepted)

    def test_material_provider_time_conflict_is_rejected(self) -> None:
        result = self.match(
            "Santos x Chapecoense",
            [
                event(
                    "Santos",
                    "Chapecoense",
                    provider="api_football",
                    event_id="1",
                ),
                event(
                    "Santos",
                    "Chapecoense",
                    provider="football_data",
                    event_id="2",
                    starts_at=NOW + timedelta(days=1, hours=2),
                ),
            ],
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "provider_time_conflict")

    def test_close_provider_times_are_consolidated(self) -> None:
        result = self.match(
            "Santos x Chapecoense",
            [
                event(
                    "Santos",
                    "Chapecoense",
                    provider="api_football",
                    event_id="1",
                ),
                event(
                    "Santos",
                    "Chapecoense",
                    provider="football_data",
                    event_id="2",
                    starts_at=NOW + timedelta(days=1, minutes=10),
                ),
            ],
        )
        self.assertTrue(result.accepted)
        self.assertIn("corroborated_by_multiple_sources", result.reasons)


if __name__ == "__main__":
    unittest.main()
