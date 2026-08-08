"""Bot Discord para copiar sinais externos por reacao autorizada."""

from __future__ import annotations

import asyncio
import hashlib
import argparse
import json
import logging
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import discord

from bankroll_module import BankrollController
from bet_analytix_writer import BetAnalytixWriter
from config import Settings, load_settings
from discord_bankroll_commands import dispatch_bankroll_command
from discord_config import (
    BankrollSettings,
    DiscordReactionSettings,
    PeixeEspertoSettings,
    load_bankroll_settings,
    load_discord_reaction_settings,
    load_peixeesperto_settings,
    validate_discord_reaction_settings,
)
from discord_database import DiscordSignalStore, tip_from_payload
from message_parser import ParsedTelegramTip
from operational_alerts import OperationalAlerter
from peixeesperto_result_sync import PeixeEspertoResultSync
from sports_event_config import SportsEventSettings, load_sports_event_settings
from sports_event_service import SportsEventService
from sports_schedule_store import SportsScheduleStore
from userbot_signal_parser import (
    ODD_CHANGED_MARKER_PATTERN,
    UserbotSignalParseError,
    is_external_signal_message,
    parse_external_signal,
)
from userbot_telegram_notifier import UserbotTelegramNotifier


logger = logging.getLogger(__name__)

THUMBS_UP_EMOJIS = {
    "\U0001F44D",
    "\U0001F44D\U0001F3FB",
    "\U0001F44D\U0001F3FC",
    "\U0001F44D\U0001F3FD",
    "\U0001F44D\U0001F3FE",
    "\U0001F44D\U0001F3FF",
}

HEART_EMOJIS = {
    "\U00002764",
    "\U00002764\U0000FE0F",
    "\U00002764\U0001F3FB",
    "\U00002764\U0001F3FC",
    "\U00002764\U0001F3FD",
    "\U00002764\U0001F3FE",
    "\U00002764\U0001F3FF",
}


class DiscordSignalClient(discord.Client):
    """Cliente Discord que escuta reacoes e processa a fila Bet-Analytix."""

    def __init__(
        self,
        base_settings: Settings,
        discord_settings: DiscordReactionSettings,
        store: DiscordSignalStore,
        writer: BetAnalytixWriter,
        notifier: UserbotTelegramNotifier | None,
        alerter: OperationalAlerter | None,
        bankroll_settings: BankrollSettings | None = None,
        bankroll_controller: BankrollController | None = None,
        peixeesperto_settings: PeixeEspertoSettings | None = None,
        peixeesperto_sync: PeixeEspertoResultSync | None = None,
        sports_event_settings: SportsEventSettings | None = None,
        sports_event_service: SportsEventService | None = None,
    ) -> None:
        # Bot oficial: message_content e reactions sao obrigatorios; members nao e usado.
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.guilds = True
        super().__init__(intents=intents)

        self._base_settings = base_settings
        self._settings = discord_settings
        self._store = store
        self._writer = writer
        self._notifier = notifier
        self._alerter = alerter
        self._bankroll_settings = bankroll_settings
        self._bankroll_controller = bankroll_controller
        self._peixeesperto_settings = peixeesperto_settings
        self._peixeesperto_sync = peixeesperto_sync
        self._sports_event_settings = sports_event_settings
        self._sports_event_service = sports_event_service
        self._processor_task: asyncio.Task[None] | None = None
        self._bankroll_scheduler_task: asyncio.Task[None] | None = None
        self._green_sync_scheduler_task: asyncio.Task[None] | None = None
        self._peixeesperto_scheduler_task: asyncio.Task[None] | None = None
        self._sports_event_recheck_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        """Inicializa tarefas de fundo depois que o cliente esta pronto."""

        self._processor_task = asyncio.create_task(self.process_due_loop(), name="discord-signal-queue")
        if self._bankroll_settings is not None and self._bankroll_settings.enabled:
            self._bankroll_scheduler_task = asyncio.create_task(
                self._bankroll_report_scheduler(),
                name="bankroll-daily-report",
            )
            self._green_sync_scheduler_task = asyncio.create_task(
                self._green_bet_sync_scheduler(),
                name="green-bet-sync",
            )
        if self._peixeesperto_settings is not None and self._peixeesperto_settings.enabled:
            self._peixeesperto_scheduler_task = asyncio.create_task(
                self._peixeesperto_result_sync_scheduler(),
                name="peixeesperto-result-sync",
            )
        if (
            self._sports_event_settings is not None
            and self._sports_event_settings.mode == "enabled"
            and self._sports_event_service is not None
        ):
            self._sports_event_recheck_task = asyncio.create_task(
                self._sports_event_recheck_scheduler(),
                name="sports-event-recheck",
            )

    async def close(self) -> None:
        """Encerra o cliente e as tarefas de fundo."""

        if self._processor_task is not None:
            self._processor_task.cancel()
            await _cancel_task(self._processor_task)
        if self._bankroll_scheduler_task is not None:
            self._bankroll_scheduler_task.cancel()
            await _cancel_task(self._bankroll_scheduler_task)
        if self._green_sync_scheduler_task is not None:
            self._green_sync_scheduler_task.cancel()
            await _cancel_task(self._green_sync_scheduler_task)
        if self._peixeesperto_scheduler_task is not None:
            self._peixeesperto_scheduler_task.cancel()
            await _cancel_task(self._peixeesperto_scheduler_task)
        if self._sports_event_recheck_task is not None:
            self._sports_event_recheck_task.cancel()
            await _cancel_task(self._sports_event_recheck_task)
        await super().close()

    async def on_ready(self) -> None:
        """Registra o estado inicial do bot Discord."""

        account_kind = "bot"
        logger.info(
            "Discord reaction client ON: account_kind=%s bot_id=%s guild_id=%s channel_id=%s admin_user_id=%s tipster_destino=%s",
            account_kind,
            self.user.id if self.user else None,
            self._settings.guild_id,
            self._settings.channel_id,
            self._settings.admin_user_id,
            self._settings.destination_tipster_name,
        )
        if self._sports_event_settings is not None:
            logger.info(
                "Sports event matching: mode=%s cache=%s providers=%s",
                self._sports_event_settings.mode,
                self._sports_event_settings.cache_path,
                (
                    ",".join(self._sports_event_service.available_providers)
                    if self._sports_event_service is not None
                    else "nenhum"
                ),
            )
        logger.info(
            "Discord reaction bot ON: guild_id=%s channel_id=%s tipster=%s",
            self._settings.guild_id,
            self._settings.channel_id,
            self._settings.destination_tipster_name,
        )

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Processa somente thumbs-up do usuario autorizado no canal monitorado."""

        try:
            await self._handle_reaction_add(payload)
        except Exception as exc:
            logger.exception("Falha ao tratar reacao Discord; listener continua ativo.")
            await self._alert_async("Falha ao tratar reacao Discord", str(exc), "discord_reaction_update")

    async def process_due_loop(self) -> None:
        """Processa jobs pendentes continuamente."""

        while True:
            try:
                await self.process_due_jobs_once()
                self._record_heartbeat()
            except Exception as exc:
                logger.exception("Falha no processador da fila Discord; nova tentativa em breve.")
                await self._alert_async("Falha no processador da fila Discord", str(exc), "discord_queue_loop")
            await asyncio.sleep(max(1, self._settings.poll_interval_seconds))

    async def process_due_jobs_once(self) -> None:
        """Executa uma rodada de envio dos jobs vencidos ao Bet-Analytix."""

        jobs = self._store.claim_due_jobs(limit=self._settings.queue_batch_size)
        for job in jobs:
            job_id = int(job["id"])
            source_bet_id = int(job["source_bet_id"])
            attempts = int(job["attempts"])
            try:
                payload = json.loads(str(job["payload_json"]))
                tip = tip_from_payload(payload)
                logger.info("Discord enviando sinal ao Bet-Analytix: source_bet_id=%s attempts=%s accumulator=%s", source_bet_id, attempts, tip.is_accumulator)
                if tip.is_accumulator:
                    response = await asyncio.to_thread(self._writer.create_accumulator_bet, tip)
                else:
                    response = await asyncio.to_thread(self._writer.create_bet, tip)
                created_id = _extract_created_bet_id(response)
                self._store.mark_job_success(job_id, created_id, response)
                if self._sports_event_service is not None:
                    next_check = self._sports_event_service.next_recheck_timestamp(
                        tip.event_datetime,
                        None,
                    )
                    self._store.mark_sports_event_applied(
                        source_bet_id,
                        created_id,
                        next_check,
                    )
                logger.info("Discord concluiu CopyTrade: source_bet_id=%s created_bet_id=%s", source_bet_id, created_id)
                if attempts > 0:
                    await self._alert_async(
                        "Discord recuperou CopyTrade apos retry",
                        f"source_bet_id={source_bet_id} created_bet_id={created_id}",
                        f"discord_recovered_{source_bet_id}",
                    )
                await self._notify_discord_success(
                    tip=tip,
                    bet_analytix_bet_id=created_id,
                    already_exists=_response_already_exists(response),
                )
                await self._notify_success(
                    tip=tip,
                    guild_id=str(job["guild_id"]),
                    channel_id=str(job["channel_id"]),
                    message_id=int(job["message_id"]),
                    bet_analytix_bet_id=created_id,
                    already_exists=_response_already_exists(response),
                )
            except Exception as exc:
                error_message = str(exc)
                if _is_bookmaker_not_found_error(error_message):
                    await self._ask_admin_for_bookmaker(job, tip, error_message)
                    continue
                if _is_unrecoverable_error(error_message):
                    self._store.mark_job_failed(job_id, error_message)
                    logger.error(
                        "Job Discord source_bet_id=%s marcado como falha permanente: %s",
                        source_bet_id,
                        error_message,
                    )
                    await self._alert_async(
                        "Discord descartou CopyTrade",
                        f"source_bet_id={source_bet_id}\nerro={exc}",
                        f"discord_failed_{source_bet_id}",
                    )
                    continue
                delay = min(
                    self._settings.retry_max_seconds,
                    self._base_settings.backoff_initial_seconds * (2**attempts),
                )
                self._store.schedule_job_retry(job_id, error_message, delay_seconds=delay)
                logger.exception(
                    "Erro ao enviar job Discord source_bet_id=%s; reagendado em %.1fs.",
                    source_bet_id,
                    delay,
                )
                await self._alert_async(
                    "Discord entrou em retry",
                    f"source_bet_id={source_bet_id}\nattempts={attempts + 1}\nretry_em={delay:.1f}s\nerro={exc}",
                    f"discord_retry_{source_bet_id}",
                )

    async def on_message(self, message: discord.Message) -> None:
        """Escuta respostas do admin e comandos de bankroll."""

        if self._settings.admin_user_id is None:
            return
        if message.author.id != self._settings.admin_user_id:
            return
        if message.author.bot:
            return
        if not message.content:
            return

        # Comandos de bankroll podem vir por DM ou no canal monitorado.
        if self._bankroll_controller is not None and self._bankroll_settings is not None:
            prefix = self._bankroll_settings.command_prefix
            if message.content.strip().lower().startswith(prefix.lower()):
                logger.info(
                    "Comando de bankroll recebido de admin=%s no canal=%s: %s",
                    message.author.id,
                    message.channel.id,
                    message.content.strip(),
                )
                await dispatch_bankroll_command(message, self._bankroll_controller, prefix)
                return

        # Resposta de correcao de bookmaker continua sendo apenas por DM.
        if not isinstance(message.channel, discord.DMChannel):
            return

        pending_jobs = self._store.get_jobs_awaiting_bookmaker(limit=1)
        if not pending_jobs:
            return

        job = pending_jobs[0]
        new_bookmaker = message.content.strip()
        if not new_bookmaker:
            return

        updated = self._store.update_job_bookmaker(int(job["id"]), new_bookmaker)
        if not updated:
            return

        logger.info(
            "Bookmaker corrigida pelo admin: job_id=%s source_bet_id=%s new_bookmaker=%s",
            job["id"],
            job["source_bet_id"],
            new_bookmaker,
        )
        try:
            await message.channel.send(f"✅ Casa atualizada para '{new_bookmaker}'. Reprocessando aposta...")
        except Exception:
            logger.exception("Falha ao confirmar correcao de casa no DM.")
        await self.process_due_jobs_once()

    async def _ask_admin_for_bookmaker(
        self,
        job: sqlite3.Row,
        tip: ParsedTelegramTip,
        error_message: str,
    ) -> None:
        """Pergunta ao admin no privado qual o nome correto da casa de aposta."""

        if self._settings.admin_user_id is None:
            self._store.schedule_job_retry(
                int(job["id"]),
                error_message,
                delay_seconds=self._base_settings.backoff_initial_seconds,
            )
            return

        try:
            user = await self.fetch_user(self._settings.admin_user_id)
            if user is None:
                logger.warning("Nao foi possivel encontrar usuario admin=%s para perguntar sobre casa.", self._settings.admin_user_id)
                self._store.schedule_job_retry(
                    int(job["id"]),
                    error_message,
                    delay_seconds=self._base_settings.backoff_initial_seconds,
                )
                return

            current_bookmaker = tip.bookmaker
            question = (
                f"⚠️ Casa não encontrada no Bet-Analytix: {current_bookmaker}\n"
                f"Evento: {tip.event}\n"
                f"Pick: {tip.pick}\n"
                f"Odd: {tip.odd}\n\n"
                f"Responda com o nome correto da casa de aposta.\n"
                f"Você também pode responder 'id N' (ex: 'id 123') para usar o ID direto.\n\n"
                f"source_bet_id: {tip.source_bet_id}"
            )
            await user.send(question)
            self._store.mark_job_awaiting_bookmaker(int(job["id"]), question)
            logger.info(
                "Pergunta enviada ao admin sobre bookmaker: source_bet_id=%s bookmaker=%s",
                tip.source_bet_id,
                current_bookmaker,
            )
            await self._alert_async(
                "Discord aguardando correcao de casa",
                f"source_bet_id={tip.source_bet_id}\nbookmaker={current_bookmaker}",
                f"discord_ask_bookmaker_{tip.source_bet_id}",
            )
        except Exception as ask_exc:
            logger.exception("Falha ao perguntar casa ao admin; fallback para retry.")
            self._store.schedule_job_retry(
                int(job["id"]),
                f"{error_message}; falha ao perguntar ao admin: {ask_exc}",
                delay_seconds=self._base_settings.backoff_initial_seconds,
            )
            await self._alert_async(
                "Falha ao perguntar casa ao admin no Discord",
                f"source_bet_id={tip.source_bet_id}\nerro={ask_exc}",
                f"discord_ask_bookmaker_fail_{tip.source_bet_id}",
            )

    async def _handle_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        guild_id = int(payload.guild_id or 0)
        channel_id = int(payload.channel_id)
        message_id = int(payload.message_id)
        user_id = int(payload.user_id)
        emoji = str(payload.emoji)

        if guild_id != self._settings.guild_id or channel_id != self._settings.channel_id:
            logger.info(
                "Reacao Discord ignorada em origem nao monitorada: guild_id=%s channel_id=%s message_id=%s",
                guild_id,
                channel_id,
                message_id,
            )
            return
        if user_id != self._settings.admin_user_id:
            logger.info("Reacao Discord ignorada: user_id=%s nao e o admin configurado.", user_id)
            return
        if emoji in THUMBS_UP_EMOJIS:
            is_accumulator = False
        elif emoji in HEART_EMOJIS:
            is_accumulator = True
        else:
            logger.info("Reacao Discord ignorada: emoji=%s nao e thumbs-up nem coracao.", emoji)
            return

        await self._process_thumb_message(guild_id, channel_id, message_id, user_id, is_accumulator)

    async def _process_thumb_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        reacting_user_id: int,
        is_accumulator: bool = False,
    ) -> None:
        existing = self._store.get_job_by_message(guild_id, channel_id, message_id)
        if existing is not None:
            logger.info(
                "Sinal Discord duplicado ignorado: source_bet_id=%s status=%s attempts=%s",
                existing["source_bet_id"],
                existing["status"],
                existing["attempts"],
            )
            return

        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logger.info("Canal Discord ignorado: tipo sem fetch_message. channel_id=%s", channel_id)
            return

        message = await channel.fetch_message(message_id)
        text = str(message.content or "").strip()
        if not text:
            logger.info("Discord ignorado: mensagem sem texto. message_id=%s", message_id)
            return
        logger.info("Discord mensagem recebida message_id=%s text=%r", message_id, text)
        is_signal = is_external_signal_message(text, signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN)
        logger.info("Discord mensagem parse check: message_id=%s is_signal=%s", message_id, is_signal)
        if not is_signal:
            logger.info("Discord ignorado: mensagem nao tem marcador/campos de sinal. message_id=%s", message_id)
            return

        try:
            signal = parse_external_signal(
                text,
                bookmaker_aliases=self._settings.bookmaker_aliases,
                signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN,
            )
        except UserbotSignalParseError as exc:
            logger.info("Discord ignorado: mensagem fora do padrao. message_id=%s error=%s", message_id, exc)
            return

        source_bet_id = build_source_bet_id(guild_id, channel_id, message_id)
        fallback_datetime_utc = _message_datetime(message.created_at)
        sports_event_audit: dict[str, Any] | None = None
        if signal.event_datetime is not None:
            event_datetime = signal.event_datetime
            logger.info(
                "Discord usou data/hora do evento da mensagem: message_id=%s event_datetime=%s",
                message_id,
                event_datetime,
            )
            if self._sports_event_settings is not None and self._sports_event_settings.enabled:
                explicit_utc = _datetime_as_utc(
                    signal.event_datetime,
                    self._base_settings.timezone,
                )
                sports_event_audit = _explicit_datetime_audit(
                    source_bet_id=source_bet_id,
                    mode=self._sports_event_settings.mode,
                    sport=signal.sport,
                    event_name=signal.event,
                    explicit_datetime_utc=explicit_utc,
                )
        else:
            event_datetime = fallback_datetime_utc
            logger.info(
                "Discord usou data/hora de envio da mensagem: message_id=%s event_datetime=%s",
                message_id,
                event_datetime,
            )
            if self._sports_event_service is not None and self._sports_event_settings is not None:
                try:
                    sports_result = await asyncio.to_thread(
                        self._sports_event_service.resolve_event,
                        sport=signal.sport,
                        event_name=signal.event,
                        received_at_utc=fallback_datetime_utc,
                    )
                    sports_event_audit = sports_result.as_audit_dict(
                        source_bet_id=source_bet_id,
                        mode=self._sports_event_settings.mode,
                        fallback_datetime_utc=fallback_datetime_utc,
                    )
                    if not self._sports_event_settings.store_raw_payload:
                        sports_event_audit["raw_payload"] = {}
                    if (
                        self._sports_event_settings.mode == "enabled"
                        and sports_result.accepted
                        and sports_result.event is not None
                    ):
                        event_datetime = sports_result.event.starts_at_utc
                        logger.info(
                            "Discord usou horario oficial do evento: message_id=%s provider=%s external_event_id=%s event_datetime_utc=%s confidence=%.4f",
                            message_id,
                            sports_result.event.provider,
                            sports_result.event.external_event_id,
                            event_datetime.isoformat(),
                            sports_result.confidence,
                        )
                    elif self._sports_event_settings.mode == "shadow":
                        logger.info(
                            "Sports shadow preservou horario da mensagem: message_id=%s candidato=%s confidence=%.4f reason=%s",
                            message_id,
                            (
                                sports_result.event.starts_at_utc.isoformat()
                                if sports_result.event is not None
                                else None
                            ),
                            sports_result.confidence,
                            sports_result.reason,
                        )
                except Exception as exc:
                    logger.exception(
                        "Identificacao esportiva falhou; horario da mensagem sera preservado. message_id=%s",
                        message_id,
                    )
                    sports_event_audit = _sports_fallback_audit(
                        source_bet_id=source_bet_id,
                        mode=self._sports_event_settings.mode,
                        sport=signal.sport,
                        event_name=signal.event,
                        fallback_datetime_utc=fallback_datetime_utc,
                        reason=f"unexpected_error:{type(exc).__name__}",
                    )
        tip = ParsedTelegramTip(
            tipster=self._settings.destination_tipster_name,
            event_datetime=event_datetime,
            sport=signal.sport,
            league=None,
            pick=_pick_label(signal.event, signal.pick),
            odd=signal.odd,
            stake=signal.stake,
            bookmaker=signal.bookmaker,
            source_bet_id=source_bet_id,
            event=signal.event,
            extra_note=signal.note(str(channel_id), message_id),
            is_accumulator=is_accumulator,
        )

        inserted = self._store.enqueue_signal(
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            signal_sender_id=int(message.author.id),
            reacting_user_id=reacting_user_id,
            tip=tip,
            raw_message=text,
            source_bookmaker_name=signal.source_bookmaker,
            source_tipster_name=signal.admin,
        )
        if not inserted:
            logger.info("Sinal Discord duplicado ignorado apos enqueue: source_bet_id=%s", source_bet_id)
            return
        if sports_event_audit is not None:
            try:
                self._store.record_sports_event_match(sports_event_audit)
            except Exception:
                logger.exception(
                    "Falha ao persistir auditoria esportiva; aposta continuara normalmente. source_bet_id=%s",
                    source_bet_id,
                )

        reaction_label = "coracao" if is_accumulator else "thumbs-up"
        logger.info(
            "Sinal Discord enfileirado por %s do admin: source_bet_id=%s guild_id=%s channel_id=%s message_id=%s",
            reaction_label,
            source_bet_id,
            guild_id,
            channel_id,
            message_id,
        )
        await self.process_due_jobs_once()

    async def _notify_discord_success(
        self,
        tip: ParsedTelegramTip,
        bet_analytix_bet_id: int | None,
        already_exists: bool,
    ) -> None:
        """Envia DM no Discord para o admin confirmando a aposta planilhada."""

        if not self._settings.notify_dm_on_success or self._settings.admin_user_id is None:
            return
        try:
            user = await self.fetch_user(self._settings.admin_user_id)
            if user is None:
                logger.warning("Nao foi possivel encontrar usuario Discord admin=%s para DM.", self._settings.admin_user_id)
                return
            status = "ja existia" if already_exists else "criada"
            message = (
                f"✅ Aposta {status} no Bet-Analytix\n"
                f"Tipster: {tip.tipster}\n"
                f"Evento: {tip.event}\n"
                f"Pick: {tip.pick}\n"
                f"Odd: {tip.odd}\n"
                f"Stake: {tip.stake}\n"
                f"Bookmaker: {tip.bookmaker}\n"
                f"ID Bet-Analytix: {bet_analytix_bet_id or 'N/A'}"
            )
            await user.send(message)
            logger.info("DM de confirmacao enviada para admin=%s source_bet_id=%s.", self._settings.admin_user_id, tip.source_bet_id)
        except Exception:
            logger.exception("Falha ao enviar DM de confirmacao no Discord para source_bet_id=%s.", tip.source_bet_id)

    async def _notify_success(
        self,
        tip: ParsedTelegramTip,
        guild_id: str,
        channel_id: str,
        message_id: int,
        bet_analytix_bet_id: int | None,
        already_exists: bool,
    ) -> None:
        """Envia confirmacao Telegram sem afetar a fila principal."""

        if self._notifier is None:
            return
        try:
            await asyncio.to_thread(
                self._notifier.send_planilha_success,
                tip,
                f"discord:{guild_id}:{channel_id}",
                message_id,
                bet_analytix_bet_id,
                already_exists,
            )
            logger.info(
                "Confirmacao Telegram enviada para source_bet_id=%s bet_analytix_bet_id=%s.",
                tip.source_bet_id,
                bet_analytix_bet_id,
            )
        except Exception:
            logger.exception(
                "Falha ao enviar confirmacao Telegram para source_bet_id=%s; aposta continua marcada como planilhada.",
                tip.source_bet_id,
            )
            await self._alert_async(
                "Falha ao enviar confirmacao do Discord",
                f"source_bet_id={tip.source_bet_id}",
                f"discord_confirm_{tip.source_bet_id}",
            )

    def _record_heartbeat(self) -> None:
        try:
            self._store.set_metadata("discord_heartbeat_ts", str(int(time.time())))
        except Exception:
            logger.exception("Nao foi possivel gravar heartbeat do Discord.")

    async def _bankroll_report_scheduler(self) -> None:
        """Aguarda ate o horario configurado e envia o relatorio diario."""

        if self._bankroll_settings is None or not self._bankroll_settings.enabled:
            return

        await self.wait_until_ready()

        while not self.is_closed():
            try:
                seconds_until_next = self._seconds_until_bankroll_report()
                if seconds_until_next > 0:
                    logger.info("Proximo relatorio de bankroll em %.1f segundos.", seconds_until_next)
                    await asyncio.sleep(seconds_until_next)

                if self.is_closed():
                    return

                await self._send_bankroll_report()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Falha no scheduler de relatorio de bankroll.")
                await self._alert_async("Falha scheduler bankroll", str(exc), "bankroll_scheduler_error")
                await asyncio.sleep(3600)

    def _seconds_until_bankroll_report(self) -> float:
        """Calcula quantos segundos faltam para o proximo envio (UTC)."""

        from datetime import datetime, timedelta, timezone

        time_str = self._bankroll_settings.report_time_utc if self._bankroll_settings else "03:00"
        try:
            hour, minute = map(int, time_str.split(":", 1))
        except ValueError:
            hour, minute = 3, 0

        now = datetime.now(timezone.utc)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _send_bankroll_report(self) -> None:
        """Coleta dados e envia o relatorio diario por DM ao admin."""

        from datetime import datetime, timezone

        if self._bankroll_controller is None or self._bankroll_settings is None:
            return
        if not self._bankroll_settings.report_dm_on or self._settings.admin_user_id is None:
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._store.get_report_for_date(today) is not None:
            logger.info("Relatorio de bankroll de %s ja foi enviado.", today)
            return

        try:
            self._bankroll_controller.sync_green_bets()
        except Exception as exc:
            logger.exception("Falha ao sincronizar greens antes do relatorio.")
            await self._alert_async("Falha ao sincronizar greens", str(exc), "green_sync_before_report_error")

        try:
            report = self._bankroll_controller.build_report()
        except Exception as exc:
            logger.exception("Falha ao montar relatorio de bankroll.")
            await self._alert_async("Falha ao montar relatorio bankroll", str(exc), "bankroll_report_build_error")
            return

        from discord_bankroll_commands import _format_report_message

        text = _format_report_message(self._bankroll_controller, report)
        user = await self.fetch_user(self._settings.admin_user_id)
        if user is None:
            logger.warning("Nao foi possivel enviar relatorio: admin nao encontrado.")
            return

        try:
            await user.send(text)
            self._store.mark_report_sent(today, json.dumps({"sent": True}))
            logger.info("Relatorio diario de bankroll enviado para admin=%s.", self._settings.admin_user_id)
        except Exception:
            logger.exception("Falha ao enviar relatorio de bankroll por DM.")

    async def _green_bet_sync_scheduler(self) -> None:
        """Sincroniza apostas green a cada hora."""

        if self._bankroll_controller is None:
            return

        await self.wait_until_ready()

        while not self.is_closed():
            try:
                green_count, green_total, red_count, red_total = await asyncio.to_thread(self._bankroll_controller.sync_green_bets)
                if green_count > 0 or red_count > 0:
                    logger.info(
                        "Scheduler sincronizou %s greens (R$ %s) e %s reds (R$ %s).",
                        green_count,
                        green_total,
                        red_count,
                        red_total,
                    )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Falha na sincronizacao automatica de greens.")
                await self._alert_async("Falha sync greens", str(exc), "green_sync_error")

            await asyncio.sleep(3600)

    async def _peixeesperto_result_sync_scheduler(self) -> None:
        """Sincroniza resultados do PeixeEsperto periodicamente."""

        if self._peixeesperto_sync is None:
            return

        await self.wait_until_ready()

        while not self.is_closed():
            try:
                updated, ambiguous, already_resolved, ignored = await asyncio.to_thread(self._peixeesperto_sync.sync_once)
                if updated > 0 or ambiguous > 0 or already_resolved > 0:
                    logger.info(
                        "PeixeEsperto scheduler: %s atualizadas, %s ambiguas, %s ja resolvidas, %s ignoradas.",
                        updated,
                        ambiguous,
                        already_resolved,
                        ignored,
                    )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Falha na sincronizacao de resultados PeixeEsperto.")
                await self._alert_async("Falha sync PeixeEsperto", str(exc), "peixeesperto_sync_error")

            interval = self._peixeesperto_settings.sync_interval_seconds if self._peixeesperto_settings else 300
            await asyncio.sleep(max(60, interval))

    async def _sports_event_recheck_scheduler(self) -> None:
        """Reconsulta eventos vinculados e atualiza somente mudanças seguras."""

        if self._sports_event_service is None or self._sports_event_settings is None:
            return
        await self.wait_until_ready()

        while not self.is_closed():
            try:
                rows = self._store.get_due_sports_event_matches(limit=50)
                for row in rows:
                    await self._refresh_sports_event_match(row)
                if rows:
                    logger.info(
                        "Sports event recheck processou %s vinculos; metrics=%s",
                        len(rows),
                        json.dumps(
                            self._sports_event_service.metrics_snapshot(),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "Falha no scheduler de reconsulta esportiva; fluxo principal permanece ativo."
                )
            await asyncio.sleep(
                max(
                    60,
                    self._sports_event_settings.recheck_scheduler_interval_seconds,
                )
            )

    async def _refresh_sports_event_match(self, row: sqlite3.Row) -> None:
        if self._sports_event_service is None or self._sports_event_settings is None:
            return
        source_bet_id = int(row["source_bet_id"])
        provider = str(row["provider"])
        external_event_id = str(row["external_event_id"])
        try:
            payload = json.loads(str(row["payload_json"]))
            tip = tip_from_payload(payload)
            event = await asyncio.to_thread(
                self._sports_event_service.refresh_event,
                provider_name=provider,
                external_event_id=external_event_id,
            )
            if event is None:
                raise RuntimeError("provider_nao_retornou_evento")

            next_check = self._sports_event_service.next_recheck_timestamp(
                event.starts_at_utc,
                event.status,
            )
            previous = datetime.fromisoformat(str(row["starts_at_utc"]))
            applied_value = row["applied_starts_at_utc"] or row["starts_at_utc"]
            applied_datetime = datetime.fromisoformat(str(applied_value))
            changed = abs(
                (
                    event.starts_at_utc
                    - _datetime_as_utc(applied_datetime, "UTC")
                ).total_seconds()
            ) >= 60
            blocked_status = str(event.status or "").casefold() in {
                "cancelled",
                "canceled",
                "canc",
                "postponed",
                "pst",
                "suspended",
                "susp",
                "abandoned",
                "abd",
                "finished",
                "ft",
                "completed",
            }
            action = "checked_no_change"
            if changed and not blocked_status and not tip.is_accumulator:
                await asyncio.to_thread(
                    self._writer.update_bet_datetime,
                    int(row["bet_analytix_bet_id"]),
                    event.starts_at_utc,
                )
                action = "bet_datetime_updated"
                logger.info(
                    "Sports event reagendado no Bet-Analytix: source_bet_id=%s provider=%s external_event_id=%s old=%s new=%s",
                    source_bet_id,
                    provider,
                    external_event_id,
                    previous.isoformat(),
                    event.starts_at_utc.isoformat(),
                )
            elif changed and tip.is_accumulator:
                action = "accumulator_change_not_applied"
            elif blocked_status:
                action = "status_change_not_applied"

            self._store.record_sports_event_refresh(
                source_bet_id=source_bet_id,
                starts_at_utc=event.starts_at_utc.isoformat(),
                event_status=event.status,
                next_check_at_ts=next_check,
                action=action,
                applied_datetime_updated=action == "bet_datetime_updated",
                details={
                    "changed": changed,
                    "blocked_status": blocked_status,
                    "is_accumulator": tip.is_accumulator,
                },
            )
        except Exception as exc:
            retry_at = int(time.time()) + max(
                300,
                self._sports_event_settings.recheck_within_24h_seconds,
            )
            self._store.record_sports_event_refresh_error(
                source_bet_id,
                f"{type(exc).__name__}: {exc}",
                retry_at,
            )
            logger.warning(
                "Sports event recheck adiado: source_bet_id=%s provider=%s error_type=%s",
                source_bet_id,
                provider,
                type(exc).__name__,
            )

    async def _alert_async(self, title: str, details: str | None, dedupe_key: str) -> None:
        if self._alerter is not None:
            await asyncio.to_thread(self._alerter.send, title, details, dedupe_key)


def _is_bookmaker_not_found_error(error_message: str) -> bool:
    """Detecta erro de casa de aposta nao encontrada no catalogo do Bet-Analytix."""

    return "Casa não encontrada no catálogo do Bet-Analytix" in error_message


def _is_unrecoverable_error(error_message: str) -> bool:
    """Detecta erros que nao serao resolvidos com retry (ex: esporte invalido)."""

    return "Esporte não mapeado para ID do Bet-Analytix" in error_message


async def main_async(env_path: str | Path = ".env") -> None:
    """Inicializa o bot Discord e mantem o listener online."""

    base_settings = load_settings(env_path)
    discord_settings = load_discord_reaction_settings(env_path)
    bankroll_settings = load_bankroll_settings(env_path)
    peixeesperto_settings = load_peixeesperto_settings(env_path)
    sports_event_settings = load_sports_event_settings(env_path)
    configure_logging(discord_settings.log_level)
    validate_discord_reaction_settings(discord_settings)

    if not discord_settings.enabled:
        logger.info("DISCORD_REACTION_BOT_ENABLED=false; cliente Discord nao sera iniciado.")
        return

    store = DiscordSignalStore(discord_settings.sqlite_path)
    store.initialize()
    writer_settings = _writer_settings_for_discord(base_settings, discord_settings)
    writer = BetAnalytixWriter(writer_settings)
    alerter = OperationalAlerter(base_settings, "discord")
    notifier = (
        UserbotTelegramNotifier(base_settings, chat_id=discord_settings.notify_chat_id)
        if discord_settings.notify_on_success
        else None
    )
    peixeesperto_sync = (
        PeixeEspertoResultSync(
            settings=peixeesperto_settings,
            store=store,
            writer=writer,
            bookmaker_aliases=discord_settings.bookmaker_aliases,
            notifier=notifier,
        )
        if peixeesperto_settings.enabled
        else None
    )
    sports_event_service: SportsEventService | None = None
    if sports_event_settings.enabled:
        sports_store = SportsScheduleStore(sports_event_settings.cache_path)
        sports_store.initialize()
        sports_event_service = SportsEventService(
            settings=sports_event_settings,
            store=sports_store,
        )
        logger.info(
            "Identificacao esportiva inicializada: mode=%s cache=%s providers=%s",
            sports_event_settings.mode,
            sports_event_settings.cache_path,
            ",".join(sports_event_service.available_providers) or "nenhum",
        )

    bankroll_controller: BankrollController | None = None
    if bankroll_settings.enabled:
        bankroll_controller = BankrollController(
            writer=writer,
            store=store,
            settings=bankroll_settings,
            timezone_name=base_settings.timezone,
        )
        logger.info("Modulo de bankroll habilitado: prefix=%s report_time_utc=%s", bankroll_settings.command_prefix, bankroll_settings.report_time_utc)
        try:
            green_count, green_total, red_count, red_total = bankroll_controller.sync_green_bets()
            logger.info(
                "Sincronizacao inicial: %s greens (R$ %s), %s reds (R$ %s)",
                green_count,
                green_total,
                red_count,
                red_total,
            )
        except Exception:
            logger.exception("Falha na sincronizacao inicial de greens; o scheduler tentara novamente.")

    client = DiscordSignalClient(
        base_settings=base_settings,
        discord_settings=discord_settings,
        store=store,
        writer=writer,
        notifier=notifier,
        alerter=alerter,
        bankroll_settings=bankroll_settings,
        bankroll_controller=bankroll_controller,
        peixeesperto_settings=peixeesperto_settings,
        peixeesperto_sync=peixeesperto_sync,
        sports_event_settings=sports_event_settings,
        sports_event_service=sports_event_service,
    )
    token = discord_settings.bot_token or discord_settings.user_token
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN ou DISCORD_USER_TOKEN precisa estar configurado.")
    await client.start(str(token))


def main() -> None:
    """Ponto de entrada sincrono do listener Discord."""

    parser = argparse.ArgumentParser(description="Bot Discord para copiar sinais externos por reacao.")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Caminho do arquivo .env a ser usado (padrao: .env)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main_async(args.env_file))
    except KeyboardInterrupt:
        logger.info("Cliente Discord interrompido pelo usuario.")
    except Exception:
        logger.exception("Cliente Discord finalizou com erro fatal.")
        raise


def configure_logging(log_level: str) -> None:
    """Configura logs para operacao 24/7."""

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def build_source_bet_id(guild_id: int, channel_id: int, message_id: int) -> int:
    """Gera ID inteiro estavel a partir da origem Discord."""

    digest = hashlib.blake2b(f"discord:{guild_id}:{channel_id}:{message_id}".encode("utf-8"), digest_size=7).digest()
    return int.from_bytes(digest, "big")


def _writer_settings_for_discord(base_settings: Settings, discord_settings: DiscordReactionSettings) -> Settings:
    """Adapta as configuracoes do writer para aceitar o tipster externo."""

    tipster_name = discord_settings.destination_tipster_name
    return replace(
        base_settings,
        target_tipster_name=tipster_name,
        target_tipster_names=(tipster_name,),
        target_tipster_id=None,
        target_tipster_ids=(),
        copytrade_use_source_tipster=True,
        copytrade_destination_tipster_name=tipster_name,
        copytrade_destination_tipster_id=None,
    )


def _pick_label(event: str, pick: str) -> str:
    """Monta a selecao com contexto do evento sempre visivel."""

    cleaned_event = " ".join(event.strip().split())
    cleaned_pick = " ".join(pick.strip().split())
    if not cleaned_event:
        return cleaned_pick
    if not cleaned_pick:
        return cleaned_event
    return f"{cleaned_event}: {cleaned_pick}"


def _message_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_as_utc(value: datetime, local_timezone_name: str) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo(local_timezone_name)).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def _explicit_datetime_audit(
    *,
    source_bet_id: int,
    mode: str,
    sport: str,
    event_name: str,
    explicit_datetime_utc: datetime,
) -> dict[str, Any]:
    return {
        "source_bet_id": source_bet_id,
        "mode": mode,
        "match_status": "explicit_datetime",
        "match_reason": "signal_provided_event_datetime_preserved",
        "sport": sport,
        "signal_participants": _audit_participants(event_name),
        "normalized_signal_participants": None,
        "provider": None,
        "external_event_id": None,
        "participant_home": None,
        "participant_away": None,
        "normalized_event_participants": None,
        "competition": None,
        "country": None,
        "starts_at_utc": explicit_datetime_utc.isoformat(),
        "event_status": None,
        "confidence": 1.0,
        "participant_1_score": 1.0,
        "participant_2_score": 1.0,
        "second_best_confidence": None,
        "candidate_count": 0,
        "providers_consulted": [],
        "reasons": ["explicit_signal_datetime"],
        "from_cache": False,
        "fallback_datetime_utc": explicit_datetime_utc.isoformat(),
        "raw_payload": {},
    }


def _sports_fallback_audit(
    *,
    source_bet_id: int,
    mode: str,
    sport: str,
    event_name: str,
    fallback_datetime_utc: datetime,
    reason: str,
) -> dict[str, Any]:
    return {
        "source_bet_id": source_bet_id,
        "mode": mode,
        "match_status": "fallback",
        "match_reason": reason,
        "sport": sport,
        "signal_participants": _audit_participants(event_name),
        "normalized_signal_participants": None,
        "provider": None,
        "external_event_id": None,
        "participant_home": None,
        "participant_away": None,
        "normalized_event_participants": None,
        "competition": None,
        "country": None,
        "starts_at_utc": None,
        "event_status": None,
        "confidence": 0.0,
        "participant_1_score": 0.0,
        "participant_2_score": 0.0,
        "second_best_confidence": None,
        "candidate_count": 0,
        "providers_consulted": [],
        "reasons": [reason],
        "from_cache": False,
        "fallback_datetime_utc": fallback_datetime_utc.isoformat(),
        "raw_payload": {},
    }


def _audit_participants(event_name: str) -> list[str] | None:
    for marker in (" x ", " X ", " vs ", " VS "):
        parts = event_name.split(marker, 1)
        if len(parts) == 2 and all(part.strip() for part in parts):
            return [parts[0].strip(), parts[1].strip()]
    return None


def _extract_created_bet_id(response: list[dict[str, Any]]) -> int | None:
    if not response:
        return None
    value = response[0].get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _response_already_exists(response: list[dict[str, Any]]) -> bool:
    return any(bool(item.get("already_exists")) for item in response if isinstance(item, dict))


async def _cancel_task(task: asyncio.Task[None]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        return


if __name__ == "__main__":
    main()
