"""Cliente HTTP para replicar as chamadas públicas do Bet-Analytix."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from config import Settings
from models import RawBet


logger = logging.getLogger(__name__)


class BetAnalytixError(RuntimeError):
    """Erro recuperável ou fatal ao consultar a API do Bet-Analytix."""


class BetAnalytixClient:
    """Cliente da API pública observada no HAR do Bet-Analytix."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = requests.Session()

    def fetch_bankroll(self) -> dict[str, Any]:
        """Busca os metadados da bankroll pública."""

        payload = self._request_json("GET", f"/bankroll/{self._settings.bankroll_id}")
        if not isinstance(payload, dict):
            raise BetAnalytixError("Resposta inesperada ao buscar a bankroll.")
        return payload

    def fetch_reference_data(self, user_id: int) -> dict[str, Any]:
        """Busca referências da bankroll, incluindo a lista de tipsters."""

        payload = self._request_json("GET", f"/bankroll/all-data/{user_id}")
        if not isinstance(payload, dict):
            raise BetAnalytixError("Resposta inesperada ao buscar referências da bankroll.")
        return payload

    def fetch_bookmakers(self) -> list[dict[str, Any]]:
        """Busca o catálogo global de bookmakers usado pelo front-end."""

        payload = self._request_json("GET", "/bookmakers")
        if not isinstance(payload, list):
            raise BetAnalytixError("Resposta inesperada ao buscar bookmakers.")
        return [item for item in payload if isinstance(item, dict)]

    def fetch_bets_page(self, page: int = 1) -> dict[str, Any]:
        """Busca uma página de apostas da bankroll monitorada."""

        payload = self._request_json(
            "GET",
            f"/bankroll/{self._settings.bankroll_id}/bets/paginated",
            params={"page": page},
        )
        if not isinstance(payload, dict):
            raise BetAnalytixError("Resposta inesperada ao buscar apostas.")
        if not isinstance(payload.get("bets"), list):
            raise BetAnalytixError("Payload de apostas não contém a lista 'bets'.")
        return payload

    def fetch_all_bets(self) -> list[RawBet]:
        """Busca as páginas configuradas e retorna todas as apostas brutas."""

        first_page = self.fetch_bets_page(page=1)
        bets = _extract_bets(first_page)
        pagination = first_page.get("pagination") if isinstance(first_page.get("pagination"), dict) else {}
        total_pages = _safe_int(pagination.get("totalPages"), default=1)
        pages_to_fetch = min(total_pages, self._settings.max_pages)

        for page in range(2, pages_to_fetch + 1):
            next_page = self.fetch_bets_page(page=page)
            bets.extend(_extract_bets(next_page))

        return bets

    def _request_json(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._settings.api_base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(self._settings.request_max_retries + 1):
            try:
                response = self._session.request(
                    method=method,
                    url=url,
                    params=params,
                    headers=self._headers(),
                    timeout=self._settings.request_timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self._settings.request_max_retries:
                    break
                self._sleep_before_retry(attempt, reason=str(exc))
                continue

            if response.status_code == 429:
                logger.warning("Rate limit no Bet-Analytix (HTTP 429) em %s.", response.url)
                if attempt >= self._settings.request_max_retries:
                    raise BetAnalytixError("Rate limit persistente no Bet-Analytix.")
                self._sleep_before_retry(attempt, response=response)
                continue

            if 500 <= response.status_code < 600:
                logger.warning(
                    "Erro temporário no Bet-Analytix: HTTP %s em %s.",
                    response.status_code,
                    response.url,
                )
                if attempt >= self._settings.request_max_retries:
                    raise BetAnalytixError(f"Erro HTTP {response.status_code} persistente no Bet-Analytix.")
                self._sleep_before_retry(attempt, response=response)
                continue

            if response.status_code >= 400:
                snippet = response.text[:500]
                raise BetAnalytixError(f"Bet-Analytix retornou HTTP {response.status_code}: {snippet}")

            try:
                return response.json()
            except ValueError as exc:
                raise BetAnalytixError(f"Resposta do Bet-Analytix não é JSON válido: {exc}") from exc

        raise BetAnalytixError(f"Falha ao chamar Bet-Analytix após retries: {last_error}")

    def _headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "app": self._settings.app_header,
            "cache-control": "no-cache",
            "content-type": "application/json",
            "origin": "https://app.bet-analytix.com",
            "pragma": "no-cache",
            "referer": "https://app.bet-analytix.com/",
            "user-agent": self._settings.user_agent,
        }
        if self._settings.sid:
            headers["sid"] = self._settings.sid
        return headers

    def _sleep_before_retry(self, attempt: int, reason: str | None = None, response: requests.Response | None = None) -> None:
        retry_after = _retry_after_seconds(response)
        delay = retry_after if retry_after is not None else self._settings.backoff_initial_seconds * (2**attempt)
        if reason:
            logger.warning("Falha temporária no Bet-Analytix: %s. Nova tentativa em %.1fs.", reason, delay)
        time.sleep(delay)


def _extract_bets(payload: dict[str, Any]) -> list[RawBet]:
    bets = payload.get("bets")
    if not isinstance(bets, list):
        raise BetAnalytixError("Payload de apostas não contém a lista 'bets'.")
    return [bet for bet in bets if isinstance(bet, dict)]


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _retry_after_seconds(response: requests.Response | None) -> float | None:
    if response is None:
        return None
    raw_value = response.headers.get("Retry-After")
    if raw_value is None:
        return None
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return None
