"""Listener de reações do Telegram e fila de CopyTrade."""

from __future__ import annotations

import json
import logging
import html
import re
import time
from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from bet_analytix_writer import BetAnalytixWriter
from betano_odds_monitor import BetanoMonitorError, BetanoOddsClient, extract_booking_code
from config import Settings
from database import BetStateStore, TELEGRAM_DELIVERY_PRIVATE
from message_parser import ParsedTelegramTip, TipParseError, is_tip_message, parse_tip_message
from operational_alerts import OperationalAlerter


logger = logging.getLogger(__name__)

THUMBS_UP_EMOJIS = {"👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿"}


ALLOWED_UPDATES = ["message_reaction", "message", "callback_query"]
DATETIME_CALLBACK_PREFIX = "dt:start:"
ODD_STAKE_CALLBACK_PREFIX = "os:start:"


class TelegramReactionListener:
    """Consome reações 👍 do Telegram e registra apostas em fila persistente."""

    def __init__(
        self,
        settings: Settings,
        store: BetStateStore,
        writer: BetAnalytixWriter,
        betano_client: BetanoOddsClient | None = None,
        alerter: OperationalAlerter | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._writer = writer
        self._betano_client = betano_client
        self._alerter = alerter
        self._session = requests.Session()
        self._base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        self._consecutive_poll_timeouts = 0
        self._consecutive_poll_errors = 0
        self._last_poll_instability_alert_monotonic = 0.0

    def run_forever(self) -> None:
        """Executa o listener e o processador da fila indefinidamente."""

        self._initialize_update_offset()
        logger.info("Listener de reações Telegram iniciado para admin_user_id=%s.", self._settings.telegram_admin_user_id)

        while True:
            try:
                self._consume_updates_once()
                self._consecutive_poll_timeouts = 0
                self._consecutive_poll_errors = 0
                self.process_due_jobs()
                self._record_heartbeat()
            except requests.exceptions.ReadTimeout as exc:
                self._consecutive_poll_timeouts += 1
                self._handle_transient_poll_error(
                    exc,
                    label="timeout no long polling do Telegram",
                    alert_after=6,
                    sleep_cap_seconds=30,
                )
            except requests.exceptions.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code in {429, 500, 502, 503, 504}:
                    self._handle_transient_poll_error(
                        exc,
                        label=f"HTTP {status_code} temporario no getUpdates",
                        alert_after=3,
                        sleep_cap_seconds=60,
                    )
                else:
                    logger.exception("Falha HTTP permanente no listener de reacoes; o processo continuara.")
                    self._alert("Falha HTTP no listener de reacoes", str(exc), "main_listener_http_error")
                    time.sleep(5)
            except requests.RequestException as exc:
                self._handle_transient_poll_error(
                    exc,
                    label="falha de rede temporaria no getUpdates",
                    alert_after=4,
                    sleep_cap_seconds=45,
                )
            except Exception as exc:
                logger.exception("Falha no listener de reações; o processo continuará.")
                self._alert("Falha no listener de reacoes", str(exc), "main_listener_loop")
                time.sleep(5)

    def _handle_transient_poll_error(
        self,
        exc: Exception,
        label: str,
        alert_after: int,
        sleep_cap_seconds: int,
    ) -> None:
        """Trata instabilidades temporarias do long polling sem derrubar o bot."""

        self._consecutive_poll_errors += 1
        logger.warning(
            "Instabilidade temporaria no listener: %s; listener continua ativo. consecutivos=%s erro=%s",
            label,
            self._consecutive_poll_errors,
            exc,
        )
        self._record_heartbeat()

        now = time.monotonic()
        should_alert = (
            self._consecutive_poll_errors >= alert_after
            and now - self._last_poll_instability_alert_monotonic >= 900
        )
        if should_alert:
            self._last_poll_instability_alert_monotonic = now
            self._alert(
                "Instabilidade persistente no listener de reacoes",
                f"tipo={label}\nconsecutivos={self._consecutive_poll_errors}\nerro={exc}",
                "main_listener_poll_instability",
            )

        delay = min(
            float(sleep_cap_seconds),
            self._settings.backoff_initial_seconds * (2 ** min(5, self._consecutive_poll_errors - 1)),
        )
        time.sleep(max(1.0, delay))

    def process_due_jobs(self) -> None:
        """Processa apostas pendentes da fila de CopyTrade."""

        for job in self._store.claim_due_copytrade_jobs(limit=self._settings.copytrade_queue_batch_size):
            job_id = int(job["id"])
            source_bet_id = int(job["source_bet_id"])
            attempts = int(job["attempts"])
            try:
                payload = json.loads(str(job["payload_json"]))
                tip = _tip_from_payload(payload)
                logger.info("Tentando envio CopyTrade: source_bet_id=%s attempts=%s", source_bet_id, attempts)
                response = self._writer.create_bet(tip)
                created_id = _extract_created_bet_id(response)
                self._store.mark_copytrade_job_success(job_id, created_id, response)
                logger.info("CopyTrade concluído: source_bet_id=%s created_bet_id=%s", source_bet_id, created_id)
                if attempts > 0:
                    self._alert(
                        "CopyTrade recuperado apos retry",
                        f"source_bet_id={source_bet_id} created_bet_id={created_id}",
                        f"main_copytrade_recovered_{source_bet_id}",
                    )
                self._set_fire_reaction(str(job["chat_id"]), int(job["message_id"]))
            except Exception as exc:
                delay = min(
                    self._settings.copytrade_retry_max_seconds,
                    self._settings.backoff_initial_seconds * (2**attempts),
                )
                self._store.schedule_copytrade_job_retry(job_id, str(exc), delay_seconds=delay)
                logger.exception(
                    "Erro ao enviar CopyTrade source_bet_id=%s; reagendado em %.1fs.",
                    source_bet_id,
                    delay,
                )
                self._alert(
                    "CopyTrade entrou em retry",
                    f"source_bet_id={source_bet_id}\nattempts={attempts + 1}\nretry_em={delay:.1f}s\nerro={exc}",
                    f"main_copytrade_retry_{source_bet_id}",
                )

    def _consume_updates_once(self) -> None:
        offset = self._store.get_metadata_int("telegram_update_offset")
        response = self._session.get(
            f"{self._base_url}/getUpdates",
            params={
                "offset": offset,
                "timeout": self._settings.telegram_reaction_poll_timeout_seconds,
                "allowed_updates": json.dumps(ALLOWED_UPDATES),
            },
            timeout=self._settings.telegram_reaction_poll_timeout_seconds + 10,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates retornou erro: {payload}")

        for update in payload.get("result") or []:
            update_id = int(update["update_id"])
            try:
                reaction_update = update.get("message_reaction")
                message_update = update.get("message")
                callback_update = update.get("callback_query")
                if isinstance(reaction_update, dict):
                    handled = self._handle_reaction_update(reaction_update)
                elif isinstance(message_update, dict):
                    handled = self._handle_message_update(message_update)
                elif isinstance(callback_update, dict):
                    handled = self._handle_callback_query(callback_update)
                else:
                    handled = True
            except Exception:
                logger.exception("Falha ao tratar update_id=%s; offset não será avançado.", update_id)
                self._alert("Falha ao tratar update Telegram", f"update_id={update_id}", "main_update_handler")
                handled = False

            if handled:
                self._store.set_metadata("telegram_update_offset", str(update_id + 1))
            else:
                break

    def _handle_message_update(self, message_update: dict[str, Any]) -> bool:
        text = message_update.get("text")
        if not isinstance(text, str):
            return True

        chat = message_update.get("chat")
        chat_id = str(chat.get("id")) if isinstance(chat, dict) and chat.get("id") is not None else None
        user = message_update.get("from")
        user_id = int(user["id"]) if isinstance(user, dict) and user.get("id") is not None else None
        command = _telegram_command(text)
        if user_id != self._settings.telegram_admin_user_id:
            if command.startswith("/"):
                logger.info("Comando %s ignorado: user_id=%s nao e o administrador configurado.", command, user_id)
            return True

        if chat_id is None:
            logger.info("Comando %s ignorado: chat_id ausente.", command)
            return True

        message_id = int(message_update["message_id"]) if message_update.get("message_id") is not None else None

        if command == "/monitorar":
            if not self._is_allowed_interaction_chat(chat_id):
                logger.info("Comando /monitorar ignorado em chat nao permitido: chat_id=%s", chat_id)
                return True
            self._handle_monitorar_command(chat_id, message_id, text)
            return True

        if command == "/sw":
            if not self._is_allowed_interaction_chat(chat_id):
                logger.info("Comando /sw ignorado em chat nao permitido: chat_id=%s", chat_id)
                return True
            new_mode = self._store.toggle_telegram_delivery_mode()
            response_text = "2" if new_mode == TELEGRAM_DELIVERY_PRIVATE else "1"
            self._send_plain_response(chat_id, response_text, message_id, command="/sw")
            logger.info("Swap de destino do monitor alterado para modo %s.", response_text)
            return True

        if command == "/ping":
            if not self._is_allowed_interaction_chat(chat_id):
                logger.info("Comando /ping ignorado em chat nao permitido: chat_id=%s", chat_id)
                return True

            self._send_ping_response(chat_id, message_id)
            return True

        if command in {"/cancel", "/cancelar"} and self._settings.telegram_admin_user_id is not None:
            pending_datetime = self._store.get_pending_bet_datetime_adjustment(self._settings.telegram_admin_user_id)
            pending_odd_stake = self._store.get_pending_bet_odd_stake_adjustment(self._settings.telegram_admin_user_id)
            if pending_datetime is not None:
                self._store.clear_pending_bet_datetime_adjustment(self._settings.telegram_admin_user_id)
            if pending_odd_stake is not None:
                self._store.clear_pending_bet_odd_stake_adjustment(self._settings.telegram_admin_user_id)
            if pending_datetime is not None or pending_odd_stake is not None:
                self._send_plain_response(chat_id, "Ajuste cancelado.", message_id, command="/cancel")
            return True

        if command.startswith("/"):
            return True

        if self._is_private_admin_chat(chat_id) and self._settings.telegram_admin_user_id is not None:
            pending_odd_stake = self._store.get_pending_bet_odd_stake_adjustment(self._settings.telegram_admin_user_id)
            if pending_odd_stake is not None:
                self._handle_odd_stake_adjustment_text(chat_id, message_id, text, pending_odd_stake)
                return True

            pending_datetime = self._store.get_pending_bet_datetime_adjustment(self._settings.telegram_admin_user_id)
            if pending_datetime is not None:
                self._handle_datetime_adjustment_text(chat_id, message_id, text, pending_datetime)
                return True

        return True

    def _is_allowed_interaction_chat(self, chat_id: str) -> bool:
        if chat_id == str(self._settings.telegram_chat_id):
            return True
        return self._settings.telegram_admin_user_id is not None and chat_id == str(self._settings.telegram_admin_user_id)

    def _is_private_admin_chat(self, chat_id: str) -> bool:
        return self._settings.telegram_admin_user_id is not None and chat_id == str(self._settings.telegram_admin_user_id)

    def _send_plain_response(
        self,
        chat_id: str,
        text: str,
        reply_to_message_id: int | None,
        command: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            response = self._session.post(
                f"{self._base_url}/sendMessage",
                json=payload,
                timeout=self._settings.request_timeout_seconds,
            )
            if response.status_code >= 400:
                logger.warning("Falha ao responder %s: HTTP %s %s", command, response.status_code, response.text[:300])
        except requests.RequestException:
            logger.exception("Falha de rede ao responder %s.", command)

    def _send_ping_response(self, chat_id: str, reply_to_message_id: int | None) -> None:
        now = int(time.time())
        monitor_age = _metadata_age_seconds(self._store.get_metadata_int("monitor_heartbeat_ts"), now)
        listener_age = _metadata_age_seconds(self._store.get_metadata_int("listener_heartbeat_ts"), now)
        betano_age = _metadata_age_seconds(self._store.get_metadata_int("betano_monitor_heartbeat_ts"), now)
        active_jobs = self._store.count_copytrade_jobs(("pending", "retry", "processing"))
        retry_jobs = self._store.count_copytrade_jobs(("retry",))
        done_jobs = self._store.count_copytrade_jobs(("done",))

        lines = [
            "<b>PONG</b>",
            "",
            "<b>Status:</b> ON",
            f"<b>Monitor:</b> {_format_age(monitor_age)}",
            f"<b>Listener:</b> {_format_age(listener_age)}",
            f"<b>Betano odds:</b> {_format_age(betano_age) if self._settings.betano_monitor_enabled else 'desligado'}",
            f"<b>Fila ativa:</b> {active_jobs}",
            f"<b>Retries:</b> {retry_jobs}",
            f"<b>Copiadas:</b> {done_jobs}",
            f"<b>Polling:</b> {self._settings.poll_interval_seconds}s",
            f"<b>Tipsters:</b> {', '.join(self._settings.target_tipster_names)}",
        ]
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": "\n".join(lines),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id

        try:
            response = self._session.post(
                f"{self._base_url}/sendMessage",
                json=payload,
                timeout=self._settings.request_timeout_seconds,
            )
            if response.status_code >= 400:
                logger.warning("Falha ao responder /ping: HTTP %s %s", response.status_code, response.text[:300])
        except requests.RequestException:
            logger.exception("Falha de rede ao responder /ping.")

    def _handle_monitorar_command(self, chat_id: str, message_id: int | None, text: str) -> None:
        if self._betano_client is None:
            self._send_plain_response(
                chat_id,
                "Monitoramento Betano indisponivel neste processo.",
                message_id,
                command="/monitorar",
            )
            return

        parsed = _parse_monitorar_command(text)
        if parsed is None:
            self._send_plain_response(
                chat_id,
                "Uso: /monitorar <link Betano bookingcode> <odd>\nExemplo: /monitorar https://www.betano.bet.br/bookingcode/D4FUDF6Q 1.60",
                message_id,
                command="/monitorar",
            )
            return

        link, target_odd = parsed
        booking_code = extract_booking_code(link)
        if booking_code is None:
            self._send_plain_response(
                chat_id,
                "Link invalido. Envie um link Betano no formato /bookingcode/CODIGO.",
                message_id,
                command="/monitorar",
            )
            return

        admin_user_id = self._settings.telegram_admin_user_id
        if admin_user_id is None:
            self._send_plain_response(chat_id, "Admin nao configurado.", message_id, command="/monitorar")
            return

        try:
            snapshot = self._betano_client.fetch_booking_code(booking_code)
        except (BetanoMonitorError, requests.RequestException) as exc:
            logger.warning("Falha ao validar booking code Betano %s: %s", booking_code, exc)
            self._send_plain_response(
                chat_id,
                f"Nao consegui validar esse booking code agora: {exc}",
                message_id,
                command="/monitorar",
            )
            return

        monitor_id = self._store.create_betano_odd_monitor(
            chat_id=chat_id,
            created_by_user_id=admin_user_id,
            link=link,
            booking_code=booking_code,
            target_odd=target_odd,
            current_odd=snapshot.current_odd,
            bet_summary=snapshot.bet_summary,
            betslip=snapshot.betslip,
        )

        if snapshot.current_odd >= target_odd:
            self._store.mark_betano_monitor_triggered(
                monitor_id,
                snapshot.current_odd,
                snapshot.bet_summary,
                snapshot.betslip,
            )
            self._send_plain_response(
                chat_id,
                f"Odd alvo ja atingida.\nAtual: {snapshot.current_odd:.3f}\nAlvo: {target_odd:.3f}\n{snapshot.bet_summary}",
                message_id,
                command="/monitorar",
            )
            logger.info(
                "Monitor Betano id=%s criado ja atingido: code=%s odd=%.3f target=%.3f.",
                monitor_id,
                booking_code,
                snapshot.current_odd,
                target_odd,
            )
            return

        self._send_plain_response(
            chat_id,
            (
                f"Monitoramento Betano ativo.\n"
                f"ID: {monitor_id}\n"
                f"Codigo: {booking_code}\n"
                f"Odd atual: {snapshot.current_odd:.3f}\n"
                f"Odd alvo: {target_odd:.3f}\n"
                f"{snapshot.bet_summary}"
            ),
            message_id,
            command="/monitorar",
        )
        logger.info(
            "Monitor Betano criado: id=%s code=%s odd=%.3f target=%.3f chat_id=%s.",
            monitor_id,
            booking_code,
            snapshot.current_odd,
            target_odd,
            chat_id,
        )

    def _handle_callback_query(self, callback_query: dict[str, Any]) -> bool:
        callback_id = str(callback_query.get("id") or "")
        user = callback_query.get("from")
        user_id = int(user["id"]) if isinstance(user, dict) and user.get("id") is not None else None
        if user_id != self._settings.telegram_admin_user_id:
            logger.info("Callback ignorado: user_id=%s nao e o administrador configurado.", user_id)
            self._answer_callback_query(callback_id, "Nao autorizado.")
            return True

        data = callback_query.get("data")
        if not isinstance(data, str):
            self._answer_callback_query(callback_id)
            return True

        if data.startswith(ODD_STAKE_CALLBACK_PREFIX):
            return self._handle_odd_stake_callback(callback_id, data, callback_query)

        if not data.startswith(DATETIME_CALLBACK_PREFIX):
            self._answer_callback_query(callback_id)
            return True

        raw_bet_id = data.removeprefix(DATETIME_CALLBACK_PREFIX)
        try:
            bet_id = int(raw_bet_id)
        except ValueError:
            logger.warning("Callback de ajuste com bet_id invalido: %s", data)
            self._answer_callback_query(callback_id, "Aposta invalida.")
            return True

        if self._settings.telegram_admin_user_id is None:
            self._answer_callback_query(callback_id, "Admin nao configurado.")
            return True

        callback_message = callback_query.get("message")
        callback_message_id = None
        callback_chat_id = str(self._settings.telegram_admin_user_id)
        if isinstance(callback_message, dict):
            if callback_message.get("message_id") is not None:
                callback_message_id = int(callback_message["message_id"])
            callback_chat = callback_message.get("chat")
            if isinstance(callback_chat, dict) and callback_chat.get("id") is not None:
                callback_chat_id = str(callback_chat["id"])

        self._store.set_pending_bet_datetime_adjustment(self._settings.telegram_admin_user_id, bet_id)
        self._answer_callback_query(callback_id, "Envie a data e hora no privado.")
        self._send_plain_response(
            callback_chat_id,
            (
                f"Ajuste de data/hora para Bet-Analytix ID {bet_id}.\n"
                "Envie no formato DD/MM/AAAA HH:MM.\n"
                "Exemplo: 14/06/2026 22:00\n"
                "Para cancelar: /cancel"
            ),
            callback_message_id,
            command="datetime_adjustment_prompt",
            reply_markup={
                "force_reply": True,
                "selective": True,
                "input_field_placeholder": "DD/MM/AAAA HH:MM",
            },
        )
        logger.info("Sessao de ajuste de data/hora aberta para bet_id=%s.", bet_id)
        return True

    def _handle_odd_stake_callback(
        self,
        callback_id: str,
        data: str,
        callback_query: dict[str, Any],
    ) -> bool:
        raw_source_bet_id = data.removeprefix(ODD_STAKE_CALLBACK_PREFIX)
        try:
            source_bet_id = int(raw_source_bet_id)
        except ValueError:
            logger.warning("Callback de odd/stake com source_bet_id invalido: %s", data)
            self._answer_callback_query(callback_id, "Aposta invalida.")
            return True

        admin_user_id = self._settings.telegram_admin_user_id
        if admin_user_id is None:
            self._answer_callback_query(callback_id, "Admin nao configurado.")
            return True

        callback_message = callback_query.get("message")
        if not isinstance(callback_message, dict) or callback_message.get("message_id") is None:
            self._answer_callback_query(callback_id, "Mensagem invalida.")
            return True

        callback_chat = callback_message.get("chat")
        if not isinstance(callback_chat, dict) or callback_chat.get("id") is None:
            self._answer_callback_query(callback_id, "Chat invalido.")
            return True

        source_chat_id = str(callback_chat["id"])
        source_message_id = int(callback_message["message_id"])
        message_text = self._store.get_telegram_message(source_chat_id, source_message_id)
        if message_text is None:
            self._answer_callback_query(callback_id, "Mensagem nao encontrada no banco local.")
            return True

        try:
            tip = parse_tip_message(message_text)
        except TipParseError:
            self._answer_callback_query(callback_id, "Mensagem fora do padrao.")
            return True

        if tip.source_bet_id != source_bet_id:
            logger.warning(
                "Callback odd/stake divergente: callback=%s mensagem=%s.",
                source_bet_id,
                tip.source_bet_id,
            )
            self._answer_callback_query(callback_id, "Aposta divergente.")
            return True

        self._store.set_pending_bet_odd_stake_adjustment(
            admin_user_id,
            source_bet_id,
            source_chat_id,
            source_message_id,
        )
        self._answer_callback_query(callback_id, "Envie odd e stake no privado.")
        self._send_plain_response(
            str(admin_user_id),
            (
                f"Ajuste de odd/stake para Bet ID {source_bet_id}.\n"
                "Envie no formato: ODD STAKE\n"
                "Exemplo: 2.10 50\n"
                "Para cancelar: /cancel"
            ),
            None,
            command="odd_stake_adjustment_prompt",
            reply_markup={
                "force_reply": True,
                "selective": True,
                "input_field_placeholder": "2.10 50",
            },
        )
        logger.info("Sessao de ajuste de odd/stake aberta para source_bet_id=%s.", source_bet_id)
        return True

    def _handle_odd_stake_adjustment_text(
        self,
        chat_id: str,
        reply_to_message_id: int | None,
        text: str,
        pending: Any,
    ) -> None:
        admin_user_id = self._settings.telegram_admin_user_id
        if admin_user_id is None:
            return

        source_bet_id = int(pending["source_bet_id"])
        source_chat_id = str(pending["chat_id"])
        source_message_id = int(pending["message_id"])

        try:
            odd, stake = _parse_odd_stake_adjustment(text)
        except ValueError as exc:
            self._send_plain_response(
                chat_id,
                f"Formato invalido: {exc}\nEnvie no formato ODD STAKE, exemplo: 2.10 50",
                reply_to_message_id,
                command="odd_stake_adjustment_invalid",
            )
            return

        message_text = self._store.get_telegram_message(source_chat_id, source_message_id)
        if message_text is None:
            self._send_plain_response(
                chat_id,
                "Nao encontrei a mensagem original no banco local. Ajuste cancelado por seguranca.",
                reply_to_message_id,
                command="odd_stake_adjustment_missing_message",
            )
            self._store.clear_pending_bet_odd_stake_adjustment(admin_user_id)
            return

        try:
            tip = parse_tip_message(message_text)
        except TipParseError as exc:
            self._send_plain_response(
                chat_id,
                f"A mensagem original nao esta mais no formato esperado: {exc}",
                reply_to_message_id,
                command="odd_stake_adjustment_parse_error",
            )
            return

        if tip.source_bet_id != source_bet_id:
            self._send_plain_response(
                chat_id,
                "A mensagem original nao confere com a sessao aberta. Ajuste cancelado por seguranca.",
                reply_to_message_id,
                command="odd_stake_adjustment_mismatch",
            )
            self._store.clear_pending_bet_odd_stake_adjustment(admin_user_id)
            return

        adjusted_tip = replace(tip, odd=odd, stake=stake)
        try:
            adjusted_text = _replace_tip_message_odd_stake(message_text, odd, stake)
        except ValueError as exc:
            self._send_plain_response(
                chat_id,
                f"Nao consegui atualizar a mensagem original: {exc}",
                reply_to_message_id,
                command="odd_stake_adjustment_replace_error",
            )
            return

        existing_job = self._store.get_copytrade_job_by_source_bet_id(source_bet_id)

        try:
            self._store.update_telegram_message_text(source_chat_id, source_message_id, adjusted_text)
            self._store.update_copytrade_job_payload(source_bet_id, adjusted_tip)
            self._edit_tip_message(source_chat_id, source_message_id, adjusted_text, source_bet_id)

            updated_bet_id = None
            if existing_job is not None and str(existing_job["status"]) == "done" and existing_job["bet_analytix_bet_id"]:
                updated_bet_id = int(existing_job["bet_analytix_bet_id"])
                update_response = self._writer.update_bet_odd_stake(updated_bet_id, odd, stake)
                if update_response is not None:
                    self._store.update_copytrade_job_response(source_bet_id, [update_response])
            elif existing_job is not None and str(existing_job["status"]) == "done":
                raise RuntimeError(
                    f"Job source_bet_id={source_bet_id} esta done, mas sem bet_analytix_bet_id para atualizar."
                )

            self._store.clear_pending_bet_odd_stake_adjustment(admin_user_id)
            if updated_bet_id is not None:
                response_text = f"Odd/stake atualizadas no Bet-Analytix ID {updated_bet_id}: {odd:.3f} / {stake:.2f}"
            else:
                response_text = f"Odd/stake salvas para a proxima reacao: {odd:.3f} / {stake:.2f}"
            self._send_plain_response(chat_id, response_text, reply_to_message_id, command="odd_stake_adjustment_success")
            logger.info(
                "Odd/stake ajustadas para source_bet_id=%s bet_analytix_bet_id=%s odd=%.3f stake=%.2f.",
                source_bet_id,
                updated_bet_id,
                odd,
                stake,
            )
        except Exception as exc:
            logger.exception("Falha ao ajustar odd/stake source_bet_id=%s.", source_bet_id)
            self._alert(
                "Falha ao ajustar odd/stake",
                f"source_bet_id={source_bet_id}\nerro={exc}",
                f"odd_stake_adjustment_{source_bet_id}",
            )
            self._send_plain_response(
                chat_id,
                "Nao consegui atualizar agora. A sessao continua aberta; envie odd/stake novamente ou /cancel.",
                reply_to_message_id,
                command="odd_stake_adjustment_error",
            )

    def _handle_datetime_adjustment_text(
        self,
        chat_id: str,
        reply_to_message_id: int | None,
        text: str,
        pending: Any,
    ) -> None:
        admin_user_id = self._settings.telegram_admin_user_id
        if admin_user_id is None:
            return

        bet_id = int(pending["bet_analytix_bet_id"])
        try:
            event_datetime = _parse_manual_event_datetime(text, self._settings.timezone)
        except ValueError as exc:
            self._send_plain_response(
                chat_id,
                f"Formato invalido: {exc}\nEnvie DD/MM/AAAA HH:MM, exemplo: 14/06/2026 22:00",
                reply_to_message_id,
                command="datetime_adjustment_invalid",
            )
            return

        try:
            self._writer.update_bet_datetime(bet_id, event_datetime)
            self._store.clear_pending_bet_datetime_adjustment(admin_user_id)
            self._send_plain_response(
                chat_id,
                f"Data/hora atualizada no Bet-Analytix: {event_datetime.strftime('%d/%m/%Y %H:%M')}",
                reply_to_message_id,
                command="datetime_adjustment_success",
            )
            logger.info("Data/hora atualizada no Bet-Analytix: bet_id=%s datetime=%s", bet_id, event_datetime.isoformat())
        except Exception as exc:
            logger.exception("Falha ao atualizar data/hora da aposta bet_id=%s.", bet_id)
            self._alert(
                "Falha ao ajustar data/hora de aposta",
                f"bet_id={bet_id}\nerro={exc}",
                f"datetime_adjustment_{bet_id}",
            )
            self._send_plain_response(
                chat_id,
                "Nao consegui atualizar agora. A sessao continua aberta; envie a data/hora novamente ou /cancel.",
                reply_to_message_id,
                command="datetime_adjustment_error",
            )

    def _answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        if not callback_query_id:
            return
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = False
        try:
            response = self._session.post(
                f"{self._base_url}/answerCallbackQuery",
                json=payload,
                timeout=self._settings.request_timeout_seconds,
            )
            if response.status_code >= 400:
                logger.warning(
                    "Falha ao responder callback: HTTP %s %s",
                    response.status_code,
                    response.text[:300],
                )
        except requests.RequestException:
            logger.exception("Falha de rede ao responder callback.")

    def _edit_tip_message(self, chat_id: str, message_id: int, text: str, source_bet_id: int) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": _odd_stake_reply_markup(source_bet_id),
        }
        try:
            response = self._session.post(
                f"{self._base_url}/editMessageText",
                json=payload,
                timeout=self._settings.request_timeout_seconds,
            )
            if response.status_code >= 400:
                logger.warning(
                    "Falha ao editar mensagem de tip: HTTP %s %s",
                    response.status_code,
                    response.text[:300],
                )
        except requests.RequestException:
            logger.exception("Falha de rede ao editar mensagem de tip.")

    def _handle_reaction_update(self, reaction_update: dict[str, Any]) -> bool:
        user = reaction_update.get("user")
        user_id = int(user["id"]) if isinstance(user, dict) and user.get("id") is not None else None
        if user_id != self._settings.telegram_admin_user_id:
            logger.info("Reação ignorada: user_id=%s não é o administrador configurado.", user_id)
            return True

        if not _has_thumbs_up(reaction_update.get("new_reaction")):
            if _has_thumbs_up(reaction_update.get("old_reaction")):
                logger.info("Remoção de 👍 ignorada: o CopyTrade é idempotente e não desfaz apostas.")
            return True

        chat = reaction_update.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None or reaction_update.get("message_id") is None:
            logger.warning("Update de reação sem chat/message_id: %s", reaction_update)
            return True

        chat_id = str(chat["id"])
        message_id = int(reaction_update["message_id"])

        if not self._is_allowed_interaction_chat(chat_id):
            logger.info("Reacao ignorada em chat nao permitido: chat_id=%s message_id=%s", chat_id, message_id)
            return True

        message_text = self._store.get_telegram_message(chat_id, message_id)
        if message_text is None:
            logger.info(
                "Reação ignorada: mensagem não está no SQLite. Provavelmente é antiga ou não foi enviada pelo bot. chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            return True

        if not is_tip_message(message_text):
            logger.info("Reação ignorada: mensagem salva não é uma tip. chat_id=%s message_id=%s", chat_id, message_id)
            return True

        try:
            tip = parse_tip_message(message_text)
        except TipParseError:
            logger.info("Reação ignorada: mensagem de tip fora do formato esperado. chat_id=%s message_id=%s", chat_id, message_id)
            return True

        inserted = self._store.enqueue_copytrade_job(chat_id, message_id, tip)
        if inserted:
            logger.info("Aposta ID %s capturada e enfileirada por reação 👍.", tip.source_bet_id)
        else:
            existing_job = self._store.get_copytrade_job_by_source_bet_id(tip.source_bet_id)
            if existing_job is None:
                logger.warning(
                    "Aposta ID %s não foi enfileirada por duplicidade, mas o job não foi encontrado para auditoria.",
                    tip.source_bet_id,
                )
                return True

            status = str(existing_job["status"])
            created_bet_id = existing_job["bet_analytix_bet_id"]
            if status == "done":
                logger.info(
                    "Aposta ID %s já foi inserida na bankroll destino como bet_id=%s; duplicidade ignorada.",
                    tip.source_bet_id,
                    created_bet_id,
                )
                self._set_fire_reaction(chat_id, message_id)
            else:
                logger.info(
                    "Aposta ID %s já existe na fila com status=%s attempts=%s; duplicidade ignorada.",
                    tip.source_bet_id,
                    status,
                    existing_job["attempts"],
                )
        return True

    def _initialize_update_offset(self) -> None:
        if self._store.get_metadata("telegram_update_offset") is not None:
            return

        response = self._session.get(
            f"{self._base_url}/getUpdates",
            params={"timeout": 0, "allowed_updates": json.dumps(ALLOWED_UPDATES)},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        updates = payload.get("result") or []
        if updates:
            self._store.set_metadata("telegram_update_offset", str(int(updates[-1]["update_id"]) + 1))
        else:
            self._store.set_metadata("telegram_update_offset", "0")

    def _set_fire_reaction(self, chat_id: str, message_id: int) -> None:
        try:
            response = self._session.post(
                f"{self._base_url}/setMessageReaction",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": "🔥"}],
                    "is_big": False,
                },
                timeout=self._settings.request_timeout_seconds,
            )
            if response.status_code >= 400:
                logger.warning("Falha ao reagir com 🔥: HTTP %s %s", response.status_code, response.text[:300])
        except requests.RequestException:
            logger.exception("Falha de rede ao reagir com 🔥.")


    def _record_heartbeat(self) -> None:
        try:
            self._store.set_metadata("listener_heartbeat_ts", str(int(time.time())))
        except Exception:
            logger.exception("Nao foi possivel gravar heartbeat do listener.")

    def _alert(self, title: str, details: str | None, dedupe_key: str) -> None:
        if self._alerter is not None:
            self._alerter.send(title, details, dedupe_key=dedupe_key)


def _has_thumbs_up(reactions: Any) -> bool:
    if not isinstance(reactions, list):
        return False
    for reaction in reactions:
        if not isinstance(reaction, dict):
            continue
        emoji = reaction.get("emoji")
        if reaction.get("type") == "emoji" and isinstance(emoji, str) and emoji in THUMBS_UP_EMOJIS:
            return True
    return False


def _telegram_command(text: str) -> str:
    first_token = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    return first_token.split("@", maxsplit=1)[0]


def _parse_monitorar_command(text: str) -> tuple[str, float] | None:
    parts = text.strip().split()
    if len(parts) != 3:
        return None
    link = parts[1].strip()
    try:
        target_odd = float(parts[2].replace(",", "."))
    except ValueError:
        return None
    if target_odd <= 1:
        return None
    return link, target_odd


def _parse_manual_event_datetime(text: str, timezone_name: str) -> datetime:
    value = " ".join(text.strip().split())
    if not value:
        raise ValueError("mensagem vazia")

    timezone = _load_timezone(timezone_name)
    current_year = datetime.now(timezone).year
    short_date_match = re.match(r"^(\d{1,2})/(\d{1,2})\s+(\d{1,2}:\d{2})$", value)
    short_date_value = (
        f"{short_date_match.group(1)}/{short_date_match.group(2)}/{current_year} {short_date_match.group(3)}"
        if short_date_match
        else value
    )
    formats = [
        ("%d/%m/%Y %H:%M", value),
        ("%d/%m/%y %H:%M", value),
        ("%Y-%m-%d %H:%M", value),
        ("%d/%m/%Y %H:%M", short_date_value),
    ]
    for fmt, candidate in formats:
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone)

    raise ValueError("use DD/MM/AAAA HH:MM")


def _load_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"timezone invalido: {timezone_name}") from exc


def _parse_odd_stake_adjustment(value: str) -> tuple[float, float]:
    cleaned = value.replace("R$", " ")
    odd_match = re.search(r"\bodd\s*[:=]?\s*(\d[\d.,]*)", cleaned, re.IGNORECASE)
    stake_match = re.search(r"\bstake\s*[:=]?\s*(\d[\d.,]*)", cleaned, re.IGNORECASE)
    if odd_match and stake_match:
        return (
            _parse_positive_decimal(odd_match.group(1), "odd"),
            _parse_positive_decimal(stake_match.group(1), "stake"),
        )

    numbers = re.findall(r"\d[\d.,]*", cleaned)
    if len(numbers) < 2:
        raise ValueError("nao encontrei dois numeros")
    return (
        _parse_positive_decimal(numbers[0], "odd"),
        _parse_positive_decimal(numbers[1], "stake"),
    )


def _parse_positive_decimal(value: str, field_name: str) -> float:
    normalized = _normalize_decimal_text(value, field_name)
    try:
        parsed = float(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} invalida") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} precisa ser maior que zero")
    return parsed


def _normalize_decimal_text(value: str, field_name: str) -> str:
    cleaned = value.strip().replace(" ", "")
    if not cleaned:
        raise ValueError(f"{field_name} vazia")

    last_comma = cleaned.rfind(",")
    last_dot = cleaned.rfind(".")
    if last_comma >= 0 and last_dot >= 0:
        decimal_separator = "," if last_comma > last_dot else "."
        thousands_separator = "." if decimal_separator == "," else ","
        return cleaned.replace(thousands_separator, "").replace(decimal_separator, ".")

    if "," in cleaned:
        return cleaned.replace(".", "").replace(",", ".")

    if "." in cleaned:
        integer, fractional = cleaned.rsplit(".", maxsplit=1)
        if field_name == "stake" and len(fractional) == 3 and integer.isdigit():
            return integer + fractional
        return cleaned

    return cleaned


def _replace_tip_message_odd_stake(message_text: str, odd: float, stake: float) -> str:
    adjusted = _replace_html_field(message_text, "Odd", f"{odd:.3f}")
    adjusted = _replace_html_field(adjusted, "Stake", f"{stake:.2f}")
    return adjusted


def _replace_html_field(message_text: str, label: str, value: str) -> str:
    escaped = html.escape(value, quote=False)
    pattern = re.compile(rf"(<b>{re.escape(label)}:</b>\s*)[^\r\n<]+", re.IGNORECASE)
    adjusted, count = pattern.subn(lambda match: f"{match.group(1)}{escaped}", message_text, count=1)
    if count == 0:
        raise ValueError(f"campo nao encontrado na mensagem: {label}")
    return adjusted


def _odd_stake_reply_markup(source_bet_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Odd/Stake", "callback_data": f"{ODD_STAKE_CALLBACK_PREFIX}{source_bet_id}"}],
        ],
    }


def _metadata_age_seconds(timestamp: int | None, now: int) -> int | None:
    if timestamp is None:
        return None
    return max(0, now - timestamp)


def _format_age(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "sem heartbeat"
    if age_seconds < 60:
        return f"ha {age_seconds}s"
    minutes, seconds = divmod(age_seconds, 60)
    if minutes < 60:
        return f"ha {minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"ha {hours}h{minutes:02d}m"


def _tip_from_payload(payload: dict[str, Any]) -> ParsedTelegramTip:
    from datetime import datetime

    return ParsedTelegramTip(
        tipster=str(payload["tipster"]),
        event_datetime=datetime.fromisoformat(str(payload["event_datetime"])),
        sport=str(payload["sport"]),
        league=str(payload["league"]) if payload.get("league") else None,
        pick=str(payload["pick"]),
        odd=float(payload["odd"]),
        stake=float(payload["stake"]),
        bookmaker=str(payload["bookmaker"]),
        source_bet_id=int(payload["source_bet_id"]),
        event=str(payload["event"]) if payload.get("event") else None,
        extra_note=str(payload["extra_note"]) if payload.get("extra_note") else None,
    )


def _extract_created_bet_id(response: list[dict[str, Any]]) -> int | None:
    if not response:
        return None
    value = response[0].get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
