"""Orquestra cache, providers e matching sem bloquear o fluxo principal."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sports_event_config import SportsEventSettings
from sports_event_matching import (
    EventMatcher,
    canonical_sport,
    split_composite_event_legs,
    split_event_participants,
)
from sports_event_models import EventMatchResult, ExternalSportsEvent
from sports_schedule_providers import (
    SportsProviderError,
    SportsScheduleProvider,
    build_sports_schedule_providers,
)
from sports_schedule_store import SportsScheduleStore


logger = logging.getLogger(__name__)


class SportsEventService:
    """Serviço central da identificação e reconsulta de eventos."""

    def __init__(
        self,
        settings: SportsEventSettings,
        store: SportsScheduleStore,
        providers: dict[str, SportsScheduleProvider] | None = None,
    ) -> None:
        self.settings = settings
        self._store = store
        self._providers = (
            providers
            if providers is not None
            else build_sports_schedule_providers(settings, store)
        )
        self._matcher = EventMatcher(
            min_confidence=settings.min_confidence,
            min_score_gap=settings.min_score_gap,
            participant_min_score=settings.participant_min_score,
            time_tolerance_minutes=settings.time_tolerance_minutes,
            participant_aliases=settings.participant_aliases,
        )
        self._owner = f"{os.getpid()}:{uuid.uuid4().hex}"

    @property
    def available_providers(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def resolve_event(
        self,
        *,
        sport: str,
        event_name: str,
        received_at_utc: datetime,
    ) -> EventMatchResult:
        received_at = _utc(received_at_utc)
        window_start = received_at - timedelta(hours=self.settings.lookback_hours)
        window_end = received_at + timedelta(days=self.settings.lookahead_days)
        canonical = canonical_sport(sport)
        reference_event_name = event_name
        composite_legs = split_composite_event_legs(event_name)
        participants = split_event_participants(reference_event_name)
        if participants is None and composite_legs is not None:
            reference_event_name = composite_legs[0]
            participants = split_event_participants(reference_event_name)
        if canonical is None or participants is None:
            result = self._matcher.match(
                sport=sport,
                event_name=reference_event_name,
                window_start_utc=window_start,
                window_end_utc=window_end,
                events=[],
            )
            result = self._with_composite_context(result, composite_legs)
            self._log_result(result, event_name, reference_event_name)
            return result

        provider_order = self.settings.providers_for_sport(canonical)
        available_order = tuple(
            provider_name
            for provider_name in provider_order
            if provider_name in self._providers
            and canonical in self._providers[provider_name].supported_sports
        )
        if not available_order:
            result = self._matcher.match(
                sport=sport,
                event_name=reference_event_name,
                window_start_utc=window_start,
                window_end_utc=window_end,
                events=[],
            )
            result = self._with_composite_context(result, composite_legs)
            self._log_result(result, event_name, reference_event_name)
            return result
        cached_events = self._store.list_events(
            sport=canonical,
            start_at_utc=window_start,
            end_at_utc=window_end,
            providers=available_order,
        )
        cached_result = self._matcher.match(
            sport=sport,
            event_name=reference_event_name,
            window_start_utc=window_start,
            window_end_utc=window_end,
            events=cached_events,
            providers_consulted=(),
            from_cache=True,
        )
        if cached_result.accepted:
            cached_result = self._with_composite_context(
                cached_result,
                composite_legs,
            )
            self._log_result(
                cached_result,
                event_name,
                reference_event_name,
            )
            return cached_result

        deadline = time.monotonic() + self.settings.total_timeout_seconds
        consulted: list[str] = []
        for provider_name in available_order:
            if time.monotonic() >= deadline:
                break
            provider = self._providers[provider_name]
            query_key = self._query_key(
                provider,
                canonical,
                participants,
                window_start,
                window_end,
            )
            fresh_query = self._store.get_fresh_query(provider_name, query_key)
            if fresh_query is not None:
                consulted.append(f"{provider_name}:cache")
            else:
                fetched = self._fetch_provider_once(
                    provider,
                    query_key=query_key,
                    sport=canonical,
                    participants=participants,
                    window_start=window_start,
                    window_end=window_end,
                    deadline=deadline,
                )
                if fetched:
                    consulted.append(provider_name)

            accumulated = self._store.list_events(
                sport=canonical,
                start_at_utc=window_start,
                end_at_utc=window_end,
                providers=available_order,
            )
            result = self._matcher.match(
                sport=sport,
                event_name=reference_event_name,
                window_start_utc=window_start,
                window_end_utc=window_end,
                events=accumulated,
                providers_consulted=tuple(consulted),
                from_cache=fresh_query is not None,
            )
            if result.accepted:
                result = self._with_composite_context(result, composite_legs)
                self._log_result(result, event_name, reference_event_name)
                return result

        final_events = self._store.list_events(
            sport=canonical,
            start_at_utc=window_start,
            end_at_utc=window_end,
            providers=available_order,
        )
        result = self._matcher.match(
            sport=sport,
            event_name=reference_event_name,
            window_start_utc=window_start,
            window_end_utc=window_end,
            events=final_events,
            providers_consulted=tuple(consulted),
            from_cache=False,
        )
        result = self._with_composite_context(result, composite_legs)
        self._log_result(result, event_name, reference_event_name)
        return result

    def refresh_event(
        self,
        *,
        provider_name: str,
        external_event_id: str,
    ) -> ExternalSportsEvent | None:
        provider = self._providers.get(provider_name)
        if provider is None:
            return None
        query_key = f"id:{external_event_id}"
        cached_query = self._store.get_fresh_query(provider_name, query_key)
        if cached_query is not None:
            return self._store.get_fresh_event(provider_name, external_event_id)

        deadline = time.monotonic() + self.settings.total_timeout_seconds
        lock_key = f"{provider_name}:{query_key}"
        if not self._store.acquire_fetch_lock(
            lock_key,
            self._owner,
            self.settings.lock_ttl_seconds,
        ):
            self._wait_for_query(provider_name, query_key, deadline)
            return self._store.get_fresh_event(provider_name, external_event_id)

        try:
            event = provider.get_event(external_event_id, deadline=deadline)
            ttl = min(
                self.settings.cache_ttl_seconds,
                self.settings.recheck_within_24h_seconds,
            )
            if event is not None:
                self._store.upsert_events(
                    [event],
                    ttl_seconds=ttl,
                    include_raw_payload=self.settings.store_raw_payload,
                )
            self._store.record_query(
                provider=provider_name,
                query_key=query_key,
                status="success" if event else "empty",
                event_count=1 if event else 0,
                ttl_seconds=ttl if event else self.settings.negative_cache_ttl_seconds,
            )
            return event
        except SportsProviderError as exc:
            self._store.record_query(
                provider=provider_name,
                query_key=query_key,
                status="error",
                event_count=0,
                ttl_seconds=self.settings.negative_cache_ttl_seconds,
                error=str(exc),
            )
            logger.warning(
                "sports_event_refresh provider=%s external_event_id=%s result=provider_error error_type=%s",
                provider_name,
                external_event_id,
                type(exc).__name__,
            )
            return None
        finally:
            self._store.release_fetch_lock(lock_key, self._owner)

    def next_recheck_timestamp(
        self,
        starts_at_utc: datetime,
        status: str | None,
    ) -> int | None:
        normalized_status = str(status or "").casefold()
        if normalized_status in {
            "finished",
            "ft",
            "completed",
            "cancelled",
            "canceled",
            "canc",
            "abandoned",
            "abd",
        }:
            return None
        now = datetime.now(timezone.utc)
        distance = _utc(starts_at_utc) - now
        if distance <= timedelta(hours=24):
            delay = self.settings.recheck_within_24h_seconds
        elif distance <= timedelta(days=7):
            delay = self.settings.recheck_within_7d_seconds
        else:
            delay = self.settings.recheck_far_seconds
        return int(time.time()) + delay

    def metrics_snapshot(self) -> dict[str, object]:
        return {
            "providers": self._store.provider_metrics(),
            "available_providers": list(self.available_providers),
            "mode": self.settings.mode,
        }

    def _fetch_provider_once(
        self,
        provider: SportsScheduleProvider,
        *,
        query_key: str,
        sport: str,
        participants: tuple[str, str],
        window_start: datetime,
        window_end: datetime,
        deadline: float,
    ) -> bool:
        lock_key = f"{provider.name}:{query_key}"
        if not self._store.acquire_fetch_lock(
            lock_key,
            self._owner,
            self.settings.lock_ttl_seconds,
        ):
            return self._wait_for_query(provider.name, query_key, deadline)
        try:
            events = provider.search_events(
                sport=sport,
                participants=participants,
                start_at_utc=window_start,
                end_at_utc=window_end,
                deadline=deadline,
            )
            ttl = (
                self.settings.cache_ttl_seconds
                if events
                else self.settings.negative_cache_ttl_seconds
            )
            self._store.upsert_events(
                events,
                ttl_seconds=ttl,
                include_raw_payload=self.settings.store_raw_payload,
            )
            self._store.record_query(
                provider=provider.name,
                query_key=query_key,
                status="success" if events else "empty",
                event_count=len(events),
                ttl_seconds=ttl,
            )
            logger.info(
                "sports_schedule_fetch provider=%s sport=%s events=%s source=external",
                provider.name,
                sport,
                len(events),
            )
            return True
        except SportsProviderError as exc:
            self._store.record_query(
                provider=provider.name,
                query_key=query_key,
                status="error",
                event_count=0,
                ttl_seconds=self.settings.negative_cache_ttl_seconds,
                error=str(exc),
            )
            logger.warning(
                "sports_schedule_fetch provider=%s sport=%s result=provider_error error_type=%s",
                provider.name,
                sport,
                type(exc).__name__,
            )
            return False
        except Exception as exc:
            self._store.record_query(
                provider=provider.name,
                query_key=query_key,
                status="error",
                event_count=0,
                ttl_seconds=self.settings.negative_cache_ttl_seconds,
                error=f"{type(exc).__name__}: {exc}",
            )
            logger.exception(
                "sports_schedule_fetch provider=%s sport=%s result=unexpected_error",
                provider.name,
                sport,
            )
            return False
        finally:
            self._store.release_fetch_lock(lock_key, self._owner)

    def _wait_for_query(
        self,
        provider_name: str,
        query_key: str,
        deadline: float,
    ) -> bool:
        wait_deadline = min(
            deadline,
            time.monotonic() + self.settings.lock_wait_seconds,
        )
        while time.monotonic() < wait_deadline:
            if self._store.get_fresh_query(provider_name, query_key) is not None:
                return True
            time.sleep(0.1)
        return False

    @staticmethod
    def _query_key(
        provider: SportsScheduleProvider,
        sport: str,
        participants: tuple[str, str],
        window_start: datetime,
        window_end: datetime,
    ) -> str:
        parts = [
            sport,
            _utc(window_start).date().isoformat(),
            _utc(window_end).date().isoformat(),
        ]
        cache_version = getattr(provider, "cache_version", None)
        if cache_version:
            parts.append(str(cache_version))
        if provider.cache_scope == "participants":
            parts.extend(participants)
        digest = hashlib.sha256("|".join(parts).casefold().encode("utf-8")).hexdigest()
        return f"search:{digest}"

    @staticmethod
    def _with_composite_context(
        result: EventMatchResult,
        composite_legs: tuple[str, ...] | None,
    ) -> EventMatchResult:
        if composite_legs is None:
            return result
        return replace(
            result,
            reasons=tuple(
                dict.fromkeys(
                    (
                        *result.reasons,
                        "composite_event_first_leg_reference",
                    )
                )
            ),
        )

    @staticmethod
    def _log_result(
        result: EventMatchResult,
        event_name: str,
        reference_event_name: str,
    ) -> None:
        payload = {
            "action": "sports_event_match",
            "sport": result.sport,
            "event_name": event_name,
            "reference_event_name": (
                reference_event_name
                if reference_event_name != event_name
                else None
            ),
            "participants": list(result.participants) if result.participants else None,
            "normalized_participants": (
                list(result.normalized_signal_participants)
                if result.normalized_signal_participants
                else None
            ),
            "providers_consulted": list(result.providers_consulted),
            "candidates_found": result.candidate_count,
            "provider": result.event.provider if result.event else None,
            "external_event_id": (
                result.event.external_event_id if result.event else None
            ),
            "confidence": round(result.confidence, 4),
            "result": result.status,
            "reason": result.reason,
            "from_cache": result.from_cache,
        }
        logger.info(
            "sports_event_match %s",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Datetime do serviço esportivo precisa possuir timezone.")
    return value.astimezone(timezone.utc)
