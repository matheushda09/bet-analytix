"""Confirmacoes Telegram para apostas planilhadas pelo userbot."""

from __future__ import annotations

import html
import logging
import time
from typing import Any

import requests

from config import Settings
from message_parser import ParsedTelegramTip


logger = logging.getLogger(__name__)


class UserbotTelegramNotifier:
    """Envia confirmacoes do userbot para o privado configurado."""

    def __init__(self, settings: Settings, chat_id: str | int | None = None) -> None:
        self._settings = settings
        self._chat_id = str(chat_id or settings.telegram_chat_id)
        self._session = requests.Session()
        self._send_message_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"

    def send_planilha_success(
        self,
        tip: ParsedTelegramTip,
        source_chat_id: str | int,
        source_message_id: int,
        bet_analytix_bet_id: int | None,
        already_exists: bool = False,
    ) -> None:
        """Envia aviso de sucesso apos criacao da aposta no Bet-Analytix."""

        status = "Aposta ja estava planilhada" if already_exists else "Aposta planilhada"
        lines = [
            f"<b>{_e(status)}</b>",
            "",
            f"<b>Casa:</b> {_e(tip.bookmaker)}",
            f"<b>Evento:</b> {_e(tip.event or 'Nao informado')}",
            f"<b>Esporte:</b> {_e(tip.sport)}",
            f"<b>Pick:</b> {_e(tip.pick)}",
            f"<b>Odd:</b> {_e(f'{tip.odd:.3f}')}",
            f"<b>Stake:</b> {_e(f'{tip.stake:.2f}')}",
            f"<b>Tipster:</b> {_e(tip.tipster)}",
            f"<b>Source ID:</b> {_e(str(tip.source_bet_id))}",
        ]
        if bet_analytix_bet_id is not None:
            lines.append(f"<b>Bet-Analytix ID:</b> {_e(str(bet_analytix_bet_id))}")

        message_link = _telegram_message_link(source_chat_id, source_message_id)
        if message_link:
            lines.append(f'<b>Origem:</b> <a href="{_e(message_link)}">abrir sinal</a>')
        else:
            lines.append(f"<b>Origem:</b> chat_id={_e(str(source_chat_id))} message_id={_e(str(source_message_id))}")

        reply_markup = None
        if bet_analytix_bet_id is not None:
            reply_markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "Ajustar data/hora",
                            "callback_data": f"dt:start:{bet_analytix_bet_id}",
                        }
                    ]
                ]
            }

        self._post_html("\n".join(lines), reply_markup=reply_markup)

    def _post_html(self, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": text,
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
                delay = _telegram_retry_after(response) or self._settings.backoff_initial_seconds * (2**attempt)
                logger.warning("Rate limit no Telegram ao confirmar userbot; nova tentativa em %.1fs.", delay)
                if attempt >= self._settings.request_max_retries:
                    raise RuntimeError("Rate limit persistente no Telegram ao confirmar userbot.")
                time.sleep(delay)
                continue

            if 500 <= response.status_code < 600:
                logger.warning("Erro temporario no Telegram ao confirmar userbot: HTTP %s.", response.status_code)
                if attempt >= self._settings.request_max_retries:
                    raise RuntimeError(f"Erro HTTP {response.status_code} persistente no Telegram.")
                self._sleep_before_retry(attempt)
                continue

            if response.status_code >= 400:
                raise RuntimeError(f"Telegram retornou HTTP {response.status_code}: {response.text[:500]}")
            return

        raise RuntimeError(f"Falha ao enviar confirmacao userbot ao Telegram: {last_error}")

    def send_peixeesperto_sync_result(
        self,
        bet_analytix_bet_id: int,
        event: str,
        pick: str,
        bookmaker: str,
        odd: float,
        stake: float,
        state_text: str,
        profit: float,
    ) -> None:
        """Envia aviso de atualizacao automatica de resultado via PeixeEsperto."""

        icon = "🟢" if state_text == "Ganha" else "🔴" if state_text == "Perdida" else "⚪"
        lines = [
            f"<b>{icon} Resultado sincronizado (PeixeEsperto)</b>",
            "",
            f"<b>Estado:</b> {_e(state_text)}",
            f"<b>Casa:</b> {_e(bookmaker)}",
            f"<b>Evento:</b> {_e(event or 'Nao informado')}",
            f"<b>Pick:</b> {_e(pick)}",
            f"<b>Odd:</b> {_e(f'{odd:.3f}')}",
            f"<b>Stake:</b> {_e(f'{stake:.2f}')}",
            f"<b>Lucro:</b> {_e(f'{profit:+.2f}')}",
            f"<b>Bet-Analytix ID:</b> {_e(str(bet_analytix_bet_id))}",
        ]

        self._post_html("\n".join(lines))

    def _sleep_before_retry(self, attempt: int, reason: str | None = None) -> None:
        delay = self._settings.backoff_initial_seconds * (2**attempt)
        if reason:
            logger.warning("Falha temporaria ao confirmar userbot: %s. Nova tentativa em %.1fs.", reason, delay)
        time.sleep(delay)


def _telegram_message_link(chat_id: str | int, message_id: int) -> str | None:
    raw_chat_id = str(chat_id)
    if raw_chat_id.startswith("-100"):
        return f"https://t.me/c/{raw_chat_id[4:]}/{message_id}"
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


def _e(value: str) -> str:
    return html.escape(value, quote=False)
