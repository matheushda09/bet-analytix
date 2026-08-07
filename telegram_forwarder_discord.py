"""Cliente Discord self-bot do redirecionador Telegram -> Discord."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord

from telegram_forwarder_config import TelegramForwarderSettings
from telegram_forwarder_models import DiscordPayload


logger = logging.getLogger(__name__)

MAX_FILES_PER_MESSAGE = 10
MAX_TEXT_LENGTH = 2000


class DiscordForwarderClient(discord.Client):
    """Self-bot que apenas posta mensagens no canal alvo."""

    def __init__(self, settings: TelegramForwarderSettings) -> None:
        # Intents minimos; self-bot nao precisa de intents especiais para enviar.
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self._settings = settings
        self._target_channel_id = settings.destiny_discord_channel_id
        self._guild_id = settings.destiny_discord_guild_id
        self._rate_limit_interval = 1.0 / max(0.01, settings.rate_limit_messages_per_second)
        self._last_send_time: float = 0.0
        self._message_map: dict[int, int] = {}
        self._map_lock = asyncio.Lock()
        self._max_map_size = 5000
        self._ready_event = asyncio.Event()
        self._shutdown = False

    async def on_ready(self) -> None:
        """Registra quando o client esta pronto."""

        logger.info(
            "Discord forwarder client ON: user_id=%s target_channel_id=%s",
            self.user.id if self.user else None,
            self._target_channel_id,
        )
        self._ready_event.set()

    async def wait_until_ready(self) -> None:
        await self._ready_event.wait()

    async def post_payload(self, payload: DiscordPayload) -> None:
        """Envia um payload ao canal Discord."""

        await self.wait_until_ready()

        channel = self.get_channel(self._target_channel_id)
        if channel is None:
            channel = await self.fetch_channel(self._target_channel_id)

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError(
                f"Canal Discord invalido ou inacessivel: {self._target_channel_id} (tipo={type(channel).__name__})"
            )

        # Throttle local simples.
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_send_time
        if elapsed < self._rate_limit_interval:
            await asyncio.sleep(self._rate_limit_interval - elapsed)

        reference = None
        if payload.reply_to_discord_message_id is not None:
            try:
                reference = discord.MessageReference(
                    message_id=payload.reply_to_discord_message_id,
                    channel_id=self._target_channel_id,
                    guild_id=self._guild_id,
                )
            except Exception:
                logger.exception("Falha ao montar message reference; prosseguindo sem reply.")

        text = self._sanitize_text(payload.text)
        files = payload.media_files

        try:
            discord_message = await self._send_to_channel(
                channel=channel,
                text=text,
                files=files,
                reference=reference,
                payload=payload,
            )
            await self._register_mapping(payload.telegram_message_id, discord_message.id)
            logger.info(
                "Mensagem enviada ao Discord: telegram_id=%s discord_id=%s",
                payload.telegram_message_id,
                discord_message.id,
            )
        except discord.HTTPException as exc:
            if reference is not None and exc.code in {50035, 10008, 10014}:
                logger.warning(
                    "Reply falhou para telegram_id=%s; enviando sem referencia.",
                    payload.telegram_message_id,
                )
                discord_message = await self._send_to_channel(
                    channel=channel,
                    text=text,
                    files=files,
                    reference=None,
                    payload=payload,
                )
                await self._register_mapping(payload.telegram_message_id, discord_message.id)
                logger.info(
                    "Mensagem enviada ao Discord (sem reply): telegram_id=%s discord_id=%s",
                    payload.telegram_message_id,
                    discord_message.id,
                )
            else:
                raise
        finally:
            self._last_send_time = asyncio.get_event_loop().time()

    async def _send_to_channel(
        self,
        channel: discord.TextChannel | discord.Thread,
        text: str,
        files: list[Path],
        reference: discord.MessageReference | None,
        payload: DiscordPayload,
    ) -> discord.Message:
        """Envia texto e/ou arquivos ao canal, retornando a mensagem enviada."""

        if not files:
            return await channel.send(
                content=text,
                reference=reference,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return await self._send_with_files(
            channel=channel,
            text=text,
            files=files,
            reference=reference,
            payload=payload,
        )

    async def _send_with_files(
        self,
        channel: discord.TextChannel | discord.Thread,
        text: str,
        files: list[Path],
        reference: discord.MessageReference | None,
        payload: DiscordPayload,
    ) -> discord.Message:
        """Envia mensagens respeitando o limite de 10 arquivos por mensagem do Discord."""

        remaining = list(files)
        first_discord_message: discord.Message | None = None

        while remaining:
            batch = remaining[:MAX_FILES_PER_MESSAGE]
            remaining = remaining[MAX_FILES_PER_MESSAGE:]

            discord_files = [discord.File(str(path)) for path in batch if path.exists()]
            if not discord_files and not remaining and first_discord_message is None:
                # Todos os arquivos falharam no download; envia texto.
                discord_message = await channel.send(
                    content=text,
                    reference=reference,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                discord_message = await channel.send(
                    content=text if first_discord_message is None else "",
                    files=discord_files or None,
                    reference=reference if first_discord_message is None else None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            if first_discord_message is None:
                first_discord_message = discord_message

            logger.info(
                "Mensagem com midia enviada ao Discord: telegram_id=%s discord_id=%s arquivos=%s",
                payload.telegram_message_id,
                discord_message.id,
                len(discord_files),
            )

            # Rate limit entre batches.
            if remaining:
                await asyncio.sleep(self._rate_limit_interval)

        if first_discord_message is None:
            # Fallback teorico: envia texto puro.
            first_discord_message = await channel.send(
                content=text,
                reference=reference,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        # Limpa arquivos temporarios se configurado.
        if not self._settings.media_keep_after_send:
            for path in files:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    logger.debug("Falha ao remover arquivo temporario: %s", path)

        return first_discord_message

    async def _register_mapping(self, telegram_message_id: int, discord_message_id: int) -> None:
        """Mantem mapeamento telegram_id -> discord_id para replies futuros."""

        async with self._map_lock:
            self._message_map[telegram_message_id] = discord_message_id
            # Evita crescimento ilimitado; remove entradas mais antigas.
            if len(self._message_map) > self._max_map_size:
                keys_to_remove = list(self._message_map.keys())[: len(self._message_map) - self._max_map_size]
                for key in keys_to_remove:
                    del self._message_map[key]

    def get_discord_message_id(self, telegram_message_id: int) -> int | None:
        return self._message_map.get(telegram_message_id)

    @staticmethod
    def _sanitize_text(text: str) -> str:
        """Garante que o texto nao exceda o limite do Discord."""

        if not text:
            return ""
        if len(text) <= MAX_TEXT_LENGTH:
            return text
        return text[: MAX_TEXT_LENGTH - 3] + "..."

    async def close(self) -> None:
        self._shutdown = True
        await super().close()
