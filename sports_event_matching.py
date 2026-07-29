"""Normalização e matching conservador de eventos esportivos."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone

from rapidfuzz import fuzz

from sports_event_models import (
    EventMatchResult,
    ExternalSportsEvent,
    ScoredEventCandidate,
)


EVENT_SEPARATOR = re.compile(r"\s+(?:x|vs?\.?|versus|@)\s+", re.IGNORECASE)
NON_ALNUM = re.compile(r"[^a-z0-9]+")
SPACE = re.compile(r"\s+")

SPORT_ALIASES: dict[str, set[str]] = {
    "football": {"futebol", "football", "soccer", "association football"},
    "basketball": {"basquete", "basketball", "basket ball"},
    "tennis": {"tenis", "tennis"},
}
SPORT_FORBIDDEN_TERMS: dict[str, set[str]] = {
    "football": {"futsal", "beach soccer", "beach football", "esoccer", "e soccer", "esports"},
    "basketball": {"wheelchair basketball", "basketball 3x3", "3x3"},
    "tennis": {"table tennis", "tenis de mesa", "ping pong", "esports"},
}

DEFAULT_PARTICIPANT_ALIASES: dict[str, str] = {
    "athletico pr": "athletico paranaense",
    "club athletico paranaense": "athletico paranaense",
    "cap": "athletico paranaense",
    "sc corinthians paulista": "corinthians",
    "sport club corinthians paulista": "corinthians",
    "corinthians paulista": "corinthians",
    "cr vasco da gama": "vasco da gama",
    "club de regatas vasco da gama": "vasco da gama",
    "vasco": "vasco da gama",
    "sao paulo fc": "sao paulo",
    "sao paulo futebol clube": "sao paulo",
}

REMOVABLE_CLUB_TOKENS = {
    "fc",
    "cf",
    "ec",
    "sc",
    "afc",
    "ac",
    "club",
    "clube",
    "futebol",
    "football",
    "esporte",
    "sport",
}

CATEGORY_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "women": (
        re.compile(r"\b(?:women|woman|womens|feminino|feminina|fem)\b"),
    ),
    "u23": (re.compile(r"\b(?:u|sub)\s*23\b"),),
    "u21": (re.compile(r"\b(?:u|sub)\s*21\b"),),
    "u20": (re.compile(r"\b(?:u|sub)\s*20\b"),),
    "u19": (re.compile(r"\b(?:u|sub)\s*19\b"),),
    "u18": (re.compile(r"\b(?:u|sub)\s*18\b"),),
    "u17": (re.compile(r"\b(?:u|sub)\s*17\b"),),
    "reserves": (re.compile(r"\b(?:reserves?|reservas?)\b"),),
    "team_b": (
        re.compile(r"\b(?:team|equipe|time)\s+b\b"),
        re.compile(r"\bb\b"),
        re.compile(r"\bii\b"),
    ),
    "futsal": (re.compile(r"\bfutsal\b"),),
    "esports": (re.compile(r"\b(?:esports?|e soccer|esoccer)\b"),),
    "doubles": (re.compile(r"\b(?:doubles?|duplas?)\b"),),
}

SHORT_WOMEN_MARKER = re.compile(r"\bw\b")

DEFAULT_BASKETBALL_CITY_ALIASES = {
    "la": "los angeles",
    "ny": "new york",
    "okc": "oklahoma city",
    "gs": "golden state",
    "sa": "san antonio",
    "no": "new orleans",
    "phx": "phoenix",
    "min": "minnesota",
}

REJECTED_STATUSES = {
    "cancelled",
    "canceled",
    "postponed",
    "suspended",
    "abandoned",
    "tbd",
    "pst",
    "canc",
    "susp",
    "abd",
}

PROVIDER_QUALITY = {
    "api_football": 1.0,
    "football_data": 0.98,
    "api_basketball": 0.98,
    "live_tennis": 0.95,
    "thesportsdb": 0.88,
}


class ParticipantNormalizer:
    """Normaliza participantes e aplica aliases extensíveis."""

    def __init__(self, custom_aliases: dict[str, str] | None = None) -> None:
        aliases = dict(DEFAULT_PARTICIPANT_ALIASES)
        aliases.update(custom_aliases or {})
        self._aliases = {
            self._base_normalize(alias): self._base_normalize(canonical)
            for alias, canonical in aliases.items()
        }

    def normalize(self, value: str) -> str:
        base = self._base_normalize(value)
        aliased = self._aliases.get(base, base)
        tokens = [token for token in aliased.split() if token not in REMOVABLE_CLUB_TOKENS]
        cleaned = " ".join(tokens)
        return self._aliases.get(cleaned, cleaned)

    def categories(self, value: str, sport: str | None = None) -> frozenset[str]:
        normalized = self._base_normalize(value)
        categories = {
            category
            for category, patterns in CATEGORY_PATTERNS.items()
            if sport != "tennis" or category != "team_b"
            if any(pattern.search(normalized) for pattern in patterns)
        }
        # "Team W" é comum em feeds de futebol/basquete, mas "W. Zhang"
        # é uma abreviação legítima de nome no tênis.
        if sport in {"football", "basketball"} and SHORT_WOMEN_MARKER.search(normalized):
            categories.add("women")
        return frozenset(categories)

    @staticmethod
    def _base_normalize(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", str(value).casefold())
        without_marks = "".join(
            char for char in decomposed if not unicodedata.combining(char)
        )
        return SPACE.sub(" ", NON_ALNUM.sub(" ", without_marks)).strip()


def canonical_sport(value: str) -> str | None:
    normalized = ParticipantNormalizer._base_normalize(value)
    for canonical, forbidden in SPORT_FORBIDDEN_TERMS.items():
        if normalized in forbidden:
            return None
        if any(term in normalized for term in forbidden):
            return None
    for canonical, aliases in SPORT_ALIASES.items():
        if normalized in aliases:
            return canonical
    return None


def split_event_participants(event_name: str) -> tuple[str, str] | None:
    parts = [part.strip() for part in EVENT_SEPARATOR.split(event_name.strip())]
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


class EventMatcher:
    """Pontua candidatos e rejeita automaticamente qualquer ambiguidade material."""

    def __init__(
        self,
        *,
        min_confidence: float,
        min_score_gap: float,
        participant_min_score: float,
        time_tolerance_minutes: int,
        participant_aliases: dict[str, str] | None = None,
    ) -> None:
        self._min_confidence = min_confidence
        self._min_score_gap = min_score_gap
        self._participant_min_score = participant_min_score
        self._time_tolerance_seconds = time_tolerance_minutes * 60
        self._normalizer = ParticipantNormalizer(participant_aliases)

    def match(
        self,
        *,
        sport: str,
        event_name: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
        events: list[ExternalSportsEvent],
        providers_consulted: tuple[str, ...] = (),
        from_cache: bool = False,
    ) -> EventMatchResult:
        canonical = canonical_sport(sport)
        participants = split_event_participants(event_name)
        base_kwargs = {
            "sport": canonical,
            "participants": participants,
            "window_start_utc": _utc(window_start_utc),
            "window_end_utc": _utc(window_end_utc),
            "providers_consulted": providers_consulted,
            "from_cache": from_cache,
        }
        if canonical is None:
            return EventMatchResult(
                accepted=False,
                status="fallback",
                reason="unsupported_or_incompatible_sport",
                **base_kwargs,
            )
        if participants is None:
            return EventMatchResult(
                accepted=False,
                status="fallback",
                reason="participants_not_safely_parsed",
                **base_kwargs,
            )

        scored = [
            candidate
            for event in events
            if (
                candidate := self._score_event(
                    canonical,
                    participants,
                    _utc(window_start_utc),
                    _utc(window_end_utc),
                    event,
                )
            )
            is not None
        ]
        scored.sort(key=lambda item: item.confidence, reverse=True)
        if not scored:
            normalized = tuple(self._normalizer.normalize(item) for item in participants)
            return EventMatchResult(
                accepted=False,
                status="fallback",
                reason="no_strong_candidate",
                normalized_signal_participants=normalized,
                candidate_count=0,
                **base_kwargs,
            )

        conflict = self._find_material_time_conflict(scored)
        if conflict is not None:
            best = scored[0]
            return self._result_from_candidate(
                best,
                accepted=False,
                status="fallback",
                reason="provider_time_conflict",
                scored=scored,
                base_kwargs=base_kwargs,
                extra_reasons=("material_time_conflict",),
            )

        distinct = self._collapse_equivalent_events(scored)
        best = distinct[0]
        second = distinct[1] if len(distinct) > 1 else None
        if best.confidence < self._min_confidence:
            return self._result_from_candidate(
                best,
                accepted=False,
                status="fallback",
                reason="confidence_below_threshold",
                scored=distinct,
                base_kwargs=base_kwargs,
            )
        if second is not None and best.confidence - second.confidence < self._min_score_gap:
            return self._result_from_candidate(
                best,
                accepted=False,
                status="fallback",
                reason="ambiguous_candidates",
                scored=distinct,
                base_kwargs=base_kwargs,
                extra_reasons=("score_gap_below_threshold",),
            )
        return self._result_from_candidate(
            best,
            accepted=True,
            status="accepted",
            reason="unique_high_confidence_match",
            scored=distinct,
            base_kwargs=base_kwargs,
            extra_reasons=("unique_candidate_in_time_window",),
        )

    def _score_event(
        self,
        sport: str,
        participants: tuple[str, str],
        window_start_utc: datetime,
        window_end_utc: datetime,
        event: ExternalSportsEvent,
    ) -> ScoredEventCandidate | None:
        if canonical_sport(event.sport) != sport:
            return None
        starts_at = _utc(event.starts_at_utc)
        if not window_start_utc <= starts_at <= window_end_utc:
            return None
        if str(event.status or "").casefold() in REJECTED_STATUSES:
            return None

        signal_categories = (
            self._normalizer.categories(participants[0], sport),
            self._normalizer.categories(participants[1], sport),
        )
        event_categories = (
            self._normalizer.categories(event.participant_home, sport),
            self._normalizer.categories(event.participant_away, sport),
        )

        direct_categories = (
            _categories_compatible(signal_categories[0], event_categories[0])
            and _categories_compatible(signal_categories[1], event_categories[1])
        )
        reversed_categories = (
            _categories_compatible(signal_categories[0], event_categories[1])
            and _categories_compatible(signal_categories[1], event_categories[0])
        )
        if not direct_categories and not reversed_categories:
            return None

        signal_norm = tuple(self._normalizer.normalize(item) for item in participants)
        event_norm = (
            self._normalizer.normalize(event.participant_home),
            self._normalizer.normalize(event.participant_away),
        )
        direct_scores = (
            _participant_similarity(signal_norm[0], event_norm[0], sport),
            _participant_similarity(signal_norm[1], event_norm[1], sport),
        )
        reversed_scores = (
            _participant_similarity(signal_norm[0], event_norm[1], sport),
            _participant_similarity(signal_norm[1], event_norm[0], sport),
        )
        direct_average = sum(direct_scores) / 2 if direct_categories else 0.0
        reversed_average = sum(reversed_scores) / 2 if reversed_categories else 0.0
        reversed_order = reversed_average > direct_average
        selected_scores = reversed_scores if reversed_order else direct_scores
        if min(selected_scores) < self._participant_min_score:
            return None

        provider_quality = PROVIDER_QUALITY.get(event.provider, 0.80)
        confidence = 0.90 * (sum(selected_scores) / 2) + 0.08 * provider_quality + 0.02
        confidence = min(1.0, confidence)
        reasons = [
            "sport_exact_match",
            "participants_reversed" if reversed_order else "participants_direct",
            "both_participants_strong",
            "within_time_window",
            f"provider_quality_{provider_quality:.2f}",
        ]
        if selected_scores[0] == 1.0:
            reasons.append("participant_1_exact_or_alias")
        if selected_scores[1] == 1.0:
            reasons.append("participant_2_exact_or_alias")
        return ScoredEventCandidate(
            event=event,
            confidence=confidence,
            participant_1_score=selected_scores[0],
            participant_2_score=selected_scores[1],
            reversed_order=reversed_order,
            reasons=tuple(reasons),
            normalized_signal_participants=signal_norm,
            normalized_event_participants=event_norm,
        )

    def _find_material_time_conflict(
        self,
        candidates: list[ScoredEventCandidate],
    ) -> tuple[ScoredEventCandidate, ScoredEventCandidate] | None:
        strong = [
            candidate
            for candidate in candidates
            if candidate.confidence >= self._min_confidence
        ]
        for index, left in enumerate(strong):
            for right in strong[index + 1 :]:
                if not _same_normalized_pair(left, right):
                    continue
                difference = abs(
                    (_utc(left.event.starts_at_utc) - _utc(right.event.starts_at_utc)).total_seconds()
                )
                if left.event.provider != right.event.provider and difference > self._time_tolerance_seconds:
                    return left, right
        return None

    def _collapse_equivalent_events(
        self,
        candidates: list[ScoredEventCandidate],
    ) -> list[ScoredEventCandidate]:
        collapsed: list[ScoredEventCandidate] = []
        for candidate in candidates:
            equivalent_index = next(
                (
                    index
                    for index, existing in enumerate(collapsed)
                    if _same_normalized_pair(candidate, existing)
                    and abs(
                        (
                            _utc(candidate.event.starts_at_utc)
                            - _utc(existing.event.starts_at_utc)
                        ).total_seconds()
                    )
                    <= self._time_tolerance_seconds
                ),
                None,
            )
            if equivalent_index is None:
                collapsed.append(candidate)
                continue
            existing = collapsed[equivalent_index]
            if candidate.confidence > existing.confidence:
                chosen = candidate
            else:
                chosen = existing
            combined_reasons = tuple(
                dict.fromkeys((*chosen.reasons, "corroborated_by_multiple_sources"))
            )
            collapsed[equivalent_index] = replace(chosen, reasons=combined_reasons)
        collapsed.sort(key=lambda item: item.confidence, reverse=True)
        return collapsed

    @staticmethod
    def _result_from_candidate(
        candidate: ScoredEventCandidate,
        *,
        accepted: bool,
        status: str,
        reason: str,
        scored: list[ScoredEventCandidate],
        base_kwargs: dict[str, object],
        extra_reasons: tuple[str, ...] = (),
    ) -> EventMatchResult:
        second = scored[1].confidence if len(scored) > 1 else None
        return EventMatchResult(
            accepted=accepted,
            status=status,
            reason=reason,
            event=candidate.event,
            confidence=candidate.confidence,
            participant_1_score=candidate.participant_1_score,
            participant_2_score=candidate.participant_2_score,
            second_best_confidence=second,
            candidate_count=len(scored),
            reasons=tuple(dict.fromkeys((*candidate.reasons, *extra_reasons))),
            normalized_signal_participants=candidate.normalized_signal_participants,
            normalized_event_participants=candidate.normalized_event_participants,
            **base_kwargs,
        )


def _participant_similarity(left: str, right: str, sport: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_tokens = left.split()
    right_tokens = right.split()
    if sport == "tennis":
        tennis_score = _tennis_name_similarity(left_tokens, right_tokens)
        if tennis_score is not None:
            return tennis_score
    if sport == "basketball":
        basketball_score = _basketball_name_similarity(left_tokens, right_tokens)
        if basketball_score is not None:
            return basketball_score
    # Nomes muito curtos são perigosos quando não foram resolvidos por alias.
    if min(len(left), len(right)) <= 5:
        return min(0.80, fuzz.ratio(left, right) / 100.0)
    return max(
        fuzz.ratio(left, right),
        fuzz.token_sort_ratio(left, right),
    ) / 100.0


def _tennis_name_similarity(
    left_tokens: list[str],
    right_tokens: list[str],
) -> float | None:
    if not left_tokens or not right_tokens:
        return None
    # Sobrenome isolado é aceito apenas como sinal forte, nunca perfeito;
    # os demais critérios ainda exigem adversário, esporte, janela e unicidade.
    if len(left_tokens) == 1 and left_tokens[0] == right_tokens[-1]:
        return 0.93
    if len(right_tokens) == 1 and right_tokens[0] == left_tokens[-1]:
        return 0.93
    if left_tokens[-1] != right_tokens[-1]:
        return None
    left_first = left_tokens[0]
    right_first = right_tokens[0]
    if (
        (
            len(left_first) == 1
            and right_first.startswith(left_first)
        )
        or (
            len(right_first) == 1
            and left_first.startswith(right_first)
        )
    ):
        return 0.97
    return None


def _basketball_name_similarity(
    left_tokens: list[str],
    right_tokens: list[str],
) -> float | None:
    if not left_tokens or not right_tokens or left_tokens[-1] != right_tokens[-1]:
        return None
    if len(left_tokens) == 1 or len(right_tokens) == 1:
        return 0.93
    left_city = " ".join(left_tokens[:-1])
    right_city = " ".join(right_tokens[:-1])
    left_city = DEFAULT_BASKETBALL_CITY_ALIASES.get(left_city, left_city)
    right_city = DEFAULT_BASKETBALL_CITY_ALIASES.get(right_city, right_city)
    if left_city and left_city == right_city:
        return 0.96
    return None


def _categories_compatible(left: frozenset[str], right: frozenset[str]) -> bool:
    return left == right


def _same_normalized_pair(
    left: ScoredEventCandidate,
    right: ScoredEventCandidate,
) -> bool:
    return set(left.normalized_event_participants) == set(right.normalized_event_participants)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Datetime de matching precisa possuir timezone.")
    return value.astimezone(timezone.utc)
