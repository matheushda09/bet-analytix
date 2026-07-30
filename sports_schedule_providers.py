"""Adaptadores desacoplados das APIs externas de agenda esportiva."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

from sports_event_config import SportsEventSettings
from sports_event_matching import ParticipantNormalizer, canonical_sport
from sports_event_models import ExternalSportsEvent
from sports_schedule_store import SportsScheduleStore


logger = logging.getLogger(__name__)
PROVIDER_PARTICIPANT_NORMALIZER = ParticipantNormalizer()


class SportsProviderError(RuntimeError):
    """Erro controlado de um provider que nunca deve interromper a aposta."""


class SportsProviderRateLimited(SportsProviderError):
    """Cota local/remota ou circuit breaker impediu a consulta."""


class SportsScheduleProvider(ABC):
    name: str
    supported_sports: frozenset[str]
    cache_scope: str = "window"

    def __init__(
        self,
        settings: SportsEventSettings,
        store: SportsScheduleStore,
        session: requests.Session | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._session = session or requests.Session()

    @abstractmethod
    def search_events(
        self,
        *,
        sport: str,
        participants: tuple[str, str],
        start_at_utc: datetime,
        end_at_utc: datetime,
        deadline: float,
    ) -> list[ExternalSportsEvent]:
        raise NotImplementedError

    @abstractmethod
    def get_event(
        self,
        external_event_id: str,
        *,
        deadline: float,
    ) -> ExternalSportsEvent | None:
        raise NotImplementedError

    def _request_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        deadline: float,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._settings.request_max_retries + 1):
            if time.monotonic() >= deadline:
                raise SportsProviderError(f"{self.name}: tempo total de consulta esgotado")

            reservation = self._store.reserve_provider_call(
                self.name,
                minute_limit=self._settings.provider_minute_limits.get(self.name, 0),
                daily_limit=self._settings.provider_daily_limits.get(self.name, 0),
            )
            if reservation is None:
                raise SportsProviderRateLimited(
                    f"{self.name}: cota local ou circuit breaker ativo"
                )

            started = time.monotonic()
            response: requests.Response | None = None
            try:
                remaining = max(0.5, deadline - time.monotonic())
                timeout = min(self._settings.request_timeout_seconds, remaining)
                response = self._session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=timeout,
                )
                duration_ms = int((time.monotonic() - started) * 1000)
                if response.status_code == 429:
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                    self._store.complete_provider_call(
                        reservation,
                        success=False,
                        status_code=429,
                        duration_ms=duration_ms,
                        error="HTTP 429",
                        block_seconds=retry_after,
                    )
                    raise SportsProviderRateLimited(
                        f"{self.name}: HTTP 429; bloqueado temporariamente"
                    )
                if response.status_code in {401, 403}:
                    self._store.complete_provider_call(
                        reservation,
                        success=False,
                        status_code=response.status_code,
                        duration_ms=duration_ms,
                        error=f"HTTP {response.status_code}",
                        block_seconds=3600,
                    )
                    raise SportsProviderError(
                        f"{self.name}: credencial ausente, inválida ou plano incompatível"
                    )
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    self._store.complete_provider_call(
                        reservation,
                        success=False,
                        status_code=response.status_code,
                        duration_ms=duration_ms,
                        error="JSON inválido",
                        block_seconds=30,
                    )
                    raise SportsProviderError(f"{self.name}: JSON inválido") from exc
                self._store.complete_provider_call(
                    reservation,
                    success=True,
                    status_code=response.status_code,
                    duration_ms=duration_ms,
                )
                return payload
            except SportsProviderRateLimited:
                raise
            except SportsProviderError:
                raise
            except requests.RequestException as exc:
                last_error = exc
                duration_ms = int((time.monotonic() - started) * 1000)
                status_code = response.status_code if response is not None else None
                retryable = status_code is None or status_code >= 500
                block_seconds = min(900, 15 * (2**attempt)) if retryable else 300
                self._store.complete_provider_call(
                    reservation,
                    success=False,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    error=type(exc).__name__,
                    block_seconds=block_seconds if attempt >= self._settings.request_max_retries else 0,
                )
                if not retryable or attempt >= self._settings.request_max_retries:
                    break
                delay = self._settings.backoff_initial_seconds * (2**attempt)
                if time.monotonic() + delay >= deadline:
                    break
                time.sleep(delay)
        raise SportsProviderError(
            f"{self.name}: falha temporária após tentativas ({type(last_error).__name__ if last_error else 'erro HTTP'})"
        )


class ApiFootballProvider(SportsScheduleProvider):
    name = "api_football"
    supported_sports = frozenset({"football"})

    def __init__(
        self,
        settings: SportsEventSettings,
        store: SportsScheduleStore,
        api_key: str,
        session: requests.Session | None = None,
    ) -> None:
        super().__init__(settings, store, session)
        self._api_key = api_key

    def search_events(
        self,
        *,
        sport: str,
        participants: tuple[str, str],
        start_at_utc: datetime,
        end_at_utc: datetime,
        deadline: float,
    ) -> list[ExternalSportsEvent]:
        if sport != "football":
            return []
        events: dict[str, ExternalSportsEvent] = {}
        for query_date in _dates_between(start_at_utc, end_at_utc):
            payload = self._request_json(
                f"{self._settings.api_football_base_url}/fixtures",
                headers={"x-apisports-key": self._api_key, "accept": "application/json"},
                params={"date": query_date.isoformat(), "timezone": "UTC"},
                deadline=deadline,
            )
            for item in _dict_list(payload, "response"):
                event = self._parse_event(item)
                if event is not None:
                    events[event.external_event_id] = event
        return list(events.values())

    def get_event(
        self,
        external_event_id: str,
        *,
        deadline: float,
    ) -> ExternalSportsEvent | None:
        payload = self._request_json(
            f"{self._settings.api_football_base_url}/fixtures",
            headers={"x-apisports-key": self._api_key, "accept": "application/json"},
            params={"id": external_event_id, "timezone": "UTC"},
            deadline=deadline,
        )
        items = _dict_list(payload, "response")
        return self._parse_event(items[0]) if items else None

    def _parse_event(self, item: dict[str, Any]) -> ExternalSportsEvent | None:
        fixture = _dict(item.get("fixture"))
        teams = _dict(item.get("teams"))
        home = _dict(teams.get("home"))
        away = _dict(teams.get("away"))
        league = _dict(item.get("league"))
        status = _dict(fixture.get("status"))
        starts_at = _datetime_from_timestamp_or_iso(
            fixture.get("timestamp"),
            fixture.get("date"),
        )
        if starts_at is None:
            return None
        event_id = fixture.get("id")
        home_name = _text(home.get("name"))
        away_name = _text(away.get("name"))
        if event_id is None or not home_name or not away_name:
            return None
        return ExternalSportsEvent(
            provider=self.name,
            external_event_id=str(event_id),
            sport="football",
            participant_home=home_name,
            participant_away=away_name,
            starts_at_utc=starts_at,
            competition=_text(league.get("name")),
            country=_text(league.get("country")),
            status=_text(status.get("short")) or _text(status.get("long")),
            raw_payload=item,
        )


class FootballDataProvider(SportsScheduleProvider):
    name = "football_data"
    supported_sports = frozenset({"football"})

    def __init__(
        self,
        settings: SportsEventSettings,
        store: SportsScheduleStore,
        api_key: str,
        session: requests.Session | None = None,
    ) -> None:
        super().__init__(settings, store, session)
        self._api_key = api_key

    def search_events(
        self,
        *,
        sport: str,
        participants: tuple[str, str],
        start_at_utc: datetime,
        end_at_utc: datetime,
        deadline: float,
    ) -> list[ExternalSportsEvent]:
        if sport != "football":
            return []
        payload = self._request_json(
            f"{self._settings.football_data_base_url}/matches",
            headers={"X-Auth-Token": self._api_key, "accept": "application/json"},
            params={
                "dateFrom": _utc(start_at_utc).date().isoformat(),
                "dateTo": _utc(end_at_utc).date().isoformat(),
            },
            deadline=deadline,
        )
        return [
            event
            for item in _dict_list(payload, "matches")
            if (event := self._parse_event(item)) is not None
        ]

    def get_event(
        self,
        external_event_id: str,
        *,
        deadline: float,
    ) -> ExternalSportsEvent | None:
        payload = self._request_json(
            f"{self._settings.football_data_base_url}/matches/{external_event_id}",
            headers={"X-Auth-Token": self._api_key, "accept": "application/json"},
            deadline=deadline,
        )
        return self._parse_event(payload) if isinstance(payload, dict) else None

    def _parse_event(self, item: dict[str, Any]) -> ExternalSportsEvent | None:
        home = _dict(item.get("homeTeam"))
        away = _dict(item.get("awayTeam"))
        competition = _dict(item.get("competition"))
        area = _dict(item.get("area")) or _dict(competition.get("area"))
        starts_at = _parse_datetime(item.get("utcDate"))
        event_id = item.get("id")
        home_name = _team_name(home)
        away_name = _team_name(away)
        if starts_at is None or event_id is None or not home_name or not away_name:
            return None
        return ExternalSportsEvent(
            provider=self.name,
            external_event_id=str(event_id),
            sport="football",
            participant_home=home_name,
            participant_away=away_name,
            starts_at_utc=starts_at,
            competition=_text(competition.get("name")),
            country=_text(area.get("name")),
            status=_text(item.get("status")),
            raw_payload=item,
        )


class ApiBasketballProvider(SportsScheduleProvider):
    name = "api_basketball"
    supported_sports = frozenset({"basketball"})

    def __init__(
        self,
        settings: SportsEventSettings,
        store: SportsScheduleStore,
        api_key: str,
        session: requests.Session | None = None,
    ) -> None:
        super().__init__(settings, store, session)
        self._api_key = api_key

    def search_events(
        self,
        *,
        sport: str,
        participants: tuple[str, str],
        start_at_utc: datetime,
        end_at_utc: datetime,
        deadline: float,
    ) -> list[ExternalSportsEvent]:
        if sport != "basketball":
            return []
        events: dict[str, ExternalSportsEvent] = {}
        for query_date in _dates_between(start_at_utc, end_at_utc):
            payload = self._request_json(
                f"{self._settings.api_basketball_base_url}/games",
                headers={"x-apisports-key": self._api_key, "accept": "application/json"},
                params={"date": query_date.isoformat(), "timezone": "UTC"},
                deadline=deadline,
            )
            for item in _dict_list(payload, "response"):
                event = self._parse_event(item)
                if event is not None:
                    events[event.external_event_id] = event
        return list(events.values())

    def get_event(
        self,
        external_event_id: str,
        *,
        deadline: float,
    ) -> ExternalSportsEvent | None:
        payload = self._request_json(
            f"{self._settings.api_basketball_base_url}/games",
            headers={"x-apisports-key": self._api_key, "accept": "application/json"},
            params={"id": external_event_id, "timezone": "UTC"},
            deadline=deadline,
        )
        items = _dict_list(payload, "response")
        return self._parse_event(items[0]) if items else None

    def _parse_event(self, item: dict[str, Any]) -> ExternalSportsEvent | None:
        teams = _dict(item.get("teams"))
        home = _dict(teams.get("home"))
        away = _dict(teams.get("away"))
        league = _dict(item.get("league"))
        country = _dict(item.get("country"))
        status = _dict(item.get("status"))
        starts_at = _datetime_from_timestamp_or_iso(
            item.get("timestamp"),
            item.get("date"),
        )
        event_id = item.get("id")
        home_name = _text(home.get("name"))
        away_name = _text(away.get("name"))
        if starts_at is None or event_id is None or not home_name or not away_name:
            return None
        return ExternalSportsEvent(
            provider=self.name,
            external_event_id=str(event_id),
            sport="basketball",
            participant_home=home_name,
            participant_away=away_name,
            starts_at_utc=starts_at,
            competition=_text(league.get("name")),
            country=_text(country.get("name")) or _text(country.get("code")),
            status=_text(status.get("short")) or _text(status.get("long")),
            raw_payload=item,
        )


class LiveTennisProvider(SportsScheduleProvider):
    name = "live_tennis"
    supported_sports = frozenset({"tennis"})

    def __init__(
        self,
        settings: SportsEventSettings,
        store: SportsScheduleStore,
        api_key: str,
        session: requests.Session | None = None,
    ) -> None:
        super().__init__(settings, store, session)
        self._api_key = api_key

    def search_events(
        self,
        *,
        sport: str,
        participants: tuple[str, str],
        start_at_utc: datetime,
        end_at_utc: datetime,
        deadline: float,
    ) -> list[ExternalSportsEvent]:
        if sport != "tennis":
            return []
        events: dict[str, ExternalSportsEvent] = {}
        for status in ("upcoming", "live"):
            offset = 0
            for _ in range(5):
                payload = self._request_json(
                    f"{self._settings.live_tennis_base_url}/matches",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "accept": "application/json",
                    },
                    params={"status": status, "limit": 200, "offset": offset},
                    deadline=deadline,
                )
                items = _dict_list(payload, "data")
                for item in items:
                    event = self._parse_event(item)
                    if event is None:
                        continue
                    if _utc(start_at_utc) <= event.starts_at_utc <= _utc(end_at_utc):
                        events[event.external_event_id] = event
                meta = _dict(payload.get("meta")) if isinstance(payload, dict) else {}
                count = _int_or_zero(meta.get("count"))
                offset += len(items)
                if not items or offset >= count:
                    break
            else:
                raise SportsProviderError(
                    "live_tennis: paginação excedeu limite seguro de 1000 partidas"
                )
        return list(events.values())

    def get_event(
        self,
        external_event_id: str,
        *,
        deadline: float,
    ) -> ExternalSportsEvent | None:
        payload = self._request_json(
            f"{self._settings.live_tennis_base_url}/matches/{external_event_id}",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "accept": "application/json",
            },
            deadline=deadline,
        )
        return self._parse_event(payload) if isinstance(payload, dict) else None

    def _parse_event(self, item: dict[str, Any]) -> ExternalSportsEvent | None:
        players = _dict(item.get("players"))
        player_1 = _dict(players.get("p1")) or _dict(players.get("player1"))
        player_2 = _dict(players.get("p2")) or _dict(players.get("player2"))
        starts_at = _parse_datetime(item.get("scheduled_time"))
        event_id = item.get("id")
        name_1 = _text(player_1.get("name"))
        name_2 = _text(player_2.get("name"))
        if starts_at is None or event_id is None or not name_1 or not name_2:
            return None
        return ExternalSportsEvent(
            provider=self.name,
            external_event_id=str(event_id),
            sport="tennis",
            participant_home=name_1,
            participant_away=name_2,
            starts_at_utc=starts_at,
            competition=_text(item.get("tournament")),
            country=None,
            status=_text(item.get("status")),
            raw_payload=item,
        )


class TheSportsDbProvider(SportsScheduleProvider):
    name = "thesportsdb"
    supported_sports = frozenset({"football", "basketball", "tennis"})
    cache_scope = "participants"
    cache_version = "team-schedule-v2"

    def search_events(
        self,
        *,
        sport: str,
        participants: tuple[str, str],
        start_at_utc: datetime,
        end_at_utc: datetime,
        deadline: float,
    ) -> list[ExternalSportsEvent]:
        events_by_id: dict[str, ExternalSportsEvent] = {}
        team_ids: dict[str, str] = {}
        query_participants = tuple(
            PROVIDER_PARTICIPANT_NORMALIZER.normalize(participant) or participant
            for participant in participants
        )
        # A ordem no sinal nem sempre é mandante x visitante. O endpoint de
        # busca direta do TheSportsDB é consultado nas duas ordens para que o
        # matcher possa decidir com os mesmos critérios conservadores.
        for first, second in (
            query_participants,
            tuple(reversed(query_participants)),
        ):
            query = f"{first}_vs_{second}".replace(" ", "_")
            payload = self._request_json(
                (
                    f"{self._settings.thesportsdb_base_url}/"
                    f"{self._settings.thesportsdb_api_key}/searchevents.php"
                ),
                params={"e": query},
                deadline=deadline,
            )
            self._collect_team_ids(
                payload,
                sport=sport,
                participants=participants,
                destination=team_ids,
            )
            for event in self._events_from_payload(payload):
                events_by_id[event.external_event_id] = event

        candidates = self._events_in_window(
            events_by_id.values(),
            sport=sport,
            start_at_utc=start_at_utc,
            end_at_utc=end_at_utc,
        )
        if _contains_exact_participant_pair(candidates, participants):
            return candidates

        # A chave gratuita limita a busca por nome e pode devolver apenas uma
        # edição histórica do confronto. A agenda por time é usada somente
        # quando não existe candidato dentro da janela solicitada.
        for participant in query_participants:
            participant_key = _provider_name_key(participant)
            if participant_key in team_ids:
                continue
            payload = self._request_json(
                (
                    f"{self._settings.thesportsdb_base_url}/"
                    f"{self._settings.thesportsdb_api_key}/searchteams.php"
                ),
                params={"t": participant},
                deadline=deadline,
            )
            team_id = self._team_id_from_search(
                payload,
                sport=sport,
                participant=participant,
            )
            if team_id is not None:
                team_ids[participant_key] = team_id

        queried_schedules: set[str] = set()
        for endpoint in ("eventsnext.php", "eventslast.php"):
            for participant in query_participants:
                team_id = team_ids.get(_provider_name_key(participant))
                if team_id is None:
                    continue
                query_identity = f"{endpoint}:{team_id}"
                if query_identity in queried_schedules:
                    continue
                queried_schedules.add(query_identity)
                payload = self._request_json(
                    (
                        f"{self._settings.thesportsdb_base_url}/"
                        f"{self._settings.thesportsdb_api_key}/{endpoint}"
                    ),
                    params={"id": team_id},
                    deadline=deadline,
                )
                for event in self._events_from_payload(payload):
                    events_by_id[event.external_event_id] = event

                candidates = self._events_in_window(
                    events_by_id.values(),
                    sport=sport,
                    start_at_utc=start_at_utc,
                    end_at_utc=end_at_utc,
                )
                if _contains_exact_participant_pair(candidates, participants):
                    return candidates

        # A chave gratuita retorna somente o próximo jogo em casa de cada
        # equipe. Se houver outra competição antes do confronto procurado,
        # esse recorte não alcança o evento mesmo quando ele já existe na
        # base. A busca nominal com filtro de data continua sendo gratuita e
        # permite varrer a pequena janela configurada sem aceitar um homônimo.
        for event_date in _dates_between(start_at_utc, end_at_utc):
            for first, second in (
                query_participants,
                tuple(reversed(query_participants)),
            ):
                query = f"{first}_vs_{second}".replace(" ", "_")
                payload = self._request_json(
                    (
                        f"{self._settings.thesportsdb_base_url}/"
                        f"{self._settings.thesportsdb_api_key}/searchevents.php"
                    ),
                    params={"e": query, "d": event_date.isoformat()},
                    deadline=deadline,
                )
                for event in self._events_from_payload(payload):
                    events_by_id[event.external_event_id] = event

                candidates = self._events_in_window(
                    events_by_id.values(),
                    sport=sport,
                    start_at_utc=start_at_utc,
                    end_at_utc=end_at_utc,
                )
                if _contains_exact_participant_pair(candidates, participants):
                    return candidates

        return self._events_in_window(
            events_by_id.values(),
            sport=sport,
            start_at_utc=start_at_utc,
            end_at_utc=end_at_utc,
        )

    @staticmethod
    def _events_in_window(
        events: Any,
        *,
        sport: str,
        start_at_utc: datetime,
        end_at_utc: datetime,
    ) -> list[ExternalSportsEvent]:
        return [
            event
            for event in events
            if event.sport == sport
            and _utc(start_at_utc) <= event.starts_at_utc <= _utc(end_at_utc)
        ]

    def _collect_team_ids(
        self,
        payload: Any,
        *,
        sport: str,
        participants: tuple[str, str],
        destination: dict[str, str],
    ) -> None:
        participant_keys = {_provider_name_key(item) for item in participants}
        for item in _thesportsdb_items(payload):
            if canonical_sport(_text(item.get("strSport")) or "") != sport:
                continue
            for name_field, id_field in (
                ("strHomeTeam", "idHomeTeam"),
                ("strAwayTeam", "idAwayTeam"),
            ):
                team_name = _text(item.get(name_field))
                team_id = _text(item.get(id_field))
                team_key = _provider_name_key(team_name or "")
                if team_id and team_key in participant_keys:
                    destination[team_key] = team_id

    @staticmethod
    def _team_id_from_search(
        payload: Any,
        *,
        sport: str,
        participant: str,
    ) -> str | None:
        if not isinstance(payload, dict) or not isinstance(payload.get("teams"), list):
            return None
        participant_key = _provider_name_key(participant)
        for item in payload["teams"]:
            if not isinstance(item, dict):
                continue
            if canonical_sport(_text(item.get("strSport")) or "") != sport:
                continue
            names = [
                _text(item.get("strTeam")),
                _text(item.get("strTeamShort")),
                *str(item.get("strTeamAlternate") or "").split(","),
            ]
            normalized_names = {
                _provider_name_key(name)
                for name in names
                if name is not None and str(name).strip()
            }
            if participant_key not in normalized_names:
                continue
            team_id = _text(item.get("idTeam"))
            if team_id:
                return team_id
        return None

    def get_event(
        self,
        external_event_id: str,
        *,
        deadline: float,
    ) -> ExternalSportsEvent | None:
        payload = self._request_json(
            (
                f"{self._settings.thesportsdb_base_url}/"
                f"{self._settings.thesportsdb_api_key}/lookupevent.php"
            ),
            params={"id": external_event_id},
            deadline=deadline,
        )
        events = self._events_from_payload(payload)
        return events[0] if events else None

    def _events_from_payload(self, payload: Any) -> list[ExternalSportsEvent]:
        return [
            event
            for item in _thesportsdb_items(payload)
            if (event := self._parse_event(item)) is not None
        ]

    def _parse_event(self, item: dict[str, Any]) -> ExternalSportsEvent | None:
        sport = canonical_sport(_text(item.get("strSport")) or "")
        if sport not in self.supported_sports:
            return None
        home = _text(item.get("strHomeTeam"))
        away = _text(item.get("strAwayTeam"))
        if not home or not away:
            event_name = _text(item.get("strEvent")) or ""
            pair = _split_provider_event(event_name)
            if pair is not None:
                home, away = pair
        starts_at = _parse_datetime(item.get("strTimestamp"))
        if starts_at is None:
            starts_at = _combine_utc_date_time(
                item.get("dateEvent"),
                item.get("strTime"),
            )
        event_id = item.get("idEvent")
        if starts_at is None or event_id is None or not home or not away:
            return None
        return ExternalSportsEvent(
            provider=self.name,
            external_event_id=str(event_id),
            sport=sport,
            participant_home=home,
            participant_away=away,
            starts_at_utc=starts_at,
            competition=_text(item.get("strLeague")),
            country=_text(item.get("strCountry")),
            status=_text(item.get("strStatus")),
            raw_payload=item,
        )


def build_sports_schedule_providers(
    settings: SportsEventSettings,
    store: SportsScheduleStore,
) -> dict[str, SportsScheduleProvider]:
    providers: dict[str, SportsScheduleProvider] = {}
    if settings.api_football_enabled and settings.api_football_key:
        providers["api_football"] = ApiFootballProvider(
            settings,
            store,
            settings.api_football_key,
        )
    if settings.football_data_enabled and settings.football_data_api_key:
        providers["football_data"] = FootballDataProvider(
            settings,
            store,
            settings.football_data_api_key,
        )
    basketball_key = settings.api_basketball_key or settings.api_football_key
    if settings.api_basketball_enabled and basketball_key:
        providers["api_basketball"] = ApiBasketballProvider(
            settings,
            store,
            basketball_key,
        )
    if settings.live_tennis_enabled and settings.live_tennis_api_key:
        providers["live_tennis"] = LiveTennisProvider(
            settings,
            store,
            settings.live_tennis_api_key,
        )
    if settings.thesportsdb_enabled and settings.thesportsdb_api_key:
        providers["thesportsdb"] = TheSportsDbProvider(settings, store)
    return providers


def _dates_between(start_at_utc: datetime, end_at_utc: datetime) -> list[date]:
    current = _utc(start_at_utc).date()
    end = _utc(end_at_utc).date()
    dates: list[date] = []
    while current <= end:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def _datetime_from_timestamp_or_iso(timestamp: Any, iso_value: Any) -> datetime | None:
    try:
        if timestamp is not None:
            return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        pass
    return _parse_datetime(iso_value)


def _parse_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    # Uma data sem horário não é suficiente para alterar o Bet Analytix.
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _combine_utc_date_time(date_value: Any, time_value: Any) -> datetime | None:
    date_text = _text(date_value)
    time_text = _text(time_value)
    if not date_text or not time_text:
        return None
    try:
        parsed = datetime.fromisoformat(f"{date_text}T{time_text}")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_list(payload: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _team_name(team: dict[str, Any]) -> str | None:
    return _text(team.get("name")) or _text(team.get("shortName"))


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _split_provider_event(value: str) -> tuple[str, str] | None:
    for marker in (" vs ", " v ", " x "):
        parts = value.split(marker)
        if len(parts) == 2 and all(part.strip() for part in parts):
            return parts[0].strip(), parts[1].strip()
    return None


def _thesportsdb_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw_items = payload.get("events")
    if raw_items is None:
        raw_items = payload.get("event")
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def _provider_name_key(value: str) -> str:
    return "".join(
        char
        for char in PROVIDER_PARTICIPANT_NORMALIZER.normalize(value)
        if char.isalnum()
    )


def _contains_exact_participant_pair(
    events: list[ExternalSportsEvent],
    participants: tuple[str, str],
) -> bool:
    expected = sorted(_provider_name_key(item) for item in participants)
    return any(
        sorted(
            (
                _provider_name_key(event.participant_home),
                _provider_name_key(event.participant_away),
            )
        )
        == expected
        for event in events
    )


def _retry_after_seconds(value: str | None) -> float:
    if value is None:
        return 60
    try:
        return max(1, min(3600, float(value)))
    except ValueError:
        return 60


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Datetime do provider precisa possuir timezone.")
    return value.astimezone(timezone.utc)
