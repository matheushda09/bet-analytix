"""Entrypoint do redirecionador Telegram -> Discord."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from telegram_forwarder_config import load_telegram_forwarder_settings
from telegram_forwarder_core import TelegramForwarderCore, configure_logging


logger = logging.getLogger(__name__)


async def main_async(env_path: str) -> None:
    """Carrega configuracoes e inicia o redirecionador."""

    settings = load_telegram_forwarder_settings(env_path)
    configure_logging(settings.log_level)

    if not settings.enabled:
        logger.info("TELEGRAM_FORWARDER_ENABLED=false; redirecionador nao sera iniciado.")
        return

    core = TelegramForwarderCore(settings)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(core.shutdown()))

    try:
        await core.run()
    except Exception:
        logger.exception("Redirecionador finalizou com erro fatal.")
        raise


def main() -> None:
    """Ponto de entrada sincrono."""

    parser = argparse.ArgumentParser(description="Redirecionador silencioso Telegram -> Discord.")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Caminho do arquivo .env a ser usado (padrao: .env)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main_async(args.env_file))
    except KeyboardInterrupt:
        logger.info("Redirecionador interrompido pelo usuario.")
    except Exception:
        logger.exception("Redirecionador finalizou com erro fatal.")
        sys.exit(1)


if __name__ == "__main__":
    main()
