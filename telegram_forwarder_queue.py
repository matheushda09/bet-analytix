"""Fila interna com debounce de albuns do Telegram."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from telegram_forwarder_models import TelegramMessage, build_discord_payload, DiscordPayload


logger = logging.getLogger(__name__)


@dataclass
class _PendingAlbum:
    messages: list[TelegramMessage] = field(default_factory=list)
    debounce_task: asyncio.Task[None] | None = None


class ForwarderQueue:
    """Buffer de albuns + fila asyncio de payloads prontos."""

    def __init__(
        self,
        max_size: int,
        album_debounce_seconds: float,
        on_payload_ready: Callable[[DiscordPayload], None] | None = None,
    ) -> None:
        self._queue: asyncio.Queue[DiscordPayload] = asyncio.Queue(maxsize=max_size)
        self._album_debounce_seconds = album_debounce_seconds
        self._on_payload_ready = on_payload_ready
        self._pending_albums: dict[int, _PendingAlbum] = {}
        self._closed = False

    @property
    def queue(self) -> asyncio.Queue[DiscordPayload]:
        return self._queue

    async def put_message(self, message: TelegramMessage, media_files: list[Path]) -> None:
        """Recebe uma mensagem individual e a enfileira ou agrupa em album."""

        if self._closed:
            logger.warning("Fila fechada; mensagem %s descartada.", message.telegram_message_id)
            return

        if message.grouped_id is None:
            payload = build_discord_payload(message, media_files)
            await self.enqueue(payload)
            return

        await self._add_to_album(message, media_files)

    async def _add_to_album(self, message: TelegramMessage, media_files: list[Path]) -> None:
        grouped_id = message.grouped_id
        pending = self._pending_albums.get(grouped_id)

        if pending is None:
            pending = _PendingAlbum()
            self._pending_albums[grouped_id] = pending
            logger.debug("Album iniciado: grouped_id=%s", grouped_id)

        # Cancela timer anterior se houver.
        if pending.debounce_task is not None and not pending.debounce_task.done():
            pending.debounce_task.cancel()
            try:
                await pending.debounce_task
            except asyncio.CancelledError:
                pass

        pending.messages.append(message)
        pending.debounce_task = asyncio.create_task(
            self._album_flush_after(grouped_id, self._album_debounce_seconds),
            name=f"album-flush-{grouped_id}",
        )

    async def _album_flush_after(self, grouped_id: int, delay: float) -> None:
        """Aguarda o debounce e libera o album."""

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        pending = self._pending_albums.pop(grouped_id, None)
        if pending is None or not pending.messages:
            return

        logger.info(
            "Flush de album: grouped_id=%s mensagens=%s",
            grouped_id,
            len(pending.messages),
        )

        # Ordena por ID do Telegram para manter a ordem original.
        pending.messages.sort(key=lambda m: m.telegram_message_id)

        # O texto do album fica na primeira mensagem; mídias de todas.
        combined_text_parts: list[str] = []
        all_media_files: list[Path] = []

        for msg in pending.messages:
            if msg.text:
                combined_text_parts.append(msg.text)
            all_media_files.extend([Path(m.file_path) for m in msg.media])

        first = pending.messages[0]
        combined_text = "\n\n".join(combined_text_parts)

        # Cria payload com texto combinado e metadados da primeira mensagem.
        payload = DiscordPayload(
            telegram_message_id=first.telegram_message_id,
            telegram_chat_id=first.telegram_chat_id,
            grouped_id=first.grouped_id,
            text=combined_text,
            media_files=all_media_files,
            reply_to_telegram_message_id=first.reply_to_message_id,
            reply_to_discord_message_id=None,
            sent_at=first.sent_at,
        )

        await self.enqueue(payload)

    async def enqueue(self, payload: DiscordPayload) -> None:
        """Metodo publico para adicionar um payload pronto diretamente a fila."""

        await self._enqueue(payload)

    async def _enqueue(self, payload: DiscordPayload) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(payload)
            logger.debug(
                "Payload enfileirado: telegram_message_id=%s media=%s",
                payload.telegram_message_id,
                len(payload.media_files),
            )
            if self._on_payload_ready is not None:
                try:
                    self._on_payload_ready(payload)
                except Exception:
                    logger.exception("Falha no callback de payload pronto.")
        except asyncio.QueueFull:
            logger.error("Fila de payloads cheia; descartando mensagem %s.", payload.telegram_message_id)

    async def get_payload(self) -> DiscordPayload:
        """Bloqueia ate haver um payload disponivel."""

        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def close(self) -> None:
        """Cancela timers pendentes e fecha a fila."""

        self._closed = True
        for pending in self._pending_albums.values():
            if pending.debounce_task is not None and not pending.debounce_task.done():
                pending.debounce_task.cancel()
        self._pending_albums.clear()
