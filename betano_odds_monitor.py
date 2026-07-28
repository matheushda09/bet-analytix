"""Monitoramento de odds da Betano a partir de booking codes."""

from __future__ import annotations

import html
import json
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests
from requests.structures import CaseInsensitiveDict

from config import Settings
from database import BetStateStore
from operational_alerts import OperationalAlerter
from telegram_notifier import TelegramNotifier


logger = logging.getLogger(__name__)

BOOKING_CODE_PATTERN = re.compile(r"(?:/bookingcode/|^)(?P<code>[A-Za-z0-9_-]{4,64})(?:[/?#]|$)")
BROWSER_FETCH_HEADER_NAMES = {"accept", "cache-control", "pragma", "content-type", "x-kbversion"}
BROWSER_PREFERRED_SECONDS = 900


class BetanoMonitorError(RuntimeError):
    """Erro ao consultar ou interpretar dados da Betano."""


@dataclass(frozen=True)
class BetanoBetslipSnapshot:
    """Estado normalizado de um bilhete Betano."""

    booking_code: str
    current_odd: float
    bet_summary: str
    betslip: dict[str, Any]


@dataclass(frozen=True)
class _BrowserFetchResult:
    status_code: int
    url: str
    headers: dict[str, str]
    text: str


@dataclass(frozen=True)
class _BrowserFetchTask:
    method: str
    path: str
    headers: dict[str, str]
    json_payload: dict[str, Any] | None
    booking_code: str | None
    prime_booking_page: bool
    response_queue: queue.Queue[Any]


class _BetanoBrowserFallback:
    """Executa chamadas da Betano dentro de um contexto real de navegador."""

    def __init__(self, settings: Settings, base_url: str) -> None:
        self._settings = settings
        self._base_url = base_url.rstrip("/")
        self._tasks: queue.Queue[_BrowserFetchTask | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    def fetch(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        json_payload: dict[str, Any] | None = None,
        booking_code: str | None = None,
        prime_booking_page: bool = False,
    ) -> _BrowserFetchResult:
        if not self._settings.betano_browser_fallback_enabled:
            raise BetanoMonitorError("Fallback de navegador Betano desativado em BETANO_BROWSER_FALLBACK_ENABLED")

        self._ensure_started()
        response_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._tasks.put(
            _BrowserFetchTask(
                method=method.upper(),
                path=path,
                headers=_browser_fetch_headers(headers),
                json_payload=json_payload,
                booking_code=booking_code,
                prime_booking_page=prime_booking_page,
                response_queue=response_queue,
            )
        )

        timeout = (
            self._settings.request_timeout_seconds
            + self._settings.betano_browser_navigation_timeout_seconds
            + 30.0
        )
        try:
            result = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise BetanoMonitorError("Timeout no fallback de navegador da Betano") from exc

        if isinstance(result, BaseException):
            if isinstance(result, BetanoMonitorError):
                raise result
            raise BetanoMonitorError(f"Fallback de navegador Betano falhou: {result}") from result
        if not isinstance(result, _BrowserFetchResult):
            raise BetanoMonitorError("Fallback de navegador Betano retornou resposta inesperada")
        return result

    def close(self) -> None:
        with self._thread_lock:
            thread = self._thread
            if thread is None:
                return
            self._tasks.put(None)
        thread.join(timeout=5)

    def _ensure_started(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._worker_main,
                name="betano-browser-fallback",
                daemon=True,
            )
            self._thread.start()

    def _worker_main(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self._fail_tasks_forever(
                BetanoMonitorError(
                    "Playwright nao esta disponivel. Instale com `pip install playwright` "
                    "e depois rode `python -m playwright install chromium`."
                )
            )
            logger.exception("Playwright indisponivel para fallback Betano: %s", exc)
            return

        context: Any | None = None
        try:
            with sync_playwright() as playwright:
                context = self._launch_context(playwright)
                page = context.new_page()
                timeout_ms = int(
                    max(
                        self._settings.request_timeout_seconds,
                        self._settings.betano_browser_navigation_timeout_seconds,
                    )
                    * 1000
                )
                page.set_default_timeout(timeout_ms)
                page.set_default_navigation_timeout(
                    int(self._settings.betano_browser_navigation_timeout_seconds * 1000)
                )

                while True:
                    task = self._tasks.get()
                    if task is None:
                        return
                    try:
                        if page.is_closed():
                            page = context.new_page()
                        task.response_queue.put(self._handle_task(page, task))
                    except Exception as exc:
                        logger.warning("Fallback de navegador Betano falhou: %s", exc)
                        task.response_queue.put(exc)
        except Exception as exc:
            logger.exception("Worker do fallback de navegador Betano parou: %s", exc)
            self._fail_tasks_forever(exc)
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    logger.debug("Falha ao fechar contexto Playwright Betano.", exc_info=True)

    def _fail_tasks_forever(self, error: BaseException) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            task.response_queue.put(error)

    def _launch_context(self, playwright: Any) -> Any:
        profile_dir = self._settings.betano_browser_profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)

        options: dict[str, Any] = {
            "headless": self._settings.betano_browser_headless,
            "locale": "pt-BR",
            "timezone_id": self._settings.timezone,
            "user_agent": self._settings.user_agent,
            "viewport": {"width": 1366, "height": 768},
        }
        channel = self._settings.betano_browser_channel
        if channel:
            try:
                logger.info("Abrindo navegador Betano via canal Playwright `%s`.", channel)
                return playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    channel=channel,
                    **options,
                )
            except Exception as exc:
                logger.warning(
                    "Nao foi possivel abrir o canal `%s`; tentando Chromium Playwright: %s",
                    channel,
                    exc,
                )

        logger.info("Abrindo navegador Betano via Chromium Playwright.")
        return playwright.chromium.launch_persistent_context(str(profile_dir), **options)

    def _handle_task(self, page: Any, task: _BrowserFetchTask) -> _BrowserFetchResult:
        self._ensure_page_ready(page, task.booking_code, task.prime_booking_page)
        timeout_ms = int(self._settings.request_timeout_seconds * 1000)
        result = page.evaluate(
            """
            async ({ path, method, headers, body, timeoutMs }) => {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const options = {
                        method,
                        headers,
                        credentials: "include",
                        cache: "no-store",
                        signal: controller.signal,
                    };
                    if (body !== null) {
                        options.body = JSON.stringify(body);
                    }
                    const response = await fetch(path, options);
                    const responseHeaders = {};
                    response.headers.forEach((value, key) => {
                        responseHeaders[key] = value;
                    });
                    return {
                        status: response.status,
                        statusText: response.statusText,
                        url: response.url,
                        headers: responseHeaders,
                        text: await response.text(),
                    };
                } catch (error) {
                    return {
                        error: error && error.message ? error.message : String(error),
                    };
                } finally {
                    clearTimeout(timeoutId);
                }
            }
            """,
            {
                "path": task.path,
                "method": task.method,
                "headers": task.headers,
                "body": task.json_payload,
                "timeoutMs": timeout_ms,
            },
        )
        if not isinstance(result, dict):
            raise BetanoMonitorError("Resposta inesperada do fetch via navegador")
        if result.get("error"):
            raise BetanoMonitorError(f"Fetch via navegador falhou: {result['error']}")
        return _BrowserFetchResult(
            status_code=int(result.get("status") or 0),
            url=str(result.get("url") or self._base_url + task.path),
            headers={
                str(name).lower(): str(value)
                for name, value in (result.get("headers") or {}).items()
            },
            text=str(result.get("text") or ""),
        )

    def _ensure_page_ready(self, page: Any, booking_code: str | None, prime_booking_page: bool) -> None:
        target_url = f"{self._base_url}/"
        if prime_booking_page and booking_code:
            target_url = f"{self._base_url}/bookingcode/{quote(booking_code, safe='')}/"

        current_url = str(page.url or "")
        should_navigate = not current_url.startswith(self._base_url)
        if prime_booking_page and booking_code and f"/bookingcode/{booking_code}" not in current_url:
            should_navigate = True

        if should_navigate:
            logger.info("Preparando sessao de navegador Betano em %s", target_url)
            page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=int(self._settings.betano_browser_navigation_timeout_seconds * 1000),
            )
            if not self._wait_for_access_screen(page):
                raise BetanoMonitorError(
                    "Sessao de navegador Betano ainda esta na verificacao de acesso; "
                    "conclua a verificacao na janela aberta e tente novamente."
                )

    def _wait_for_access_screen(self, page: Any) -> bool:
        deadline = time.monotonic() + self._settings.betano_browser_navigation_timeout_seconds
        while time.monotonic() < deadline:
            try:
                title = str(page.title() or "").lower()
                body_text = str(page.locator("body").inner_text(timeout=1000) or "").lower()
            except Exception:
                page.wait_for_timeout(500)
                continue

            visible_text = f"{title}\n{body_text[:2000]}"
            if not _text_looks_like_access_screen(visible_text):
                return True
            page.wait_for_timeout(1000)

        logger.warning("Sessao Betano ainda parece estar em tela de verificacao de acesso.")
        return False


class BetanoOddsClient:
    """Cliente HTTP minimo baseado no fluxo observado no HAR da Betano."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = requests.Session()
        self._base_url = "https://www.betano.bet.br"
        self._browser_fallback = _BetanoBrowserFallback(settings, self._base_url)
        self._request_lock = threading.RLock()
        self._browser_preferred_until_monotonic = 0.0

    def close(self) -> None:
        self._browser_fallback.close()
        self._session.close()

    def fetch_booking_code(self, booking_code: str) -> BetanoBetslipSnapshot:
        """Carrega o betslip associado a um booking code."""

        path = f"/api/sharebookingcode/getbetslip?code={quote(booking_code, safe='')}"
        headers = self._headers(authenticated_json=False)
        via_browser = False
        if self._should_start_with_browser():
            response = self._browser_response(
                method="GET",
                path=path,
                headers=headers,
                booking_code=booking_code,
                prime_booking_page=True,
            )
            via_browser = True
        else:
            with self._request_lock:
                response = self._session.get(
                    f"{self._base_url}{path}",
                    headers=headers,
                    timeout=self._settings.request_timeout_seconds,
                )
            if _should_retry_with_browser(response):
                logger.warning("Betano bloqueou GET direto do booking code; usando fallback de navegador.")
                self._mark_browser_preferred()
                response = self._browser_response(
                    method="GET",
                    path=path,
                    headers=headers,
                    booking_code=booking_code,
                    prime_booking_page=True,
                )
                via_browser = True

        self._raise_fetch_response_error(response, via_browser=via_browser)
        return self._snapshot_from_response(booking_code, response)

    def _raise_fetch_response_error(self, response: requests.Response, *, via_browser: bool) -> None:
        if response.status_code == 404:
            raise BetanoMonitorError("booking code nao encontrado ou expirado")
        if response.status_code == 429:
            raise BetanoMonitorError("Betano retornou rate limit HTTP 429")
        if response.status_code >= 500:
            raise BetanoMonitorError(f"Betano instavel: HTTP {response.status_code}")
        if response.status_code == 403 and _looks_like_betano_splash(response):
            if via_browser:
                raise BetanoMonitorError(
                    "Betano tambem bloqueou a sessao do navegador (HTTP 403 Cloudflare/Splash Screen)"
                )
            raise BetanoMonitorError(
                "Betano bloqueou o acesso HTTP direto do processo (HTTP 403 Cloudflare/Splash Screen)"
            )
        if response.status_code >= 400:
            raise BetanoMonitorError(f"Betano retornou HTTP {response.status_code}")

    def refresh_betslip(
        self,
        booking_code: str,
        previous_betslip: dict[str, Any] | None,
    ) -> BetanoBetslipSnapshot:
        """Atualiza odds do betslip usando POST e fallback no booking code."""

        if previous_betslip:
            try:
                headers = self._headers(authenticated_json=True)
                path = "/api/betslip/v3/getbetslip"
                payload = _build_getbetslip_payload(previous_betslip)
                via_browser = False
                if self._should_start_with_browser():
                    response = self._browser_response(
                        method="POST",
                        path=path,
                        headers=headers,
                        json_payload=payload,
                        booking_code=booking_code,
                        prime_booking_page=False,
                    )
                    via_browser = True
                else:
                    with self._request_lock:
                        response = self._session.post(
                            f"{self._base_url}{path}",
                            json=payload,
                            headers=headers,
                            timeout=self._settings.request_timeout_seconds,
                        )
                    if _should_retry_with_browser(response):
                        logger.warning("Betano bloqueou POST getbetslip direto; usando fallback de navegador.")
                        self._mark_browser_preferred()
                        response = self._browser_response(
                            method="POST",
                            path=path,
                            headers=headers,
                            json_payload=payload,
                            booking_code=booking_code,
                            prime_booking_page=False,
                        )
                        via_browser = True

                if response.status_code == 429:
                    raise BetanoMonitorError("Betano retornou rate limit HTTP 429")
                if response.status_code >= 500:
                    raise BetanoMonitorError(f"Betano instavel: HTTP {response.status_code}")
                if response.status_code == 403 and _looks_like_betano_splash(response):
                    if via_browser:
                        raise BetanoMonitorError(
                            "Betano tambem bloqueou a sessao do navegador (HTTP 403 Cloudflare/Splash Screen)"
                        )
                    raise BetanoMonitorError(
                        "Betano bloqueou o acesso HTTP direto do processo (HTTP 403 Cloudflare/Splash Screen)"
                    )
                if response.status_code < 400:
                    return self._snapshot_from_response(booking_code, response)
                logger.warning(
                    "Betano getbetslip retornou HTTP %s; tentando booking code novamente.",
                    response.status_code,
                )
            except requests.RequestException as exc:
                logger.warning("Falha temporaria no POST getbetslip da Betano: %s", exc)

        return self.fetch_booking_code(booking_code)

    def _browser_response(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        json_payload: dict[str, Any] | None = None,
        booking_code: str | None = None,
        prime_booking_page: bool = False,
    ) -> requests.Response:
        result = self._browser_fallback.fetch(
            method=method,
            path=path,
            headers=headers,
            json_payload=json_payload,
            booking_code=booking_code,
            prime_booking_page=prime_booking_page,
        )
        response = requests.Response()
        response.status_code = result.status_code
        response.url = result.url
        response.headers = CaseInsensitiveDict(result.headers)
        response._content = result.text.encode("utf-8", errors="replace")
        response.encoding = "utf-8"
        return response

    def _should_start_with_browser(self) -> bool:
        return (
            self._settings.betano_browser_fallback_enabled
            and time.monotonic() < self._browser_preferred_until_monotonic
        )

    def _mark_browser_preferred(self) -> None:
        self._browser_preferred_until_monotonic = max(
            self._browser_preferred_until_monotonic,
            time.monotonic() + BROWSER_PREFERRED_SECONDS,
        )

    def _snapshot_from_response(self, booking_code: str, response: requests.Response) -> BetanoBetslipSnapshot:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BetanoMonitorError(f"Resposta Betano nao e JSON valido: {exc}") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise BetanoMonitorError("Resposta Betano sem objeto data")

        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            logger.info("Betano retornou errors no betslip: %s", errors[:3])

        bets = data.get("bets")
        if not isinstance(bets, list) or not bets:
            raise BetanoMonitorError("Betslip Betano sem apostas ativas")

        odd = _extract_final_odd(bets)
        summary = _build_betslip_summary(data)
        return BetanoBetslipSnapshot(
            booking_code=booking_code,
            current_odd=odd,
            bet_summary=summary,
            betslip=data,
        )

    def _headers(self, authenticated_json: bool) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "pt-BR,pt;q=0.5",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "referer": f"{self._base_url}/",
            "sec-ch-ua": '"Brave";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "sec-gpc": "1",
            "user-agent": self._settings.user_agent,
            "x-kbversion": "3.46.0",
        }
        if authenticated_json:
            headers["content-type"] = "application/json"
            headers["origin"] = self._base_url
        return headers


class BetanoOddsMonitor:
    """Loop isolado que checa odds da Betano e alerta no Telegram."""

    def __init__(
        self,
        settings: Settings,
        store: BetStateStore,
        client: BetanoOddsClient,
        notifier: TelegramNotifier,
        alerter: OperationalAlerter | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._client = client
        self._notifier = notifier
        self._alerter = alerter

    def run_forever(self) -> None:
        """Executa o monitoramento indefinidamente em thread propria."""

        logger.info(
            "Monitor de odds Betano iniciado; intervalo=%ss.",
            self._settings.betano_monitor_interval_seconds,
        )
        while True:
            try:
                self.process_due_once()
                self._record_heartbeat()
            except Exception as exc:
                logger.exception("Falha no loop do monitor Betano; processo continuara.")
                self._alert("Falha no monitor Betano", str(exc), "betano_monitor_loop")
                time.sleep(5)
            time.sleep(1)

    def process_due_once(self) -> None:
        """Processa monitoramentos vencidos."""

        rows = self._store.claim_due_betano_odd_monitors(limit=10)
        for row in rows:
            monitor_id = int(row["id"])
            booking_code = str(row["booking_code"])
            target_odd = float(row["target_odd"])
            try:
                previous = _json_object(row["betslip_json"])
                snapshot = self._client.refresh_betslip(booking_code, previous)
                logger.info(
                    "Betano monitor check: id=%s code=%s odd=%.3f target=%.3f.",
                    monitor_id,
                    booking_code,
                    snapshot.current_odd,
                    target_odd,
                )
                if snapshot.current_odd >= target_odd:
                    self._send_target_alert(row, snapshot)
                    self._store.mark_betano_monitor_triggered(
                        monitor_id,
                        snapshot.current_odd,
                        snapshot.bet_summary,
                        snapshot.betslip,
                    )
                    continue

                self._store.mark_betano_monitor_checked(
                    monitor_id,
                    snapshot.current_odd,
                    snapshot.bet_summary,
                    snapshot.betslip,
                    int(time.time()) + self._settings.betano_monitor_interval_seconds,
                )
            except Exception as exc:
                delay = _retry_delay(
                    error_count=int(row["error_count"]),
                    base_interval=self._settings.betano_monitor_interval_seconds,
                )
                self._store.schedule_betano_monitor_retry(
                    monitor_id,
                    str(exc),
                    delay_seconds=delay,
                    max_error_count=self._settings.betano_monitor_max_error_count,
                )
                logger.warning(
                    "Monitor Betano id=%s code=%s falhou; retry em %ss: %s",
                    monitor_id,
                    booking_code,
                    delay,
                    exc,
                )

    def _send_target_alert(self, row: Any, snapshot: BetanoBetslipSnapshot) -> None:
        target_odd = float(row["target_odd"])
        message = "\n".join(
            [
                "<b>Odd alvo atingida</b>",
                "",
                f"<b>Betano:</b> {_e(snapshot.booking_code)}",
                f"<b>Odd atual:</b> {_e(f'{snapshot.current_odd:.3f}')}",
                f"<b>Odd alvo:</b> {_e(f'{target_odd:.3f}')}",
                f"<b>Aposta:</b> {_e(snapshot.bet_summary)}",
                f"<b>Link:</b> {_e(str(row['link']))}",
            ]
        )
        self._notifier.send_html(message, chat_id=str(row["chat_id"]))

    def _record_heartbeat(self) -> None:
        try:
            self._store.set_metadata("betano_monitor_heartbeat_ts", str(int(time.time())))
        except Exception:
            logger.exception("Nao foi possivel gravar heartbeat do monitor Betano.")

    def _alert(self, title: str, details: str | None, dedupe_key: str) -> None:
        if self._alerter is not None:
            self._alerter.send(title, details, dedupe_key=dedupe_key)


def extract_booking_code(value: str) -> str | None:
    """Extrai booking code de URL Betano ou de codigo cru."""

    cleaned = value.strip()
    match = BOOKING_CODE_PATTERN.search(cleaned)
    if not match:
        return None
    return match.group("code")


def _build_getbetslip_payload(betslip: dict[str, Any]) -> dict[str, Any]:
    hash_value = str(betslip.get("hash") or betslip.get("slipData") or "")
    if not hash_value:
        raise BetanoMonitorError("Betslip sem hash/slipData para refresh")

    return {
        "hash": hash_value,
        "betslip": {
            "hash": hash_value,
            "slipData": str(betslip.get("slipData") or hash_value),
            "legs": betslip.get("legs") if isinstance(betslip.get("legs"), list) else [],
            "bets": betslip.get("bets") if isinstance(betslip.get("bets"), list) else [],
            "betslipTabId": betslip.get("betslipTabId") or betslip.get("betSlipTabId") or 1,
            "betslipTrackId": betslip.get("betslipTrackId"),
            "syncMechanism": -1,
        },
    }


def _extract_final_odd(bets: list[Any]) -> float:
    first_bet = bets[0]
    if not isinstance(first_bet, dict):
        raise BetanoMonitorError("Item de aposta Betano inesperado")
    raw_odd = first_bet.get("odds")
    try:
        odd = float(str(raw_odd).replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise BetanoMonitorError(f"Odd Betano invalida: {raw_odd!r}") from exc
    if odd <= 1:
        raise BetanoMonitorError(f"Odd Betano fora do esperado: {odd}")
    return odd


def _build_betslip_summary(data: dict[str, Any]) -> str:
    legs = data.get("legs")
    if not isinstance(legs, list) or not legs:
        return "Betslip Betano"

    parts: list[str] = []
    for leg in legs[:3]:
        if not isinstance(leg, dict):
            continue
        event_name = str(leg.get("eventName") or "").strip()
        leg_items = leg.get("legItems")
        descriptions: list[str] = []
        if isinstance(leg_items, list):
            for item in leg_items[:4]:
                if isinstance(item, dict) and item.get("description"):
                    descriptions.append(str(item["description"]).strip())
        if not descriptions and leg.get("description"):
            descriptions.append(str(leg["description"]).strip())
        label = event_name
        if descriptions:
            label = f"{event_name}: {', '.join(descriptions)}" if event_name else ", ".join(descriptions)
        if label:
            parts.append(label)

    if not parts:
        return "Betslip Betano"
    suffix = "" if len(legs) <= 3 else f" +{len(legs) - 3}"
    return " | ".join(parts) + suffix


def _json_object(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _retry_delay(error_count: int, base_interval: int) -> int:
    return min(300, max(base_interval, base_interval * (2 ** min(5, error_count))))


def _should_retry_with_browser(response: requests.Response) -> bool:
    return response.status_code == 403 and _looks_like_betano_splash(response)


def _browser_fetch_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        name.lower(): str(value)
        for name, value in headers.items()
        if name.lower() in BROWSER_FETCH_HEADER_NAMES
    }


def _looks_like_betano_splash(response: requests.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type and response.status_code != 403:
        return False
    body_start = response.text[:2000].lower()
    return _text_looks_like_access_screen(body_start)


def _text_looks_like_access_screen(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "betano splash screen",
        "cloudflare",
        "checking your browser",
        "just a moment",
        "attention required",
        "verificando se voce",
        "verifique se voce",
    )
    return any(marker in lowered for marker in markers)


def _e(value: str) -> str:
    return html.escape(value, quote=False)
