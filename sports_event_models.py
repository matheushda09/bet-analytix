"""Modelos internos independentes dos providers de agenda esportiva."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ExternalSportsEvent:
    """Evento normalizado retornado por qualquer provider externo."""

    provider: str
    external_event_id: str
    sport: str
    participant_home: str
    participant_away: str
    starts_at_utc: datetime
    competition: str | None = None
    country: str | None = None
    status: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    fetched_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_cache_dict(self, include_raw_payload: bool = True) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "external_event_id": self.external_event_id,
            "sport": self.sport,
            "participant_home": self.participant_home,
            "participant_away": self.participant_away,
            "starts_at_utc": _as_utc(self.starts_at_utc).isoformat(),
            "competition": self.competition,
            "country": self.country,
            "status": self.status,
            "raw_payload": self.raw_payload if include_raw_payload else {},
            "fetched_at_utc": _as_utc(self.fetched_at_utc).isoformat(),
        }


@dataclass(frozen=True)
class ScoredEventCandidate:
    """Candidato pontuado de maneira determinística e auditável."""

    event: ExternalSportsEvent
    confidence: float
    participant_1_score: float
    participant_2_score: float
    reversed_order: bool
    reasons: tuple[str, ...]
    normalized_signal_participants: tuple[str, str]
    normalized_event_participants: tuple[str, str]


@dataclass(frozen=True)
class EventMatchResult:
    """Resultado final do matching, aceito ou rejeitado."""

    accepted: bool
    status: str
    reason: str
    sport: str | None
    participants: tuple[str, str] | None
    window_start_utc: datetime
    window_end_utc: datetime
    event: ExternalSportsEvent | None = None
    confidence: float = 0.0
    participant_1_score: float = 0.0
    participant_2_score: float = 0.0
    second_best_confidence: float | None = None
    candidate_count: int = 0
    providers_consulted: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    normalized_signal_participants: tuple[str, str] | None = None
    normalized_event_participants: tuple[str, str] | None = None
    from_cache: bool = False

    def as_audit_dict(
        self,
        *,
        source_bet_id: int,
        mode: str,
        fallback_datetime_utc: datetime,
    ) -> dict[str, Any]:
        event = self.event
        return {
            "source_bet_id": source_bet_id,
            "mode": mode,
            "match_status": self.status,
            "match_reason": self.reason,
            "sport": self.sport,
            "signal_participants": list(self.participants) if self.participants else None,
            "normalized_signal_participants": (
                list(self.normalized_signal_participants)
                if self.normalized_signal_participants
                else None
            ),
            "provider": event.provider if event else None,
            "external_event_id": event.external_event_id if event else None,
            "participant_home": event.participant_home if event else None,
            "participant_away": event.participant_away if event else None,
            "normalized_event_participants": (
                list(self.normalized_event_participants)
                if self.normalized_event_participants
                else None
            ),
            "competition": event.competition if event else None,
            "country": event.country if event else None,
            "starts_at_utc": _as_utc(event.starts_at_utc).isoformat() if event else None,
            "event_status": event.status if event else None,
            "confidence": round(self.confidence, 6),
            "participant_1_score": round(self.participant_1_score, 6),
            "participant_2_score": round(self.participant_2_score, 6),
            "second_best_confidence": (
                round(self.second_best_confidence, 6)
                if self.second_best_confidence is not None
                else None
            ),
            "candidate_count": self.candidate_count,
            "providers_consulted": list(self.providers_consulted),
            "reasons": list(self.reasons),
            "from_cache": self.from_cache,
            "fallback_datetime_utc": _as_utc(fallback_datetime_utc).isoformat(),
            "raw_payload": event.raw_payload if event else {},
        }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Datetime esportivo precisa possuir timezone.")
    return value.astimezone(timezone.utc)
