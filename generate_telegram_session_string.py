"""Gera uma StringSession do Telethon para uso no Railway.

Uso:
    python generate_telegram_session_string.py

Digite o codigo enviado para o celular. A StringSession sera salva em
TF_TELEGRAM_SESSION_STRING no arquivo .env local (apenas para referencia)
e tambem impressa no terminal. Cole apenas a string no Railway.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

ENV_PATH = Path(".env")


def _load_int(name: str, default: int = 0) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    return int(value)


def _load_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


async def main() -> None:
    load_dotenv(ENV_PATH)

    api_id = _load_int("TF_TELEGRAM_API_ID", _load_int("TELEGRAM_API_ID"))
    api_hash = _load_str("TF_TELEGRAM_API_HASH", _load_str("TELEGRAM_API_HASH"))
    phone = _load_str("TF_TELEGRAM_PHONE", _load_str("TELEGRAM_PHONE"))

    if not api_id or not api_hash or not phone:
        logger.error("Preencha TF_TELEGRAM_API_ID, TF_TELEGRAM_API_HASH e TF_TELEGRAM_PHONE no .env")
        return

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        await client.send_code_request(phone)
        code = input("Digite o codigo Telegram: ").strip()
        try:
            await client.sign_in(phone, code)
        except Exception:
            password = getpass.getpass("Conta com 2FA. Digite a senha: ")
            await client.sign_in(password=password)

    session_string = client.session.save()
    await client.disconnect()

    logger.info("\n\n=== TF_TELEGRAM_SESSION_STRING ===\n%s\n====================================", session_string)
    logger.info("Cole a string acima no Railway como variavel TF_TELEGRAM_SESSION_STRING.")


if __name__ == "__main__":
    asyncio.run(main())
