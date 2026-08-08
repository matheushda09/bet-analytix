"""Cliente Telethon do redirecionador Telegram -> Discord."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events
from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
    User,
    Channel,
    Chat,
)

from telegram_forwarder_config import TelegramForwarderSettings
from telegram_forwarder_models import TelegramMedia, TelegramMessage, build_discord_payload, DiscordPayload
from telegram_forwarder_queue import ForwarderQueue


logger = logging.getLogger(__name__)


class TelegramForwarderClient:
    """Cliente MTProto passivo: apenas le mensagens, nunca interage no Telegram."""

    def __init__(
        self,
        settings: TelegramForwarderSettings,
        queue: ForwarderQueue,
    ) -> None:
        self._settings = settings
        self._queue = queue

        # Garante que os diretorios existem antes de instanciar o TelegramClient,
        # pois o arquivo .session e um banco SQLite e precisa de pasta valida.
        settings.telegram_session_path.parent.mkdir(parents=True, exist_ok=True)
        self._media_dir = settings.media_download_dir
        self._media_dir.mkdir(parents=True, exist_ok=True)

        self._client = TelegramClient(
            str(settings.telegram_session_path),
            settings.telegram_api_id,
            settings.telegram_api_hash,
            # System version neutro para nao chamar atencao.
            system_version="4.16.30-vxCUSTOM",
            device_model="Desktop",
            app_version="1.0",
            # Reconexao automatica gerenciada pelo Telethon.
            auto_reconnect=True,
            connection_retries=10 ** 6,
            request_retries=5,
            retry_delay=5,
            flood_sleep_threshold=120,
        )
        self._running = False
        self._message_handler_registered = False

    async def start(self) -> None:
        """Inicia o cliente e escuta mensagens."""

        logger.info(
            "Iniciando cliente Telethon: session=%s source_chat_id=%s",
            self._settings.telegram_session_path,
            self._settings.source_chat_id,
        )

        await self._client.connect()
        if not await self._client.is_user_authorized():
            await self._interactive_login()

        me = await self._client.get_me()
        logger.info("Cliente Telethon conectado: user_id=%s", me.id if me else None)

        if not self._message_handler_registered:
            chats = [self._settings.source_chat_id]
            if self._settings.source_comments_chat_id is not None:
                chats.append(self._settings.source_comments_chat_id)
            self._client.add_event_handler(
                self._on_new_message,
                events.NewMessage(chats=chats),
            )
            self._message_handler_registered = True

        self._running = True
        await self._client.run_until_disconnected()

    async def _interactive_login(self) -> None:
        """Solicita codigo e 2FA interativamente quando a sessao nao existe."""

        logger.info("Sessao nao autorizada; solicitando codigo para %s", self._settings.telegram_phone)
        await self._client.send_code_request(self._settings.telegram_phone)

        code = await self._ask_input(
            f"Digite o codigo Telegram enviado para {self._settings.telegram_phone}: "
        )
        if not code:
            raise RuntimeError("Codigo de autorizacao nao fornecido.")

        try:
            await self._client.sign_in(self._settings.telegram_phone, code)
        except Exception as first_exc:
            error_text = str(first_exc)
            if "2FA" in error_text or "password" in error_text.lower():
                password = await self._ask_input("Conta com 2FA. Digite a senha: ")
                if not password:
                    raise RuntimeError("Senha 2FA nao fornecida.") from first_exc
                await self._client.sign_in(password=password)
            else:
                raise
        logger.info("Sessao Telegram autorizada e salva em %s.", self._settings.telegram_session_path)

    async def _ask_input(self, prompt: str) -> str:
        """Le input do terminal sem bloquear o event loop do asyncio."""

        try:
            return (await asyncio.to_thread(input, prompt)).strip()
        except EOFError:
            raise RuntimeError(
                "Entrada interativa indisponivel. "
                "Autorize a sessao localmente em um terminal real."
            )

    async def stop(self) -> None:
        """Para o cliente de forma limpa."""

        self._running = False
        try:
            await self._client.disconnect()
        except Exception:
            logger.exception("Falha ao desconectar cliente Telethon.")

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        """Handler de novas mensagens nos chats monitorados."""

        message = event.message
        if message is None:
            return

        # Ignora mensagens de servico (entrada/saida de membros, fixacao etc.).
        if getattr(message, "action", None) is not None:
            logger.debug("Mensagem de servico ignorada: message_id=%s", message.id)
            return

        chat_id = message.chat_id

        try:
            if chat_id == self._settings.source_chat_id:
                await self._handle_channel_message(message)
            elif chat_id == self._settings.source_comments_chat_id:
                await self._handle_comment_message(message)
            else:
                logger.debug("Mensagem ignorada de chat nao monitorado: chat_id=%s", chat_id)
        except Exception:
            logger.exception(
                "Falha ao processar mensagem Telegram message_id=%s; listener continua.",
                getattr(message, "id", None),
            )

    async def _handle_channel_message(self, message) -> None:
        """Processa uma mensagem do canal de origem."""

        tg_message, media_files = await self._extract_message(message)
        if tg_message is None:
            return
        logger.info(
            "Mensagem Telegram capturada: message_id=%s chat_id=%s grouped_id=%s sender=%s media=%s",
            tg_message.telegram_message_id,
            tg_message.telegram_chat_id,
            tg_message.grouped_id,
            tg_message.sender_name,
            len(media_files),
        )
        await self._queue.put_message(tg_message, media_files)

    async def _handle_comment_message(self, message) -> None:
        """Processa um comentario do grupo de discussao vinculado."""

        channel_message_id = self._resolve_comment_target(message)
        if channel_message_id is None:
            logger.debug(
                "Comentario ignorado pois nao conseguiu identificar mensagem do canal: message_id=%s",
                message.id,
            )
            return

        tg_message, media_files = await self._extract_message(message)
        if tg_message is None:
            return

        logger.info(
            "Comentario Telegram capturado: message_id=%s chat_id=%s target_channel_message_id=%s sender=%s",
            tg_message.telegram_message_id,
            tg_message.telegram_chat_id,
            channel_message_id,
            tg_message.sender_name,
        )

        # Sobrescreve flags para identificar como comentario.
        payload = build_discord_payload(tg_message, media_files)
        object.__setattr__(payload, "is_comment", True)
        object.__setattr__(payload, "telegram_channel_message_id", channel_message_id)
        await self._queue.enqueue(payload)

    def _resolve_comment_target(self, message) -> int | None:
        """Tenta extrair o ID da mensagem do canal original a partir de um comentario."""

        reply_to = getattr(message, "reply_to", None)
        if reply_to is None:
            return None

        # Comentarios no Telegram referenciam a mensagem do canal via reply_to_msg_id.
        for attr in ("reply_to_msg_id", "reply_to_top_id"):
            value = getattr(reply_to, attr, None)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue

        # Fallback: reply_to pode ter um atributo message_id direto em algumas versoes.
        message_id = getattr(reply_to, "message_id", None)
        if message_id is not None:
            try:
                return int(message_id)
            except (TypeError, ValueError):
                pass

        return None

    async def _extract_message(
        self,
        message,
    ) -> tuple[TelegramMessage | None, list[Path]]:
        """Extrai texto, metadados e midia de uma mensagem do Telegram."""

        sender_name = await self._resolve_sender_name(message)

        is_reply = bool(message.reply_to_msg_id)
        reply_to_message_id = int(message.reply_to_msg_id) if message.reply_to_msg_id else None
        reply_to_text: str | None = None
        reply_to_sender_name: str | None = None

        if reply_to_message_id is not None:
            try:
                reply_message = await message.get_reply_message()
                if reply_message is not None:
                    reply_to_text = reply_message.text or ""
                    reply_to_sender_name = await self._resolve_sender_name(reply_message)
            except Exception:
                logger.debug("Nao foi possivel carregar mensagem de reply para message_id=%s", message.id)

        text = message.text or ""
        sent_at = message.date.replace(tzinfo=timezone.utc) if message.date else datetime.now(timezone.utc)

        media_files: list[Path] = []
        media_entries: list[TelegramMedia] = []

        if message.media is not None:
            try:
                media_entries, media_files = await self._download_media(message)
            except Exception as exc:
                logger.warning(
                    "Nao foi possivel baixar midia da mensagem message_id=%s: %s. Enviando texto/caption.",
                    message.id,
                    exc,
                )

        tg_message = TelegramMessage(
            telegram_message_id=int(message.id),
            telegram_chat_id=int(message.chat_id),
            grouped_id=int(message.grouped_id) if message.grouped_id else None,
            sender_id=int(message.sender_id) if message.sender_id else None,
            sender_name=sender_name,
            sent_at=sent_at,
            text=text,
            is_reply=is_reply,
            reply_to_message_id=reply_to_message_id,
            reply_to_text=reply_to_text,
            reply_to_sender_name=reply_to_sender_name,
            media=media_entries,
            raw_type_name=type(message.media).__name__ if message.media else "message",
        )

        return tg_message, media_files

    async def _resolve_sender_name(self, message) -> str | None:
        """Resolve o nome do remetente sem gerar requisicoes desnecessarias."""

        sender = await message.get_sender()
        if isinstance(sender, User):
            parts = [p for p in (sender.first_name, sender.last_name) if p]
            return " ".join(parts) or sender.username or str(sender.id)
        if isinstance(sender, (Channel, Chat)):
            return sender.title or str(sender.id)
        return str(message.sender_id) if message.sender_id else None

    async def _download_media(self, message) -> tuple[list[TelegramMedia], list[Path]]:
        """Baixa midia localmente para reenvio ao Discord."""

        media_entries: list[TelegramMedia] = []
        file_paths: list[Path] = []

        ext_hint = self._media_extension(message.media)
        file_name = f"{message.id}_{message.chat_id}{ext_hint}"
        file_path = self._media_dir / file_name

        try:
            downloaded_path = await message.download_media(file=str(file_path))
        except Exception as exc:
            logger.warning("Falha no download_media de message_id=%s: %s", message.id, exc)
            return media_entries, file_paths

        if downloaded_path is None:
            return media_entries, file_paths

        downloaded_path = Path(downloaded_path)
        file_paths.append(downloaded_path)

        is_voice = False
        is_video_note = False
        is_sticker = False
        mime_type: str | None = None
        width: int | None = None
        height: int | None = None
        duration: int | None = None

        if isinstance(message.media, MessageMediaDocument):
            document = message.media.document
            if document:
                mime_type = document.mime_type
                attrs = {type(a).__name__: a for a in document.attributes}
                if "DocumentAttributeAudio" in attrs:
                    attr = attrs["DocumentAttributeAudio"]
                    is_voice = bool(getattr(attr, "voice", False))
                    duration = int(getattr(attr, "duration", 0) or 0) or None
                if "DocumentAttributeVideo" in attrs:
                    attr = attrs["DocumentAttributeVideo"]
                    is_video_note = bool(getattr(attr, "round_message", False))
                    width = int(getattr(attr, "w", 0) or 0) or None
                    height = int(getattr(attr, "h", 0) or 0) or None
                    duration = int(getattr(attr, "duration", 0) or 0) or None
                if "DocumentAttributeSticker" in attrs:
                    is_sticker = True
        elif isinstance(message.media, MessageMediaPhoto):
            mime_type = "image/jpeg"

        media_entries.append(
            TelegramMedia(
                file_path=downloaded_path,
                mime_type=mime_type,
                file_name=downloaded_path.name,
                is_voice=is_voice,
                is_video_note=is_video_note,
                is_sticker=is_sticker,
                width=width,
                height=height,
                duration_seconds=duration,
            )
        )

        return media_entries, file_paths

    def _media_extension(self, media: Any) -> str:
        """Tenta adivinhar a extensao do arquivo para facilitar o upload no Discord."""

        if isinstance(media, MessageMediaPhoto):
            return ".jpg"
        if isinstance(media, MessageMediaDocument) and media.document:
            mime = media.document.mime_type or ""
            ext_map = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "image/webp": ".webp",
                "video/mp4": ".mp4",
                "audio/ogg": ".ogg",
                "audio/mpeg": ".mp3",
                "application/pdf": ".pdf",
            }
            for m, e in ext_map.items():
                if mime.startswith(m):
                    return e
            # Fallback a partir do file name se disponivel.
            for attr in media.document.attributes:
                if type(attr).__name__ == "DocumentAttributeFilename":
                    fn = getattr(attr, "file_name", "")
                    if "." in fn:
                        return os.path.splitext(fn)[1]
        return ""
