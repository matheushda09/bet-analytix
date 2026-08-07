"""Orquestracao do redirecionador Telegram -> Discord."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from telegram_forwarder_config import TelegramForwarderSettings
from telegram_forwarder_discord import DiscordForwarderClient
from telegram_forwarder_models import DiscordPayload
from telegram_forwarder_queue import ForwarderQueue
from telegram_forwarder_telegram import TelegramForwarderClient


logger = logging.getLogger(__name__)


class TelegramForwarderCore:
    """OrquestraTelegram -> fila -> Discord com retry e healthcheck."""

    def __init__(self, settings: TelegramForwarderSettings) -> None:
        self._settings = settings
        self._queue = ForwarderQueue(
            max_size=settings.queue_max_size,
            album_debounce_seconds=settings.album_debounce_seconds,
        )
        self._telegram_client = TelegramForwarderClient(settings, self._queue)
        self._discord_client = DiscordForwarderClient(settings)
        self._tasks: list[asyncio.Task[Any]] = []
        self._shutdown_event = asyncio.Event()
        self._messages_forwarded = 0
        self._messages_failed = 0
        self._last_healthcheck = 0.0

    async def run(self) -> None:
        """Inicia todos os componentes e aguarda interrupcao."""

        logger.info("Iniciando redirecionador Telegram -> Discord.")

        # Tarefa do cliente Discord (self-bot).
        discord_task = asyncio.create_task(
            self._run_discord_client(),
            name="discord-forwarder-client",
        )
        self._tasks.append(discord_task)

        # Espera o Discord estar pronto antes de iniciar o consumidor.
        try:
            await asyncio.wait_for(self._discord_client.wait_until_ready(), timeout=120)
        except asyncio.TimeoutError:
            logger.error("Discord forwarder nao ficou pronto em 120s.")
            await self.shutdown()
            return

        # Tarefa consumidora da fila.
        consumer_task = asyncio.create_task(
            self._consume_loop(),
            name="discord-consumer",
        )
        self._tasks.append(consumer_task)

        # Tarefa de healthcheck.
        health_task = asyncio.create_task(
            self._healthcheck_loop(),
            name="healthcheck",
        )
        self._tasks.append(health_task)

        # Tarefa do cliente Telegram (fica em primeiro plano).
        telegram_task = asyncio.create_task(
            self._run_telegram_client(),
            name="telegram-forwarder-client",
        )
        self._tasks.append(telegram_task)

        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            logger.info("Core cancelado; iniciando shutdown.")
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Encerra todos os componentes de forma ordenada."""

        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        logger.info("Encerrando redirecionador Telegram -> Discord...")

        await self._telegram_client.stop()
        await self._discord_client.close()
        await self._queue.close()

        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        logger.info("Redirecionador encerrado.")

    async def _run_discord_client(self) -> None:
        """Mantem o self-bot Discord conectado."""

        try:
            await self._discord_client.start(self._settings.destiny_discord_user_token)
        except Exception:
            logger.exception("Cliente Discord finalizou com erro fatal.")
            raise

    async def _run_telegram_client(self) -> None:
        """Mantem o cliente Telegram conectado."""

        try:
            await self._telegram_client.start()
        except Exception:
            logger.exception("Cliente Telegram finalizou com erro fatal.")
            raise
        finally:
            # Se o client Telegram parar, inicia shutdown geral.
            self._shutdown_event.set()

    async def _consume_loop(self) -> None:
        """Consome payloads da fila e envia ao Discord com retry."""

        while not self._shutdown_event.is_set():
            try:
                payload = await asyncio.wait_for(
                    self._queue.get_payload(),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue

            try:
                await self._process_payload(payload)
            except Exception:
                logger.exception("Falha critica ao processar payload %s; descartando.", payload.telegram_message_id)
                self._messages_failed += 1
            finally:
                self._queue.task_done()

    async def _process_payload(self, payload: DiscordPayload) -> None:
        """Tenta enviar payload ao Discord com retry exponencial."""

        enriched_payload = self._enrich_with_reply(payload)

        for attempt in range(self._settings.retry_max_attempts):
            try:
                await self._discord_client.post_payload(enriched_payload)
                self._messages_forwarded += 1
                return
            except Exception as exc:
                delay = min(
                    300.0,
                    self._settings.retry_base_delay_seconds * (2 ** attempt),
                )
                logger.warning(
                    "Erro ao enviar payload telegram_id=%s ao Discord (tentativa %s/%s): %s. Retry em %.1fs.",
                    payload.telegram_message_id,
                    attempt + 1,
                    self._settings.retry_max_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        logger.error(
            "Payload telegram_id=%s descartado apos %s tentativas.",
            payload.telegram_message_id,
            self._settings.retry_max_attempts,
        )
        self._messages_failed += 1

    def _enrich_with_reply(self, payload: DiscordPayload) -> DiscordPayload:
        """Tenta resolver o reply para uma mensagem ja enviada ao Discord."""

        if payload.reply_to_discord_message_id is not None:
            return payload

        if payload.reply_to_message_id is None:
            return payload

        discord_id = self._discord_client.get_discord_message_id(payload.reply_to_message_id)
        if discord_id is None:
            return payload

        return DiscordPayload(
            telegram_message_id=payload.telegram_message_id,
            telegram_chat_id=payload.telegram_chat_id,
            grouped_id=payload.grouped_id,
            text=payload.text,
            media_files=payload.media_files,
            reply_to_discord_message_id=discord_id,
            sent_at=payload.sent_at,
        )

    async def _healthcheck_loop(self) -> None:
        """Loga heartbeat periodicamente."""

        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self._settings.healthcheck_interval_seconds)
            except asyncio.CancelledError:
                return

            queue_size = self._queue.queue.qsize()
            logger.info(
                "Healthcheck: forwarded=%s failed=%s queue_size=%s",
                self._messages_forwarded,
                self._messages_failed,
                queue_size,
            )
            self._last_healthcheck = time.monotonic()


def configure_logging(log_level: str) -> None:
    """Configura logging estruturado."""

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
