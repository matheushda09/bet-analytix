"""Bootstrap da sessao Telethon a partir de variavel de ambiente base64.

Uso:
    export TF_TELEGRAM_SESSION_B64="$(cat /app/data/telegram_forwarder.session | base64 -w 0)"
    python telegram_forwarder_session_bootstrap.py

Se o arquivo /app/data/telegram_forwarder.session nao existir e a variavel
TF_TELEGRAM_SESSION_B64 estiver definida, o script decodifica e salva a sessao.
Isso torna o forwarder resistente a perda do volume no Railway.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from pathlib import Path


logger = logging.getLogger(__name__)

DEFAULT_SESSION_PATH = Path("/app/data/telegram_forwarder.session")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def restore_session(session_path: Path) -> bool:
    """Restaura a sessao a partir de TF_TELEGRAM_SESSION_B64 se necessario."""

    if session_path.exists():
        logger.info("Sessao Telethon ja existe: %s", session_path)
        return False

    b64_env = os.getenv("TF_TELEGRAM_SESSION_B64", "").strip()
    if not b64_env:
        logger.warning(
            "Sessao nao encontrada e TF_TELEGRAM_SESSION_B64 nao definida. "
            "Autorize manualmente no console do Railway."
        )
        return False

    try:
        raw = base64.b64decode(b64_env, validate=True)
    except Exception as exc:
        logger.error("TF_TELEGRAM_SESSION_B64 invalida: %s", exc)
        return False

    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_bytes(raw)
    logger.info("Sessao Telethon restaurada de TF_TELEGRAM_SESSION_B64 em %s", session_path)
    return True


def main() -> int:
    configure_logging()

    session_path = Path(os.getenv("TF_TELEGRAM_SESSION_PATH", str(DEFAULT_SESSION_PATH)))
    restore_session(session_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
