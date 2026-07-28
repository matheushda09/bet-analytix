"""Alertas operacionais privados para falhas que nao podem ficar silenciosas."""

from __future__ import annotations

import html
import logging
import time
from typing import Any

import requests

from config import Settings


logger = logging.getLogger(__name__)


class OperationalAlerter:
    """Envia alertas privados com throttling simples anti-spam."""

    def __init__(self, settings: Settings, component: str, min_interval_seconds: int = 300) -> None:
        self._settings = settings
        self._component = component
        self._min_interval_seconds = min_interval_seconds
        self._session = requests.Session()
        self._send_message_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        self._last_sent_by_key: dict[str, float] = {}

    def send(self, title: str, details: str | None = None, dedupe_key: str | None = None) -> None:
        """Tenta enviar um alerta; nunca levanta excecao para nao derrubar o bot."""

        if not self._settings.telegram_bot_token:
            return
        chat_id = self._chat_id()
        if not chat_id:
            logger.warning("Alerta operacional descartado: TELEGRAM_ADMIN_USER_ID ausente.")
            return

        key = dedupe_key or title
        now = time.monotonic()
        last_sent = self._last_sent_by_key.get(key)
        if last_sent is not None and now - last_sent < self._min_interval_seconds:
            return
        self._last_sent_by_key[key] = now

        lines = [
            "<b>ALERTA OPERACIONAL</b>",
            f"<b>Componente:</b> {_e(self._component)}",
            f"<b>Evento:</b> {_e(title)}",
        ]
        if details:
            lines.extend(["", _e(_truncate(details, 1800))])

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": "\n".join(lines),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        timeout = min(self._settings.request_timeout_seconds, 8.0)
        for attempt in range(3):
            try:
                response = self._session.post(
                    self._send_message_url,
                    json=payload,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                if attempt >= 2:
                    logger.warning("Falha de rede ao enviar alerta operacional apos retries: %s", exc)
                    return
                time.sleep(self._retry_delay(attempt))
                continue

            if response.status_code == 429:
                if attempt >= 2:
                    logger.warning("Rate limit persistente ao enviar alerta operacional.")
                    return
                time.sleep(self._retry_after(response) or self._retry_delay(attempt))
                continue

            if 500 <= response.status_code < 600:
                if attempt >= 2:
                    logger.warning("Falha ao enviar alerta operacional: HTTP %s %s", response.status_code, response.text[:300])
                    return
                time.sleep(self._retry_delay(attempt))
                continue

            if response.status_code >= 400:
                logger.warning("Falha ao enviar alerta operacional: HTTP %s %s", response.status_code, response.text[:300])
                return

            return

    def _chat_id(self) -> str | None:
        if self._settings.telegram_admin_user_id is not None:
            return str(self._settings.telegram_admin_user_id)
        return None

    def _retry_delay(self, attempt: int) -> float:
        return min(8.0, self._settings.backoff_initial_seconds * (2**attempt))

    @staticmethod
    def _retry_after(response: requests.Response) -> float | None:
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            return None
        parameters = payload.get("parameters")
        if not isinstance(parameters, dict):
            return None
        try:
            return max(0.0, float(parameters.get("retry_after")))
        except (TypeError, ValueError):
            return None


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _e(value: str) -> str:
    return html.escape(value, quote=False)
