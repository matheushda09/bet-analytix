"""Envio de notificações formatadas para o Telegram."""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from config import Settings
from models import Bet


logger = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    """Erro ao enviar mensagem para o Telegram."""


@dataclass(frozen=True)
class SentTelegramMessage:
    """Mensagem enviada e identificadores retornados pelo Telegram."""

    chat_id: str
    message_id: int
    text: str


class TelegramNotifier:
    """Cliente simples da API `sendMessage` do Telegram."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = requests.Session()
        self._send_message_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"

    def send_bet(self, bet: Bet, chat_id: str | int | None = None) -> SentTelegramMessage:
        """Envia uma aposta formatada em HTML para o chat configurado."""

        return self._post_message(
            self._format_bet_message(bet),
            chat_id=chat_id,
            reply_markup={
                "inline_keyboard": [
                    [{"text": "Odd/Stake", "callback_data": f"os:start:{bet.id}"}],
                ],
            },
        )

    def send_startup(self) -> SentTelegramMessage:
        """Envia uma mensagem curta avisando que o bot iniciou."""

        message = "\n".join(
            [
                "<b>BOT ON</b>",
                "",
                f"<b>Bankroll:</b> {_e(str(self._settings.bankroll_id))}",
                f"<b>Tipsters:</b> {_e(', '.join(self._settings.target_tipster_names))}",
                f"<b>Polling:</b> {_e(str(self._settings.poll_interval_seconds))}s",
            ]
        )
        return self._post_message(message)

    def send_html(self, message: str, chat_id: str | int | None = None) -> SentTelegramMessage:
        """Envia uma mensagem HTML generica."""

        return self._post_message(message, chat_id=chat_id)

    def _post_message(
        self,
        message: str,
        chat_id: str | int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> SentTelegramMessage:
        payload = {
            "chat_id": str(chat_id) if chat_id is not None else self._settings.telegram_chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        last_error: Exception | None = None

        for attempt in range(self._settings.request_max_retries + 1):
            try:
                response = self._session.post(
                    self._send_message_url,
                    json=payload,
                    timeout=self._settings.request_timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self._settings.request_max_retries:
                    break
                self._sleep_before_retry(attempt, reason=str(exc))
                continue

            if response.status_code == 429:
                delay = _telegram_retry_after(response) or _retry_after_header(response) or self._settings.backoff_initial_seconds
                logger.warning("Rate limit no Telegram (HTTP 429). Nova tentativa em %.1fs.", delay)
                if attempt >= self._settings.request_max_retries:
                    raise TelegramError("Rate limit persistente no Telegram.")
                time.sleep(delay)
                continue

            if 500 <= response.status_code < 600:
                logger.warning("Erro temporário no Telegram: HTTP %s.", response.status_code)
                if attempt >= self._settings.request_max_retries:
                    raise TelegramError(f"Erro HTTP {response.status_code} persistente no Telegram.")
                self._sleep_before_retry(attempt, response=response)
                continue

            if response.status_code >= 400:
                raise TelegramError(f"Telegram retornou HTTP {response.status_code}: {response.text[:500]}")

            try:
                payload = response.json()
            except ValueError as exc:
                raise TelegramError(f"Telegram retornou JSON inválido: {exc}") from exc

            result = payload.get("result")
            if not isinstance(result, dict):
                raise TelegramError(f"Telegram retornou payload inesperado: {payload}")
            chat = result.get("chat")
            if not isinstance(chat, dict) or result.get("message_id") is None:
                raise TelegramError(f"Telegram não retornou chat/message_id: {payload}")
            return SentTelegramMessage(
                chat_id=str(chat.get("id")),
                message_id=int(result["message_id"]),
                text=message,
            )

        raise TelegramError(f"Falha ao enviar mensagem ao Telegram após retries: {last_error}")

    def _sleep_before_retry(
        self,
        attempt: int,
        reason: str | None = None,
        response: requests.Response | None = None,
    ) -> None:
        delay = _retry_after_header(response) or self._settings.backoff_initial_seconds * (2**attempt)
        if reason:
            logger.warning("Falha temporária no Telegram: %s. Nova tentativa em %.1fs.", reason, delay)
        time.sleep(delay)

    def _format_bet_message(self, bet: Bet) -> str:
        lines = [
            "<b>Nova aposta detectada</b>",
            "",
            f"🏆 <b>Tipster:</b> {_e(bet.tipster_name)}",
            f"📅 <b>Data/Hora do Evento:</b> {_e(bet.event_datetime)}",
            f"⚽ <b>Esporte/Liga:</b> {_e(_join_present([bet.sport, bet.league], separator=' / '))}",
        ]

        if bet.event:
            lines.append(f"⚔️ <b>Jogo/Evento:</b> {_e(bet.event)}")

        lines.extend(
            [
                f"🎯 <b>Aposta (Pick):</b> {_e(bet.pick)}",
                f"📈 <b>Odd:</b> {_e(bet.odd)}",
                f"💰 <b>Stake:</b> {_e(bet.stake)}",
            ]
        )

        if bet.bookmaker:
            lines.append(f"🏦 <b>Casa:</b> {_e(bet.bookmaker)}")

        lines.append(f"🆔 <b>Bet ID:</b> {_e(str(bet.id))}")
        return "\n".join(lines)


def _e(value: str) -> str:
    return html.escape(value, quote=False)


def _join_present(values: list[str | None], separator: str) -> str:
    present = [value for value in values if value]
    return separator.join(present) if present else "Não informado"


def _retry_after_header(response: requests.Response | None) -> float | None:
    if response is None:
        return None
    raw_value = response.headers.get("Retry-After")
    if raw_value is None:
        return None
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return None


def _telegram_retry_after(response: requests.Response) -> float | None:
    try:
        payload: dict[str, Any] = response.json()
    except ValueError:
        return None
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        return None
    retry_after = parameters.get("retry_after")
    try:
        return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        return None
