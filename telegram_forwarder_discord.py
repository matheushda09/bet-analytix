"""Cliente Discord bot oficial do redirecionador Telegram -> Discord."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import discord

from telegram_forwarder_config import TelegramForwarderSettings
from telegram_forwarder_models import DiscordPayload
from telegram_forwarder_store import TelegramForwarderStore


logger = logging.getLogger(__name__)

MAX_FILES_PER_MESSAGE = 10
MAX_TEXT_LENGTH = 2000


class DiscordForwarderClient(discord.Client):
    """Self-bot que posta mensagens e comentarios (threads) no canal alvo."""

    def __init__(
        self,
        settings: TelegramForwarderSettings,
        store: TelegramForwarderStore,
    ) -> None:
        # Bot oficial precisa de intents explicitos para ler mensagens, reacoes e criar threads.
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents)

        self._settings = settings
        self._store = store
        self._target_channel_id = settings.destiny_discord_channel_id
        self._guild_id = settings.destiny_discord_guild_id
        self._rate_limit_interval = 1.0 / max(0.01, settings.rate_limit_messages_per_second)
        self._last_send_time: float = 0.0
        self._ready_event = asyncio.Event()
        self._shutdown = False

    async def on_ready(self) -> None:
        """Registra quando o client esta pronto."""

        logger.info(
            "Discord forwarder bot ON: bot_id=%s target_channel_id=%s",
            self.user.id if self.user else None,
            self._target_channel_id,
        )
        self._ready_event.set()

    async def wait_until_ready(self) -> None:
        await self._ready_event.wait()

    async def post_payload(self, payload: DiscordPayload) -> None:
        """Envia uma mensagem normal ao canal Discord."""

        await self.wait_until_ready()

        channel = await self._resolve_channel(self._target_channel_id)

        # Throttle local simples.
        await self._throttle()

        reference = self._build_reference(payload.reply_to_discord_message_id)
        text = self._sanitize_text(payload.text)

        try:
            discord_message = await self._send_to_channel(
                channel=channel,
                text=text,
                files=payload.media_files,
                reference=reference,
                payload=payload,
            )
            await self._persist_mapping(
                payload.telegram_chat_id,
                payload.telegram_message_id,
                discord_message.id,
            )
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
                    files=payload.media_files,
                    reference=None,
                    payload=payload,
                )
                await self._persist_mapping(
                    payload.telegram_chat_id,
                    payload.telegram_message_id,
                    discord_message.id,
                )
                logger.info(
                    "Mensagem enviada ao Discord (sem reply): telegram_id=%s discord_id=%s",
                    payload.telegram_message_id,
                    discord_message.id,
                )
            else:
                raise
        finally:
            self._last_send_time = asyncio.get_event_loop().time()

    async def post_comment(
        self,
        payload: DiscordPayload,
        telegram_channel_message_id: int,
    ) -> None:
        """Envia um comentario do Telegram como mensagem numa thread do Discord."""

        await self.wait_until_ready()
        await self._throttle()

        channel = await self._resolve_channel(self._target_channel_id)

        thread_id = self._store.get_discord_thread_id(telegram_channel_message_id)
        thread: discord.Thread | None = None

        if thread_id is not None:
            thread = self.get_channel(thread_id)
            if thread is None:
                try:
                    thread = await self.fetch_channel(thread_id)
                except Exception:
                    logger.warning("Thread %s nao encontrada; criando nova.", thread_id)
                    thread = None

        if thread is None:
            discord_message_id = self._store.get_discord_message_id(
                self._settings.source_chat_id,
                telegram_channel_message_id,
            )
            if discord_message_id is None:
                raise RuntimeError(
                    f"Mensagem do canal {telegram_channel_message_id} ainda nao foi enviada ao Discord; "
                    "nao e possivel criar thread de comentarios."
                )

            parent_message = await channel.fetch_message(discord_message_id)
            thread_name = self._thread_name_from_text(payload.text)
            try:
                thread = await channel.create_thread(
                    name=thread_name,
                    message=parent_message,
                    type=discord.ChannelType.public_thread,
                    auto_archive_duration=10080,  # 7 dias
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Falha ao criar thread para mensagem {telegram_channel_message_id}: {exc}"
                ) from exc

            self._store.save_thread_mapping(telegram_channel_message_id, thread.id)
            logger.info(
                "Thread criada no Discord: telegram_channel_message_id=%s discord_thread_id=%s",
                telegram_channel_message_id,
                thread.id,
            )

        if not isinstance(thread, discord.Thread):
            raise RuntimeError(f"Canal retornado nao e uma thread: {type(thread).__name__}")

        text = self._sanitize_text(payload.text)
        await self._send_to_channel(
            channel=thread,
            text=text,
            files=payload.media_files,
            reference=None,
            payload=payload,
        )
        logger.info(
            "Comentario enviado para thread: telegram_message_id=%s discord_thread_id=%s",
            payload.telegram_message_id,
            thread.id,
        )

        self._last_send_time = asyncio.get_event_loop().time()

    async def _resolve_channel(self, channel_id: int) -> discord.TextChannel | discord.Thread:
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError(
                f"Canal Discord invalido ou inacessivel: {channel_id} (tipo={type(channel).__name__})"
            )
        return channel

    async def _throttle(self) -> None:
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_send_time
        if elapsed < self._rate_limit_interval:
            await asyncio.sleep(self._rate_limit_interval - elapsed)

    def _build_reference(self, discord_message_id: int | None) -> discord.MessageReference | None:
        if discord_message_id is None:
            return None
        try:
            return discord.MessageReference(
                message_id=discord_message_id,
                channel_id=self._target_channel_id,
                guild_id=self._guild_id,
            )
        except Exception:
            logger.exception("Falha ao montar message reference; prosseguindo sem reply.")
            return None

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

    async def _persist_mapping(
        self,
        telegram_chat_id: int,
        telegram_message_id: int,
        discord_message_id: int,
    ) -> None:
        """Persiste mapeamento no SQLite e mantem cache em memoria."""

        try:
            self._store.save_message_mapping(
                telegram_chat_id,
                telegram_message_id,
                discord_message_id,
            )
        except Exception:
            logger.exception(
                "Falha ao persistir mapeamento telegram_chat_id=%s telegram_message_id=%s",
                telegram_chat_id,
                telegram_message_id,
            )

    def resolve_discord_message_id(
        self,
        telegram_chat_id: int,
        telegram_message_id: int,
    ) -> int | None:
        """Resolve o discord_message_id a partir do store."""

        return self._store.get_discord_message_id(telegram_chat_id, telegram_message_id)

    @staticmethod
    def _sanitize_text(text: str) -> str:
        """Garante que o texto nao exceda o limite do Discord."""

        if not text:
            return ""
        if len(text) <= MAX_TEXT_LENGTH:
            return text
        return text[: MAX_TEXT_LENGTH - 3] + "..."

    @staticmethod
    def _thread_name_from_text(text: str) -> str:
        """Gera um nome curto para a thread baseado no texto."""

        clean = text.replace("\n", " ").strip()
        if not clean:
            return "Comentarios"
        if len(clean) <= 80:
            return clean
        return clean[:77] + "..."

    async def close(self) -> None:
        self._shutdown = True
        await super().close()
