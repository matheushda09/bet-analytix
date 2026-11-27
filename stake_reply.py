"""Interpretacao da resposta do usuario com uma nova stake (reais ou dolares).

Regras de moeda:
    R$, R, r, reais, real, BRL           -> reais
    $, US$, U$, USD, dolar(es)           -> dolares (convertidos para reais)
    sem simbolo                          -> reais (a bankroll e em reais)

Tolera espacos e a moeda antes ou depois do numero: "R$43", "R$   43",
"$ 41", "41$", "r41", "41 reais", "1.250,00", "43.5".
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass

import requests


logger = logging.getLogger(__name__)

BRL = "BRL"
USD = "USD"

# Ordem importa: tokens de dolar com "$" antes de "R$"/"$" isolado.
_CURRENCY_TOKENS: tuple[tuple[str, str], ...] = (
    ("us$", USD),
    ("u$", USD),
    ("usd", USD),
    ("dolares", USD),
    ("dolar", USD),
    ("r$", BRL),
    ("brl", BRL),
    ("reais", BRL),
    ("real", BRL),
    ("$", USD),
    ("r", BRL),
)
_NUMBER_PATTERN = re.compile(r"\d[\d.,]*")


class StakeReplyError(ValueError):
    """Resposta que nao e um valor de stake valido."""


@dataclass(frozen=True)
class StakeReply:
    amount: float
    currency: str


def parse_stake_reply(text: str) -> StakeReply:
    """Extrai valor e moeda da resposta; erro se houver texto alem disso."""

    normalized = _strip_accents(str(text or "")).casefold().strip()
    # Remove formatacao comum do Discord e pontuacao final.
    normalized = normalized.replace("*", "").replace("`", "").replace("_", " ").strip(" .!")
    numbers = _NUMBER_PATTERN.findall(normalized)
    if len(numbers) != 1:
        raise StakeReplyError("envie um unico valor, ex.: R$43 ou $8")

    number_text = numbers[0]
    rest = normalized.replace(number_text, " ", 1)
    rest = re.sub(r"\s+", "", rest)

    currency = BRL
    if rest:
        matched = None
        for token, token_currency in _CURRENCY_TOKENS:
            if rest == token:
                matched = token_currency
                break
        if matched is None:
            raise StakeReplyError("moeda nao reconhecida; use R$ para reais ou $ para dolares")
        currency = matched

    amount = _parse_number(number_text)
    if amount <= 0:
        raise StakeReplyError("o valor precisa ser maior que zero")
    return StakeReply(amount=amount, currency=currency)


def _parse_number(value: str) -> float:
    cleaned = value.strip(".,")
    if "," in cleaned and "." in cleaned:
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        cleaned = cleaned.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in cleaned:
        # Virgula unica e decimal (padrao BR); varias sao separador de milhar.
        if cleaned.count(",") > 1:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")
    elif "." in cleaned:
        parts = cleaned.split(".")
        # "1.250" / "1.250.000" sao milhares; "43.5" / "43.50" sao decimais.
        if len(parts) > 2 or len(parts[-1]) == 3:
            cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError as exc:
        raise StakeReplyError(f"valor invalido: {value}") from exc


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


class UsdBrlRateProvider:
    """Cotacao USD->BRL: fixa por ENV ou consultada online com cache."""

    def __init__(
        self,
        fixed_rate: float | None = None,
        url: str = "https://economia.awesomeapi.com.br/json/last/USD-BRL",
        cache_seconds: float = 1800,
        timeout_seconds: float = 10,
    ) -> None:
        self._fixed_rate = fixed_rate
        self._url = url
        self._cache_seconds = cache_seconds
        self._timeout_seconds = timeout_seconds
        self._cached_rate: float | None = None
        self._cached_at = 0.0
        self._lock = threading.Lock()

    def get_rate(self) -> float | None:
        if self._fixed_rate:
            return self._fixed_rate
        with self._lock:
            if self._cached_rate and time.monotonic() - self._cached_at < self._cache_seconds:
                return self._cached_rate
            try:
                response = requests.get(self._url, timeout=self._timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                rate = float(payload["USDBRL"]["bid"])
            except Exception as exc:
                logger.warning("Nao foi possivel consultar cotacao USD-BRL: %s", exc)
                return self._cached_rate
            if rate <= 0:
                return self._cached_rate
            self._cached_rate = rate
            self._cached_at = time.monotonic()
            return rate


def stake_in_brl(reply: StakeReply, signal_rate: float | None, provider: UsdBrlRateProvider | None) -> tuple[float, float | None]:
    """Converte a resposta para reais. Retorna (stake_brl, cotacao_usada)."""

    if reply.currency == BRL:
        return round(reply.amount, 2), None
    rate = signal_rate or (provider.get_rate() if provider is not None else None)
    if not rate:
        raise StakeReplyError("nao consegui obter a cotacao do dolar; envie o valor em reais (ex.: R$43)")
    return round(reply.amount * rate, 2), rate
