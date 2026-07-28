"""Cliente autenticado para registrar apostas na bankroll do Bet-Analytix."""

from __future__ import annotations

import json
import logging
import os
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from rapidfuzz import fuzz, process

from config import Settings
from message_parser import ParsedTelegramTip
from models import build_tipster_map, to_int


logger = logging.getLogger(__name__)

# Score minimo (0-100) para aceitar fuzzy match de casa.
BOOKMAKER_FUZZY_MATCH_THRESHOLD = int(os.getenv("BOOKMAKER_FUZZY_MATCH_THRESHOLD", "80"))


class BetAnalytixWriterError(RuntimeError):
    """Erro ao registrar uma aposta no Bet-Analytix."""


class BetAnalytixWriter:
    """Cliente HTTP para login e criação de apostas no Bet-Analytix."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = requests.Session()
        self._access_token: str | None = settings.bet_analytix_access_token
        self._refresh_token: str | None = None
        self._bookmakers_by_name: dict[str, int] | None = None
        self._bookmaker_ids: set[int] | None = None
        self._fixed_destination_tipster_id: int | None = settings.copytrade_destination_tipster_id
        self._destination_user_id: int | None = None
        self._destination_tipsters_by_name: dict[str, int] | None = None

    def create_bet(self, tip: ParsedTelegramTip) -> list[dict[str, Any]]:
        """Registra a tip parseada na bankroll de destino."""

        self._ensure_authenticated()
        existing_bet_id = self.find_existing_copytrade_bet(tip.source_bet_id)
        if existing_bet_id is not None:
            logger.info(
                "Aposta source_bet_id=%s já existe na bankroll de destino como bet_id=%s.",
                tip.source_bet_id,
                existing_bet_id,
            )
            return [{"id": existing_bet_id, "already_exists": True}]

        bookmaker_id = self._resolve_bookmaker_id(tip.bookmaker)
        sport_id = self._resolve_sport_id(tip.sport)
        tipster_id = self._resolve_destination_tipster_id(tip.tipster)
        event_date, event_time = self._bet_analytix_datetime_fields(tip.event_datetime)
        existing_bet_id = self.find_existing_matching_copytrade_bet(
            tip=tip,
            bookmaker_id=bookmaker_id,
            sport_id=sport_id,
            tipster_id=tipster_id,
            event_timestamp=_bet_analytix_timestamp(event_date, event_time),
        )
        if existing_bet_id is not None:
            logger.info(
                "Aposta source_bet_id=%s ja existe na bankroll de destino por equivalencia como bet_id=%s.",
                tip.source_bet_id,
                existing_bet_id,
            )
            return [{"id": existing_bet_id, "already_exists": True}]

        payload = {
            "bankroll": self._settings.copytrade_bankroll_internal_id,
            "date": event_date,
            "time": event_time,
            "selections": [
                {
                    "id": None,
                    "isExpanded": True,
                    "label": tip.pick,
                    "odds": f"{tip.odd:.3f}",
                    "sport": sport_id,
                    "status": 0,
                    "showDetails": False,
                    "category": None,
                    "competition": None,
                    "betType": None,
                    "closing": None,
                    "estimatedProbability": None,
                }
            ],
            "type": 1,
            "systemCombination": [],
            "stakes": {"single": tip.stake},
            "overallLabel": tip.event,
            "bookmaker": bookmaker_id,
            "tipster": tipster_id,
            "category": None,
            "commission": {
                "amount": None,
                "percentage": None,
                "base": None,
                "applyOnLoss": False,
            },
            "bonus": None,
            "live": False,
            "freebet": False,
            "cashout": None,
            "eachway": None,
            "masked": False,
            "note": _build_note(tip),
            "stake": tip.stake,
        }

        response = self._request_json("POST", "/bet", json_payload=payload, authenticated=True)
        if not isinstance(response, list):
            raise BetAnalytixWriterError(f"Resposta inesperada ao criar aposta: {response!r}")
        return [item for item in response if isinstance(item, dict)]

    def create_accumulator_bet(self, tip: ParsedTelegramTip) -> list[dict[str, Any]]:
        """Registra a tip como uma aposta multipla/combinada na bankroll de destino.

        A aposta sempre contem 2 selecoes: a primeira com os dados reais da tip
        e a segunda com label "Múltipla" e odd 1.000. Isso garante que o
        Bet-Analytix trate o bilhete como multipla sem distorcer a odd total.
        """

        self._ensure_authenticated()
        existing_bet_id = self.find_existing_copytrade_bet(tip.source_bet_id)
        if existing_bet_id is not None:
            logger.info(
                "Aposta multipla source_bet_id=%s ja existe na bankroll de destino como bet_id=%s.",
                tip.source_bet_id,
                existing_bet_id,
            )
            return [{"id": existing_bet_id, "already_exists": True}]

        bookmaker_id = self._resolve_bookmaker_id(tip.bookmaker)
        sport_id = self._resolve_sport_id(tip.sport)
        tipster_id = self._resolve_destination_tipster_id(tip.tipster)
        event_date, event_time = self._bet_analytix_datetime_fields(tip.event_datetime)
        existing_bet_id = self.find_existing_matching_copytrade_bet(
            tip=tip,
            bookmaker_id=bookmaker_id,
            sport_id=sport_id,
            tipster_id=tipster_id,
            event_timestamp=_bet_analytix_timestamp(event_date, event_time),
        )
        if existing_bet_id is not None:
            logger.info(
                "Aposta multipla source_bet_id=%s ja existe na bankroll de destino por equivalencia como bet_id=%s.",
                tip.source_bet_id,
                existing_bet_id,
            )
            return [{"id": existing_bet_id, "already_exists": True}]

        payload = {
            "bankroll": self._settings.copytrade_bankroll_internal_id,
            "date": event_date,
            "time": event_time,
            "selections": [
                {
                    "id": None,
                    "label": tip.pick,
                    "odds": f"{tip.odd:.3f}",
                    "sport": sport_id,
                    "status": 0,
                    "category": None,
                    "competition": None,
                    "betType": None,
                    "closing": None,
                    "estimatedProbability": None,
                },
                {
                    "id": None,
                    "label": "Múltipla",
                    "odds": "1.000",
                    "sport": sport_id,
                    "status": 0,
                    "category": None,
                    "competition": None,
                    "betType": None,
                    "closing": None,
                    "estimatedProbability": None,
                },
            ],
            "type": 2,
            "systemCombination": [],
            "stakes": {"combi": tip.stake},
            "overallLabel": tip.event if tip.event else "Múltipla 2 Apostas",
            "bookmaker": bookmaker_id,
            "tipster": tipster_id,
            "category": None,
            "commission": {
                "amount": None,
                "percentage": None,
                "base": None,
                "applyOnLoss": False,
            },
            "bonus": None,
            "live": False,
            "freebet": False,
            "cashout": None,
            "eachway": None,
            "masked": False,
            "note": _build_note(tip),
            "stake": tip.stake,
        }

        response = self._request_json("POST", "/bet", json_payload=payload, authenticated=True)
        if not isinstance(response, list):
            raise BetAnalytixWriterError(f"Resposta inesperada ao criar aposta multipla: {response!r}")
        return [item for item in response if isinstance(item, dict)]

    def update_bet_datetime(self, bet_id: int, event_datetime: datetime) -> dict[str, Any] | None:
        """Atualiza exclusivamente a data/hora de uma aposta simples existente.

        O metodo busca a aposta atual no Bet-Analytix, remonta o payload
        preservando todos os campos relevantes e troca apenas `date`/`time`.
        Por seguranca, ele recusa apostas que nao sejam simples (`type == 1`).
        """

        self._ensure_authenticated()
        raw_bet = self._request_json(
            "GET",
            f"/bet/{bet_id}",
            authenticated=True,
            allow_existing_bet_read=True,
        )
        if not isinstance(raw_bet, dict):
            raise BetAnalytixWriterError(f"Resposta inesperada ao buscar aposta {bet_id}: {raw_bet!r}")

        payload = self._build_datetime_update_payload(bet_id, raw_bet, event_datetime)
        response = self._request_json(
            "PUT",
            f"/bet/{bet_id}",
            json_payload=payload,
            authenticated=True,
            allow_existing_bet_update=True,
        )
        if response is None:
            return None
        if isinstance(response, list):
            first_item = response[0] if response else None
            if isinstance(first_item, dict):
                return first_item
        if not isinstance(response, dict):
            raise BetAnalytixWriterError(f"Resposta inesperada ao atualizar aposta {bet_id}: {response!r}")
        return response

    def update_bet_odd_stake(self, bet_id: int, odd: float, stake: float) -> dict[str, Any] | None:
        """Atualiza exclusivamente odd e stake de uma aposta simples existente."""

        self._ensure_authenticated()
        raw_bet = self._request_json(
            "GET",
            f"/bet/{bet_id}",
            authenticated=True,
            allow_existing_bet_read=True,
        )
        if not isinstance(raw_bet, dict):
            raise BetAnalytixWriterError(f"Resposta inesperada ao buscar aposta {bet_id}: {raw_bet!r}")

        payload = self._build_odd_stake_update_payload(bet_id, raw_bet, odd, stake)
        response = self._request_json(
            "PUT",
            f"/bet/{bet_id}",
            json_payload=payload,
            authenticated=True,
            allow_existing_bet_update=True,
        )
        if response is None:
            return None
        if isinstance(response, list):
            first_item = response[0] if response else None
            if isinstance(first_item, dict):
                return first_item
        if not isinstance(response, dict):
            raise BetAnalytixWriterError(f"Resposta inesperada ao atualizar aposta {bet_id}: {response!r}")
        return response

    def update_bet_state(
        self,
        bet_id: int,
        state: int,
        profit: float | None = None,
        allow_non_pending: bool = False,
    ) -> dict[str, Any] | None:
        """Atualiza o estado (green/red/cashout) de uma aposta simples existente.

        `state` segue a enumeracao do Bet-Analytix:
            0 = pendente, 1 = ganha, 2 = perdida, 6 = cashout.
        Se `profit` for informado, tambem ajusta `gain` e `profit` da aposta.
        Por seguranca, so atualiza apostas ainda pendentes (state=0), a menos que
        `allow_non_pending=True` seja informado.
        """

        self._ensure_authenticated()
        try:
            raw_bet = self._request_json(
                "GET",
                f"/bet/{bet_id}",
                authenticated=True,
                allow_existing_bet_read=True,
            )
        except BetAnalytixWriterError as exc:
            if "404" in str(exc):
                logger.warning("Aposta bet_id=%s nao encontrada no Bet-Analytix (404); sync ignorado.", bet_id)
                return None
            raise
        if not isinstance(raw_bet, dict):
            raise BetAnalytixWriterError(f"Resposta inesperada ao buscar aposta {bet_id}: {raw_bet!r}")

        current_state = to_int(raw_bet.get("state"))
        if current_state != 0 and not allow_non_pending:
            logger.warning(
                "Aposta bet_id=%s nao esta pendente (state=%s); sync ignorado.",
                bet_id,
                current_state,
            )
            return None

        payload = self._build_state_update_payload(bet_id, raw_bet, state, profit)
        response = self._request_json(
            "PUT",
            f"/bet/{bet_id}",
            json_payload=payload,
            authenticated=True,
            allow_existing_bet_update=True,
        )
        if response is None:
            return None
        if isinstance(response, list):
            first_item = response[0] if response else None
            if isinstance(first_item, dict):
                return first_item
        if not isinstance(response, dict):
            raise BetAnalytixWriterError(f"Resposta inesperada ao atualizar aposta {bet_id}: {response!r}")
        return response

    def _build_state_update_payload(
        self,
        bet_id: int,
        raw_bet: dict[str, Any],
        state: int,
        profit: float | None,
    ) -> dict[str, Any]:
        """Monta payload de edicao preservando campos e alterando state/gain/profit."""

        bet_type = to_int(raw_bet.get("type"))
        if bet_type != 1:
            raise BetAnalytixWriterError(
                f"Ajuste automatico de estado bloqueado: aposta {bet_id} nao e simples (type={bet_type})."
            )

        label = str(raw_bet.get("label") or "").strip()
        if not label:
            raise BetAnalytixWriterError(f"Aposta {bet_id} sem label; ajuste de estado bloqueado.")

        event_date, event_time = _bet_timestamp_fields(raw_bet.get("date"))
        commission = _commission_payload(raw_bet.get("commission"))
        stake = float(str(raw_bet.get("stake") or "0").replace(",", ".")) or 0.0
        odds = float(str(raw_bet.get("odds") or "0").replace(",", ".")) or 1.0

        if profit is None:
            # Calcula o lucro com base na stake real da aposta no Bet-Analytix,
            # garantindo consistencia quando o valor sincronizado do PeixeEsperto
            # diverge da stake local (ex: conversao de unidades, apostas parciais).
            if state == 1:
                profit_value = round(stake * (odds - 1.0), 2)
                gain_value = round(stake * odds, 2)
            elif state == 2:
                profit_value = round(-stake, 2)
                gain_value = 0.0
            elif state in {3, 7}:
                # Reembolsada (3) ou cancelada (7): stake devolvida, lucro zero.
                profit_value = 0.0
                gain_value = stake
            else:
                profit_value = None
                gain_value = None
        else:
            profit_value = profit
            if state == 1:
                gain_value = round(stake + profit, 2)
            elif state == 6:
                gain_value = round(stake + profit, 2)
            elif state == 2:
                gain_value = 0.0
            elif state in {3, 7}:
                gain_value = round(stake + profit, 2)
            else:
                gain_value = None

        cashout_value = gain_value if state == 6 else None

        return {
            "id": bet_id,
            "bankroll": to_int(raw_bet.get("bankroll")) or self._settings.copytrade_bankroll_internal_id,
            "date": event_date,
            "time": event_time,
            "selections": [
                {
                    "id": bet_id,
                    "label": label,
                    "odds": raw_bet.get("odds"),
                    "sport": to_int(raw_bet.get("sport")),
                    "status": state,
                    "category": raw_bet.get("category") or None,
                    "competition": raw_bet.get("competition") or None,
                    "betType": raw_bet.get("bet_type") or None,
                    "closing": raw_bet.get("closing") or None,
                    "estimatedProbability": raw_bet.get("estimated_probability") or None,
                }
            ],
            "type": 1,
            "systemCombination": [],
            "stakes": {"single": raw_bet.get("stake")},
            "overallLabel": label,
            "bookmaker": to_int(raw_bet.get("bookmaker")) or raw_bet.get("bookmaker"),
            "tipster": to_int(raw_bet.get("tipster")),
            "category": raw_bet.get("category") or None,
            "commission": commission,
            "bonus": raw_bet.get("bonus") or None,
            "live": _truthy(raw_bet.get("live")),
            "freebet": _truthy(raw_bet.get("freebet")),
            "cashout": cashout_value,
            "eachway": raw_bet.get("eachway") or None,
            "masked": _truthy(raw_bet.get("masked_bet") or raw_bet.get("masked")),
            "note": raw_bet.get("comment") or None,
            "stake": raw_bet.get("stake"),
            "state": state,
            "gain": gain_value if gain_value is not None else raw_bet.get("gain"),
            "profit": profit_value if profit_value is not None else raw_bet.get("profit"),
        }

    def find_existing_copytrade_bet(self, source_bet_id: int) -> int | None:
        """Procura uma aposta já criada a partir do mesmo `source_bet_id`.

        Isso evita duplicidade se o processo cair depois do `POST /bet` e antes
        de marcar o job como concluído no SQLite.
        """

        marker = f"source_bet_id={source_bet_id}"
        page = 1
        while page <= self._settings.copytrade_duplicate_check_max_pages:
            payload = self._request_json(
                "GET",
                f"/bankroll/{self._settings.copytrade_bankroll_id}/bets/paginated?page={page}",
                authenticated=True,
            )
            if not isinstance(payload, dict):
                return None

            bets = payload.get("bets")
            if not isinstance(bets, list):
                return None

            for bet in bets:
                if not isinstance(bet, dict):
                    continue
                comment = str(bet.get("comment") or "")
                if marker in comment:
                    bet_id = to_int(bet.get("id"))
                    if bet_id is not None:
                        return bet_id

            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
            total_pages = to_int(pagination.get("totalPages")) or page
            has_next_page = bool(pagination.get("hasNextPage"))
            if page >= total_pages or not has_next_page:
                break
            page += 1
        return None

    def find_existing_matching_copytrade_bet(
        self,
        tip: ParsedTelegramTip,
        bookmaker_id: int,
        sport_id: int,
        tipster_id: int,
        event_timestamp: int,
    ) -> int | None:
        """Procura uma aposta equivalente sem depender do comentario.

        O comentario enviado ao Bet-Analytix pode ficar limpo, contendo apenas
        a odd justa. Esta busca ainda cobre a janela rara em que o POST deu
        certo e o processo caiu antes de persistir o sucesso no SQLite.
        """

        page = 1
        while page <= self._settings.copytrade_duplicate_check_max_pages:
            payload = self._request_json(
                "GET",
                f"/bankroll/{self._settings.copytrade_bankroll_id}/bets/paginated?page={page}",
                authenticated=True,
            )
            if not isinstance(payload, dict):
                return None

            bets = payload.get("bets")
            if not isinstance(bets, list):
                return None

            for bet in bets:
                if not isinstance(bet, dict):
                    continue
                if _is_matching_existing_bet(
                    bet=bet,
                    tip=tip,
                    bookmaker_id=bookmaker_id,
                    sport_id=sport_id,
                    tipster_id=tipster_id,
                    event_timestamp=event_timestamp,
                ):
                    bet_id = to_int(bet.get("id"))
                    if bet_id is not None:
                        return bet_id

            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
            total_pages = to_int(pagination.get("totalPages")) or page
            has_next_page = bool(pagination.get("hasNextPage"))
            if page >= total_pages or not has_next_page:
                break
            page += 1
        return None

    def fetch_bankroll_summary(self) -> dict[str, Any]:
        """Retorna os metadados e estatisticas da bankroll de destino.

        O campo `stats_global` vem como string JSON; o metodo faz o parse
        e devolve o dicionario ja normalizado para consumo externo.
        """

        self._ensure_authenticated()
        bankroll = self._request_json(
            "GET",
            f"/bankroll/{self._settings.copytrade_bankroll_id}",
            authenticated=True,
        )
        if not isinstance(bankroll, dict):
            raise BetAnalytixWriterError("Nao foi possivel buscar a bankroll de destino.")

        stats_global = bankroll.get("stats_global")
        if isinstance(stats_global, str):
            try:
                bankroll["stats_global"] = json.loads(stats_global)
            except json.JSONDecodeError as exc:
                raise BetAnalytixWriterError(f"stats_global nao e JSON valido: {exc}") from exc
        return bankroll

    def fetch_bankroll_bets_page(
        self,
        page: int = 1,
        status_filter: str | None = None,
    ) -> dict[str, Any]:
        """Retorna uma pagina de apostas da bankroll de destino.

        `status_filter` pode ser usado futuramente para filtrar por status;
        por enquanto a API publica retorna todas as apostas e o filtro e
        aplicado pelo consumidor.
        """

        self._ensure_authenticated()
        path = f"/bankroll/{self._settings.copytrade_bankroll_id}/bets/paginated?page={page}"
        payload = self._request_json("GET", path, authenticated=True)
        if not isinstance(payload, dict):
            raise BetAnalytixWriterError(f"Resposta inesperada ao buscar apostas: {payload!r}")
        return payload

    def get_bookmaker_name(self, bookmaker_id: int | str) -> str | None:
        """Resolve o nome amigavel de uma casa a partir do seu id."""

        resolved_id: int | None = to_int(bookmaker_id)
        if resolved_id is None:
            return None
        self._load_bookmakers()
        if self._bookmakers_by_name is None:
            return None
        for name, id_ in self._bookmakers_by_name.items():
            if id_ == resolved_id:
                return name
        return None

    def _load_bookmakers(self) -> None:
        """Carrega o catalogo de bookmakers uma unica vez."""

        if self._bookmakers_by_name is not None:
            return
        payload = self._request_json("GET", "/bookmakers", authenticated=False)
        if not isinstance(payload, list):
            raise BetAnalytixWriterError("Catalogo de bookmakers retornou payload inesperado.")
        self._bookmakers_by_name = {
            _normalize_text(str(item.get("name"))): int(item["id"])
            for item in payload
            if isinstance(item, dict) and item.get("id") is not None and item.get("name")
        }
        self._bookmaker_ids = {
            int(item["id"])
            for item in payload
            if isinstance(item, dict) and item.get("id") is not None
        }

    def _ensure_authenticated(self) -> None:
        if self._access_token:
            return
        if not self._settings.bet_analytix_email or not self._settings.bet_analytix_password:
            raise BetAnalytixWriterError("Credenciais BET_ANALYTIX_EMAIL/BET_ANALYTIX_PASSWORD ausentes.")
        self._login()

    def _login(self) -> None:
        payload = {
            "email": self._settings.bet_analytix_email,
            "password": self._settings.bet_analytix_password,
        }
        response = self._request_json("POST", "/auth/login", json_payload=payload, authenticated=False)
        if not isinstance(response, dict) or not response.get("accessToken"):
            raise BetAnalytixWriterError("Login no Bet-Analytix não retornou accessToken.")
        self._access_token = str(response["accessToken"])
        self._refresh_token = str(response.get("refreshToken") or "")
        logger.info("Login no Bet-Analytix realizado com sucesso.")

    def _resolve_bookmaker_id(self, bookmaker_name: str) -> int:
        self._load_bookmakers()

        bookmaker_id_from_text = _bookmaker_id_from_text(bookmaker_name)
        if bookmaker_id_from_text is not None and bookmaker_id_from_text in (self._bookmaker_ids or set()):
            return bookmaker_id_from_text

        normalized_input = _normalize_text(bookmaker_name)
        bookmaker_id = self._bookmakers_by_name.get(normalized_input)
        if bookmaker_id is not None:
            return bookmaker_id

        bet_variant_id = self._resolve_bookmaker_id_bet_variant(bookmaker_name, normalized_input)
        if bet_variant_id is not None:
            return bet_variant_id

        fuzzy_id = self._resolve_bookmaker_id_fuzzy(bookmaker_name, normalized_input)
        if fuzzy_id is not None:
            return fuzzy_id

        raise BetAnalytixWriterError(f"Casa não encontrada no catálogo do Bet-Analytix: {bookmaker_name}")

    def _resolve_bookmaker_id_bet_variant(self, bookmaker_name: str, normalized_input: str) -> int | None:
        """Tenta equivalencia exata adicionando ou removendo o sufixo ``bet``."""

        if not self._bookmakers_by_name:
            return None

        variants = _bookmaker_bet_variants(normalized_input)
        variants.discard(normalized_input)
        for variant in variants:
            matched_id = self._bookmakers_by_name.get(variant)
            if matched_id is None:
                continue
            logger.info(
                "Match de casa por variacao BET: '%s' -> '%s' (id=%s).",
                bookmaker_name,
                variant,
                matched_id,
            )
            return matched_id
        return None

    def _resolve_bookmaker_id_fuzzy(self, bookmaker_name: str, normalized_input: str) -> int | None:
        """Tenta encontrar a casa mais proxima usando fuzzy matching.

        Estrategia:
        1. fuzz.ratio >= threshold: captura erros de digitacao/acentuacao
           (ex: "Bet 365" -> "Bet365").
        2. fuzz.partial_ratio >= 95 com input >= 5 caracteres: captura
           substrings (ex: "Pitaco" -> "Rei Do Pitaco").
        """

        if not self._bookmakers_by_name:
            return None

        choices = list(self._bookmakers_by_name.keys())

        best_match = process.extractOne(
            normalized_input,
            choices,
            scorer=fuzz.ratio,
            score_cutoff=BOOKMAKER_FUZZY_MATCH_THRESHOLD,
        )
        if best_match is not None:
            matched_name, score, _ = best_match
            matched_id = self._bookmakers_by_name[matched_name]
            logger.warning(
                "Fuzzy match de casa (ratio): '%s' -> '%s' (score=%s, id=%s).",
                bookmaker_name,
                matched_name,
                score,
                matched_id,
            )
            return matched_id

        if len(normalized_input) >= 5:
            best_partial = process.extractOne(
                normalized_input,
                choices,
                scorer=fuzz.partial_ratio,
                score_cutoff=95,
            )
            if best_partial is not None:
                matched_name, score, _ = best_partial
                matched_id = self._bookmakers_by_name[matched_name]
                logger.warning(
                    "Fuzzy match de casa (partial): '%s' -> '%s' (score=%s, id=%s).",
                    bookmaker_name,
                    matched_name,
                    score,
                    matched_id,
                )
                return matched_id

        return None

    def _resolve_sport_id(self, sport_name: str) -> int:
        normalized = _normalize_text(sport_name)
        for sport_id, configured_name in self._settings.sport_names.items():
            if _normalize_text(configured_name) == normalized:
                return sport_id
        logger.warning(
            "Esporte '%s' não mapeado no Bet-Analytix. Usando fallback Futebol (id=1).",
            sport_name,
        )
        return 1

    def _resolve_legacy_destination_tipster_id(self) -> int:
        raise BetAnalytixWriterError("Metodo legado desativado; use o tipster vindo da mensagem.")

    def _bet_analytix_datetime_fields(self, event_datetime: datetime) -> tuple[str, str]:
        """Converte a data/hora local da mensagem para os campos UTC da API.

        O Bet-Analytix grava `date` + `time` como UTC. Se enviarmos `22:00`
        diretamente, a tela em America/Sao_Paulo exibe `19:00`. Por isso, a
        data/hora lida do Telegram e marcada com `APP_TIMEZONE` e convertida
        para UTC antes do `POST /bet`.
        """

        local_timezone = self._local_timezone()
        if event_datetime.tzinfo is None:
            local_datetime = event_datetime.replace(tzinfo=local_timezone)
        else:
            local_datetime = event_datetime.astimezone(local_timezone)

        utc_datetime = local_datetime.astimezone(timezone.utc)
        return utc_datetime.strftime("%Y-%m-%d"), utc_datetime.strftime("%H:%M")

    def _local_timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self._settings.timezone)
        except ZoneInfoNotFoundError as exc:
            raise BetAnalytixWriterError(f"Timezone invalido em APP_TIMEZONE: {self._settings.timezone}") from exc

    def _resolve_destination_tipster_id(self, source_tipster_name: str) -> int:
        if not self._settings.copytrade_use_source_tipster:
            return self._resolve_fixed_destination_tipster_id()

        self._validate_source_tipster_name(source_tipster_name)
        destination_tipster_name = self._mapped_destination_tipster_name(source_tipster_name)
        resolved_id = self._find_destination_tipster_id(destination_tipster_name)
        if resolved_id is not None:
            return resolved_id

        if not self._settings.copytrade_auto_create_tipsters:
            raise BetAnalytixWriterError(
                f"Tipster de destino nao encontrado: {destination_tipster_name}"
            )

        logger.info("Tipster de destino '%s' nao existe; criando no Bet-Analytix.", destination_tipster_name)
        self._create_tipster(destination_tipster_name)
        resolved_id = self._find_destination_tipster_id(destination_tipster_name, refresh=True)
        if resolved_id is None:
            raise BetAnalytixWriterError(
                f"Tipster criado, mas nao encontrado apos refresh: {destination_tipster_name}"
            )
        return resolved_id

    def _resolve_fixed_destination_tipster_id(self) -> int:
        if self._fixed_destination_tipster_id is not None:
            return self._fixed_destination_tipster_id

        resolved_id = self._find_destination_tipster_id(self._settings.copytrade_destination_tipster_name)
        if resolved_id is None:
            raise BetAnalytixWriterError(
                f"Tipster de destino nao encontrado: {self._settings.copytrade_destination_tipster_name}"
            )
        self._fixed_destination_tipster_id = resolved_id
        return resolved_id

    def _mapped_destination_tipster_name(self, source_tipster_name: str) -> str:
        for source_name, destination_name in self._settings.copytrade_tipster_mapping.items():
            if _normalize_text(source_name) == _normalize_text(source_tipster_name):
                mapped_name = destination_name.strip()
                if not mapped_name:
                    raise BetAnalytixWriterError(f"Mapeamento de tipster vazio para: {source_tipster_name}")
                return mapped_name
        cleaned_name = source_tipster_name.strip()
        if not cleaned_name:
            raise BetAnalytixWriterError("Mensagem do Telegram nao trouxe tipster valido.")
        return cleaned_name

    def _validate_source_tipster_name(self, source_tipster_name: str) -> None:
        allowed_names = set(self._settings.target_tipster_names)
        allowed_names.update(self._settings.copytrade_tipster_mapping.keys())
        if any(_normalize_text(name) == _normalize_text(source_tipster_name) for name in allowed_names):
            return
        raise BetAnalytixWriterError(
            f"Tipster da mensagem nao esta na lista monitorada: {source_tipster_name}"
        )

    def _find_destination_tipster_id(self, tipster_name: str, refresh: bool = False) -> int | None:
        tipsters_by_name = self._load_destination_tipsters_by_name(refresh=refresh)
        return tipsters_by_name.get(_normalize_text(tipster_name))

    def _load_destination_tipsters_by_name(self, refresh: bool = False) -> dict[str, int]:
        if self._destination_tipsters_by_name is not None and not refresh:
            return self._destination_tipsters_by_name

        user_id = self._resolve_destination_user_id()
        all_data = self._request_json("GET", f"/bankroll/all-data/{user_id}", authenticated=True)
        if not isinstance(all_data, dict):
            raise BetAnalytixWriterError("Referencias da bankroll de destino retornaram payload inesperado.")

        tipsters = build_tipster_map(all_data)
        self._destination_tipsters_by_name = {
            _normalize_text(tipster.name): tipster.id
            for tipster in tipsters.values()
        }
        return self._destination_tipsters_by_name

    def _resolve_destination_user_id(self) -> int:
        if self._destination_user_id is not None:
            return self._destination_user_id

        bankroll = self._request_json(
            "GET",
            f"/bankroll/{self._settings.copytrade_bankroll_id}",
            authenticated=True,
        )
        if not isinstance(bankroll, dict):
            raise BetAnalytixWriterError("Nao foi possivel buscar a bankroll de destino.")
        user_id = to_int(bankroll.get("id_user"))
        if user_id is None:
            raise BetAnalytixWriterError("Bankroll de destino nao retornou id_user.")

        self._destination_user_id = user_id
        return user_id

    def _create_tipster(self, tipster_name: str) -> None:
        self._request_json(
            "POST",
            "/tipster",
            json_payload={"name": tipster_name, "web": None},
            authenticated=True,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        json_payload: dict[str, Any] | None = None,
        authenticated: bool = False,
        allow_existing_bet_read: bool = False,
        allow_existing_bet_update: bool = False,
    ) -> Any:
        self._assert_non_destructive_request(
            method,
            path,
            allow_existing_bet_read=allow_existing_bet_read,
            allow_existing_bet_update=allow_existing_bet_update,
        )
        url = f"{self._settings.api_base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(self._settings.request_max_retries + 1):
            try:
                response = self._session.request(
                    method=method,
                    url=url,
                    json=json_payload,
                    headers=self._headers(authenticated=authenticated),
                    timeout=self._settings.request_timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self._settings.request_max_retries:
                    break
                self._sleep_before_retry(attempt, reason=str(exc))
                continue

            if response.status_code in {401, 403} and authenticated:
                logger.warning("Token Bet-Analytix expirado ou recusado; refazendo login.")
                self._access_token = None
                self._login()
                continue

            if response.status_code == 429 or 500 <= response.status_code < 600:
                logger.warning("Bet-Analytix retornou HTTP %s em %s.", response.status_code, url)
                if attempt >= self._settings.request_max_retries:
                    raise BetAnalytixWriterError(f"HTTP {response.status_code} persistente no Bet-Analytix.")
                self._sleep_before_retry(attempt, response=response)
                continue

            if response.status_code >= 400:
                raise BetAnalytixWriterError(f"Bet-Analytix retornou HTTP {response.status_code}: {response.text[:500]}")

            if response.status_code == 204 or not response.text.strip():
                return None

            try:
                return response.json()
            except ValueError as exc:
                if method.upper() in {"POST", "PUT", "DELETE"} and 200 <= response.status_code < 300:
                    logger.warning(
                        "Bet-Analytix retornou HTTP %s sem JSON em %s; tratando como sucesso.",
                        response.status_code,
                        url,
                    )
                    return None
                raise BetAnalytixWriterError(f"Resposta do Bet-Analytix não é JSON válido: {exc}") from exc

        raise BetAnalytixWriterError(f"Falha ao chamar Bet-Analytix após retries: {last_error}")

    def _headers(self, authenticated: bool) -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "app": self._settings.app_header,
            "cache-control": "no-cache",
            "content-type": "application/json",
            "origin": "https://app.bet-analytix.com",
            "pragma": "no-cache",
            "referer": "https://app.bet-analytix.com/",
            "user-agent": self._settings.user_agent,
        }
        if self._settings.sid:
            headers["sid"] = self._settings.sid
        if authenticated and self._access_token:
            headers["authorization"] = f"Bearer {self._access_token}"
        return headers

    def _build_datetime_update_payload(
        self,
        bet_id: int,
        raw_bet: dict[str, Any],
        event_datetime: datetime,
    ) -> dict[str, Any]:
        """Monta o payload de edicao preservando todos os campos da aposta."""

        bet_type = to_int(raw_bet.get("type"))
        if bet_type != 1:
            raise BetAnalytixWriterError(
                f"Ajuste automatico de data/hora bloqueado: aposta {bet_id} nao e simples (type={bet_type})."
            )

        label = str(raw_bet.get("label") or "").strip()
        if not label:
            raise BetAnalytixWriterError(f"Aposta {bet_id} sem label; ajuste de data/hora bloqueado.")

        event_date, event_time = self._bet_analytix_datetime_fields(event_datetime)
        state = to_int(raw_bet.get("state")) or 0
        commission = _commission_payload(raw_bet.get("commission"))

        return {
            "id": bet_id,
            "bankroll": to_int(raw_bet.get("bankroll")) or self._settings.copytrade_bankroll_internal_id,
            "date": event_date,
            "time": event_time,
            "selections": [
                {
                    "id": bet_id,
                    "label": label,
                    "odds": raw_bet.get("odds"),
                    "sport": to_int(raw_bet.get("sport")),
                    "status": state,
                    "category": raw_bet.get("category") or None,
                    "competition": raw_bet.get("competition") or None,
                    "betType": raw_bet.get("bet_type") or None,
                    "closing": raw_bet.get("closing") or None,
                    "estimatedProbability": raw_bet.get("estimated_probability") or None,
                }
            ],
            "type": 1,
            "systemCombination": [],
            "stakes": {"single": raw_bet.get("stake")},
            "overallLabel": label,
            "bookmaker": to_int(raw_bet.get("bookmaker")) or raw_bet.get("bookmaker"),
            "tipster": to_int(raw_bet.get("tipster")),
            "category": raw_bet.get("category") or None,
            "commission": commission,
            "bonus": raw_bet.get("bonus") or None,
            "live": _truthy(raw_bet.get("live")),
            "freebet": _truthy(raw_bet.get("freebet")),
            "cashout": raw_bet.get("gain") if state == 6 else None,
            "eachway": raw_bet.get("eachway") or None,
            "masked": _truthy(raw_bet.get("masked_bet") or raw_bet.get("masked")),
            "note": raw_bet.get("comment") or None,
            "stake": raw_bet.get("stake"),
        }

    def _build_odd_stake_update_payload(
        self,
        bet_id: int,
        raw_bet: dict[str, Any],
        odd: float,
        stake: float,
    ) -> dict[str, Any]:
        bet_type = to_int(raw_bet.get("type"))
        if bet_type != 1:
            raise BetAnalytixWriterError(
                f"Ajuste automatico de odd/stake bloqueado: aposta {bet_id} nao e simples (type={bet_type})."
            )

        label = str(raw_bet.get("label") or "").strip()
        if not label:
            raise BetAnalytixWriterError(f"Aposta {bet_id} sem label; ajuste de odd/stake bloqueado.")

        event_date, event_time = _bet_timestamp_fields(raw_bet.get("date"))
        state = to_int(raw_bet.get("state")) or 0
        commission = _commission_payload(raw_bet.get("commission"))
        formatted_odd = f"{odd:.3f}"
        formatted_stake = f"{stake:.2f}"

        return {
            "id": bet_id,
            "bankroll": to_int(raw_bet.get("bankroll")) or self._settings.copytrade_bankroll_internal_id,
            "date": event_date,
            "time": event_time,
            "selections": [
                {
                    "id": bet_id,
                    "label": label,
                    "odds": formatted_odd,
                    "sport": to_int(raw_bet.get("sport")),
                    "status": state,
                    "category": raw_bet.get("category") or None,
                    "competition": raw_bet.get("competition") or None,
                    "betType": raw_bet.get("bet_type") or None,
                    "closing": raw_bet.get("closing") or None,
                    "estimatedProbability": raw_bet.get("estimated_probability") or None,
                }
            ],
            "type": 1,
            "systemCombination": [],
            "stakes": {"single": formatted_stake},
            "overallLabel": label,
            "bookmaker": to_int(raw_bet.get("bookmaker")) or raw_bet.get("bookmaker"),
            "tipster": to_int(raw_bet.get("tipster")),
            "category": raw_bet.get("category") or None,
            "commission": commission,
            "bonus": raw_bet.get("bonus") or None,
            "live": _truthy(raw_bet.get("live")),
            "freebet": _truthy(raw_bet.get("freebet")),
            "cashout": raw_bet.get("gain") if state == 6 else None,
            "eachway": raw_bet.get("eachway") or None,
            "masked": _truthy(raw_bet.get("masked_bet") or raw_bet.get("masked")),
            "note": raw_bet.get("comment") or None,
            "stake": formatted_stake,
        }

    def _assert_non_destructive_request(
        self,
        method: str,
        path: str,
        allow_existing_bet_read: bool = False,
        allow_existing_bet_update: bool = False,
    ) -> None:
        """Bloqueia qualquer chamada capaz de apagar ou alterar apostas existentes.

        O CopyTrade so precisa ler referencias, criar uma aposta nova com
        `POST /bet` e, opcionalmente, criar tipsters. Endpoints destrutivos
        ficam proibidos no proprio cliente HTTP, antes de qualquer chamada de
        rede, para que uma reacao removida, um bug ou uma mudanca futura nao
        consiga apagar apostas da bankroll.
        """

        normalized_method = method.upper().strip()
        normalized_path = path.split("?", maxsplit=1)[0].rstrip("/").lower() or "/"

        if _is_single_bet_path(normalized_path):
            if allow_existing_bet_read and normalized_method == "GET":
                return
            if allow_existing_bet_update and normalized_method == "PUT":
                return

        if normalized_method in {"DELETE", "PATCH", "PUT"}:
            raise BetAnalytixWriterError(f"Operacao Bet-Analytix bloqueada por seguranca: {method} {path}")

        if "/delete" in normalized_path:
            raise BetAnalytixWriterError(f"Endpoint destrutivo bloqueado por seguranca: {method} {path}")

        if normalized_path == "/bet" and normalized_method != "POST":
            raise BetAnalytixWriterError(f"Mutacao invalida de aposta bloqueada: {method} {path}")

        if normalized_path.startswith("/bet/"):
            raise BetAnalytixWriterError(f"Endpoint de alteracao de aposta bloqueado: {method} {path}")

    def _sleep_before_retry(
        self,
        attempt: int,
        reason: str | None = None,
        response: requests.Response | None = None,
    ) -> None:
        delay = _retry_after_seconds(response) or self._settings.backoff_initial_seconds * (2**attempt)
        if reason:
            logger.warning("Falha temporária no Bet-Analytix writer: %s. Nova tentativa em %.1fs.", reason, delay)
        time.sleep(delay)


def _retry_after_seconds(response: requests.Response | None) -> float | None:
    if response is None:
        return None
    raw_value = response.headers.get("Retry-After")
    if raw_value is None:
        return None
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return None


def _is_single_bet_path(path: str) -> bool:
    return path.startswith("/bet/") and path.removeprefix("/bet/").isdigit()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "sim", "s"}
    return bool(value)


def _commission_payload(value: Any) -> dict[str, Any] | None:
    if value in (None, "", 0, "0", "0.00"):
        return None
    return {
        "amount": value,
        "percentage": None,
        "base": None,
        "applyOnLoss": False,
    }


def _normalize_text(value: str) -> str:
    normalized_value = value.strip().lower()
    if normalized_value in {"?guas profundas", "�guas profundas"}:
        normalized_value = "aguas profundas"
    if normalized_value in {"t?nis", "t�nis"}:
        normalized_value = "tenis"

    normalized = unicodedata.normalize("NFKD", normalized_value)
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return "".join(char for char in without_marks if char.isalnum())


def _bookmaker_id_from_text(value: str) -> int | None:
    cleaned = value.strip()
    if cleaned.lower().startswith("id "):
        cleaned = cleaned[3:].strip()
    if not cleaned.isdigit():
        return None
    return int(cleaned)


def _bookmaker_bet_variants(normalized_name: str) -> set[str]:
    """Retorna o nome e sua forma exata alternativa com/sem sufixo ``bet``."""

    variants = {normalized_name}
    if normalized_name.endswith("bet") and len(normalized_name) > 3:
        without_bet = normalized_name[:-3]
        if len(without_bet) >= 3:
            variants.add(without_bet)
    elif len(normalized_name) >= 3:
        variants.add(f"{normalized_name}bet")
    return variants


def _bet_analytix_timestamp(event_date: str, event_time: str) -> int:
    event_datetime = datetime.strptime(f"{event_date} {event_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return int(event_datetime.timestamp())


def _bet_timestamp_fields(value: Any) -> tuple[str, str]:
    timestamp = to_int(value)
    if timestamp is None:
        raise BetAnalytixWriterError(f"Aposta sem timestamp valido para preservar data/hora: {value!r}")
    event_datetime = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return event_datetime.strftime("%Y-%m-%d"), event_datetime.strftime("%H:%M")


def _is_matching_existing_bet(
    bet: dict[str, Any],
    tip: ParsedTelegramTip,
    bookmaker_id: int,
    sport_id: int,
    tipster_id: int,
    event_timestamp: int,
) -> bool:
    return (
        to_int(bet.get("date")) == event_timestamp
        and str(bet.get("bookmaker") or "") == str(bookmaker_id)
        and to_int(bet.get("tipster")) == tipster_id
        and to_int(bet.get("sport")) == sport_id
        and _same_text(str(bet.get("label") or ""), tip.pick)
        and _same_decimal(bet.get("odds"), tip.odd, tolerance=0.0005)
        and _same_decimal(bet.get("stake"), tip.stake, tolerance=0.005)
    )


def _same_text(left: str, right: str) -> bool:
    return " ".join(left.strip().split()) == " ".join(right.strip().split())


def _same_decimal(value: Any, expected: float, tolerance: float) -> bool:
    try:
        parsed = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return False
    return abs(parsed - expected) <= tolerance


def _build_note(tip: ParsedTelegramTip) -> str:
    if tip.extra_note is not None:
        return tip.extra_note.strip()

    return ""
