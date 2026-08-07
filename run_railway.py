"""Runner para Railway: sobe N instâncias do bot com ENVs e databases separados.

Uso ( Railway / local ):
    export RAILWAY_INSTANCES=MAIN,RENAN,TARIK
    python run_railway.py

Cada instância é um subprocesso independente. As variáveis podem ser definidas
com prefixo (ex: RENAN_DISCORD_USER_TOKEN) para separar configs no mesmo
service, ou diretamente sem prefixo quando cada instância roda em seu próprio
service no Railway.

Bancos SQLite e sessões Telegram são automaticamente separados por instância,
a menos que o caminho seja explicitamente sobrescrito.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger("railway-runner")


@dataclass(frozen=True)
class InstanceSpec:
    """Descrição de uma instância a ser executada."""

    name: str
    service_type: str
    env: dict[str, str]
    command: list[str]


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def get_instances() -> list[str]:
    """Lê a lista de instâncias a partir de RAILWAY_INSTANCES."""

    raw = os.getenv("RAILWAY_INSTANCES", "").strip()
    if not raw:
        return []
    return [name.strip() for name in raw.split(",") if name.strip()]


def resolve_service_type(
    name: str,
    env: dict[str, str],
    overrides: dict[str, str],
) -> str:
    """Determina o tipo de bot com base em SERVICE_TYPE ou inferência."""

    service_type = (overrides.get("SERVICE_TYPE") or env.get("SERVICE_TYPE", "")).strip().lower()
    if service_type:
        return service_type

    # Inferência simples a partir das variáveis presentes.
    if env.get("TF_DESTINY_DISCORD_USER_TOKEN") and env.get("TF_SOURCE_CHAT_ID"):
        return "forwarder"
    if env.get("DISCORD_USER_TOKEN") or env.get("DISCORD_BOT_TOKEN"):
        return "discord"
    if env.get("TELEGRAM_API_ID") and env.get("TELEGRAM_API_HASH"):
        return "userbot"

    return "main"


def service_command(service_type: str) -> list[str]:
    """Mapeia o tipo de serviço para o script Python correspondente."""

    mapping = {
        "main": [sys.executable, "main.py"],
        "discord": [sys.executable, "discord_reaction_bot.py"],
        "userbot": [sys.executable, "userbot_listener.py"],
        "forwarder": [sys.executable, "run_telegram_forwarder.py"],
    }
    if service_type not in mapping:
        raise RuntimeError(f"Tipo de serviço desconhecido: {service_type}")
    return mapping[service_type]


def apply_sqlite_defaults(
    env: dict[str, str],
    name: str,
    service_type: str,
    overrides: dict[str, str],
) -> None:
    """Garante que cada instância use arquivos SQLite/sessão distintos por padrão."""

    lower_name = name.lower()
    defaults: list[tuple[str, str]] = []

    if service_type == "main":
        defaults.append(("SQLITE_PATH", f"data/{lower_name}_notified_bets.sqlite3"))
    elif service_type == "discord":
        defaults.append(("DISCORD_SQLITE_PATH", f"data/{lower_name}_discord_signals.sqlite3"))
    elif service_type == "userbot":
        defaults.append(("USERBOT_SQLITE_PATH", f"data/{lower_name}_userbot_signals.sqlite3"))
        defaults.append(("TELEGRAM_USERBOT_SESSION", f"data/{lower_name}_telegram_userbot.session"))
    elif service_type == "forwarder":
        defaults.append(("TF_TELEGRAM_SESSION_PATH", f"data/{lower_name}_telegram_forwarder.session"))
        defaults.append(("TF_MEDIA_DOWNLOAD_DIR", f"data/{lower_name}_telegram_forwarder_media"))
        defaults.append(("TF_SQLITE_PATH", f"data/{lower_name}_telegram_forwarder.sqlite3"))

    for var_name, default_path in defaults:
        if var_name not in overrides:
            env[var_name] = default_path


def build_instance_spec(name: str, shared_env: dict[str, str]) -> InstanceSpec:
    """Monta o env e comando de uma instância específica."""

    prefix = f"{name.upper()}_"
    env = dict(shared_env)

    # Variáveis prefixadas viram overrides não-prefixadas.
    overrides: dict[str, str] = {}
    prefixed_keys = [key for key in env if key.startswith(prefix)]
    for key in prefixed_keys:
        new_key = key[len(prefix) :]
        overrides[new_key] = env[key]
        del env[key]

    env.update(overrides)
    service_type = resolve_service_type(name, env, overrides)
    apply_sqlite_defaults(env, name, service_type, overrides)
    command = service_command(service_type)

    return InstanceSpec(name=name, service_type=service_type, env=env, command=command)


def start_instance(spec: InstanceSpec) -> subprocess.Popen[str]:
    """Inicia o subprocesso de uma instância."""

    logger.info("Iniciando instância %s (%s): %s", spec.name, spec.service_type, " ".join(spec.command))
    Path("data").mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        spec.command,
        env=spec.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def stream_output(process: subprocess.Popen[str], prefix: str) -> None:
    """Lê stdout do subprocesso e repassa para o stdout do container com prefixo."""

    try:
        if process.stdout is None:
            return
        for line in process.stdout:
            sys.stdout.write(f"[{prefix}] {line}")
            sys.stdout.flush()
    except Exception:
        logger.exception("Falha ao ler saída da instância %s.", prefix)


def restart_delay_seconds() -> int:
    raw = os.getenv("RAILWAY_RESTART_DELAY_SECONDS", "10")
    try:
        return max(1, int(raw))
    except ValueError:
        return 10


def main() -> None:
    configure_logging()

    instance_names = get_instances()
    if not instance_names:
        logger.error(
            "RAILWAY_INSTANCES não está definida. "
            "Exemplo: RAILWAY_INSTANCES=MAIN,RENAN,TARIK"
        )
        sys.exit(1)

    shared_env = os.environ.copy()
    specs = [build_instance_spec(name, shared_env) for name in instance_names]

    for spec in specs:
        logger.info(
            "Configurada instância %s -> %s (sqlite=%s)",
            spec.name,
            spec.service_type,
            spec.env.get("DISCORD_SQLITE_PATH") or spec.env.get("SQLITE_PATH") or spec.env.get("USERBOT_SQLITE_PATH"),
        )

    processes_by_name: dict[str, subprocess.Popen[str]] = {}
    stop_event = threading.Event()

    def shutdown(signum: int, frame: object | None) -> None:
        logger.info("Sinal de encerramento recebido; parando instâncias...")
        stop_event.set()
        for process in processes_by_name.values():
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    delay = restart_delay_seconds()

    try:
        while not stop_event.is_set():
            for spec in specs:
                process = processes_by_name.get(spec.name)

                # Se o processo morreu, limpa e espera antes de reiniciar.
                if process is not None and process.poll() is not None:
                    logger.warning(
                        "Instância %s finalizou (exit=%s); reiniciando em %ss...",
                        spec.name,
                        process.returncode,
                        delay,
                    )
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    del processes_by_name[spec.name]
                    time.sleep(delay)
                    if stop_event.is_set():
                        break

                # Inicia a instância se ainda não estiver rodando.
                if spec.name not in processes_by_name:
                    process = start_instance(spec)
                    processes_by_name[spec.name] = process
                    thread = threading.Thread(
                        target=stream_output,
                        args=(process, spec.name),
                        daemon=True,
                    )
                    thread.start()

            time.sleep(2)
    finally:
        logger.info("Aguardando encerramento das instâncias ativas...")
        for process in processes_by_name.values():
            if process.poll() is None:
                process.terminate()
        for process in processes_by_name.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
