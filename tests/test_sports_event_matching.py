from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sports_event_matching import (
    BRAZILIAN_STATES,
    EventMatcher,
    ParticipantNormalizer,
    brazilian_state_uf,
    canonical_sport,
    split_event_participants,
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
        values = (
            "Vasco",
            "Vasco da Gama",
            "CR Vasco da Gama",
            "Vasco da Gama SAF",
        )
        self.assertEqual(
            {self.normalizer.normalize(value) for value in values},
            {"vasco da gama"},
        )

    def test_unambiguous_brasileirao_aliases_are_equivalent(self) -> None:
        cases = {
            "atletico mineiro": ("Atlético Mineiro", "Atlético-MG"),
            "athletico paranaense": (
                "Athletico",
                "Athletico-PR",
                "Atlético-PR",
                "Atlético Paranaense",
            ),
            "bragantino": ("Red Bull Bragantino", "Bragantino"),
            "chapecoense": ("Chapecoense", "Chape"),
            "internacional": ("Internacional", "Inter-RS"),
            "coritiba": ("Coritiba", "Coritiba SAF"),
        }
        for expected, values in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(
                    {self.normalizer.normalize(value) for value in values},
                    {expected},
                )

    def test_ambiguous_inter_alias_is_not_globally_canonicalized(self) -> None:
        self.assertEqual(self.normalizer.normalize("Inter"), "inter")
        self.assertEqual(
            self.normalizer.normalize("Inter Miami"),
            "inter miami",
        )

    def test_incompatible_modalities_are_not_canonicalized(self) -> None:
        self.assertIsNone(canonical_sport("Futsal"))
        self.assertIsNone(canonical_sport("Tênis de mesa"))
        self.assertIsNone(canonical_sport("eSoccer"))
        self.assertEqual(canonical_sport("Futebol"), "football")

    def test_brazilian_state_suffixes_do_not_reduce_club_matching(self) -> None:
        for state_name, state_uf in BRAZILIAN_STATES.items():
            with self.subTest(state=state_name):
                self.assertEqual(
                    self.normalizer.normalize(f"Clube Teste {state_uf}"),
                    "teste",
                )
                self.assertEqual(
                    self.normalizer.normalize(f"Clube Teste {state_name}"),
                    "teste",
                )

    def test_state_name_alone_is_preserved_as_a_possible_club_name(self) -> None:
        self.assertEqual(self.normalizer.normalize("Bahia"), "bahia")
        self.assertEqual(self.normalizer.normalize("São Paulo"), "sao paulo")
        self.assertEqual(self.normalizer.normalize("Amazonas FC"), "amazonas")


class BrazilianStateRecoveryTests(unittest.TestCase):
    def test_map_contains_all_states_and_federal_district(self) -> None:
        self.assertEqual(len(BRAZILIAN_STATES), 27)
        self.assertEqual(set(BRAZILIAN_STATES.values()), {
            "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO",
            "MA", "MT", "MS", "MG", "PA", "PB", "PR", "PE", "PI",
            "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO",
        })

    def test_every_state_name_and_uf_recovers_extra_x_separator(self) -> None:
        for state_name, state_uf in BRAZILIAN_STATES.items():
            for state_token in (state_name, state_uf, state_uf.casefold()):
                with self.subTest(state=state_name, token=state_token):
                    self.assertEqual(brazilian_state_uf(state_token), state_uf)
                    self.assertEqual(
                        split_event_participants(
                            f"Mandante x Visitante x {state_token}"
                        ),
                        ("Mandante", f"Visitante {state_uf}"),
                    )

    def test_state_names_are_accent_and_case_insensitive(self) -> None:
        cases = {
            "amapa": "AP",
            "CEARA": "CE",
            "distrito federal": "DF",
            "espirito santo": "ES",
            "goias": "GO",
            "maranhao": "MA",
            "para": "PA",
            "paraiba": "PB",
            "parana": "PR",
            "piaui": "PI",
            "rondonia": "RO",
            "sao paulo": "SP",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(brazilian_state_uf(value), expected)

    def test_real_extra_x_case_is_recovered(self) -> None:
        self.assertEqual(
            split_event_participants("Corinthians x Athletico x PR"),
            ("Corinthians", "Athletico PR"),
        )

    def test_extra_x_before_both_state_suffixes_is_recovered(self) -> None:
        self.assertEqual(
            split_event_participants("Corinthians x SP x Athletico x Paraná"),
            ("Corinthians SP", "Athletico PR"),
        )

    def test_normal_two_participant_event_with_state_name_is_untouched(self) -> None:
        self.assertEqual(
            split_event_participants("Fluminense x Bahia"),
            ("Fluminense", "Bahia"),
        )
        self.assertEqual(
            split_event_participants("Palmeiras x São Paulo"),
            ("Palmeiras", "São Paulo"),
        )

    def test_three_unknown_participants_remain_rejected(self) -> None:
        self.assertIsNone(
            split_event_participants("Time A x Time B x Time C")
        )


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

    def test_extra_x_before_state_suffix_matches_official_participants(self) -> None:
        result = self.match(
            "Corinthians x Athletico x PR",
            [event("Corinthians", "Athletico Paranaense")],
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.participants, ("Corinthians", "Athletico PR"))

    def test_standard_state_suffixes_match_names_without_suffixes(self) -> None:
        result = self.match(
            "Corinthians SP x Athletico Paranaense PR",
            [event("Corinthians", "Athletico Paranaense")],
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.participant_1_score, 1.0)
        self.assertEqual(result.participant_2_score, 1.0)

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
