from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from database import BetStateStore
from main import BetMonitor, ReferenceCache
from models import Tipster, build_competition_map, parse_bet


def _raw(bet_id: int, competition=None) -> dict:
    return {
        "id": bet_id,
        "tipster": 262847,
        "sport": 1,
        "date": 1795740660,
        "label": f"Pick {bet_id}",
        "odds": "2.000",
        "stake": "10.00",
        "bookmaker": "1135",
        "competition": competition,
    }


class CompetitionNameTests(unittest.TestCase):
    def test_competition_id_becomes_label(self) -> None:
        names = build_competition_map({"competitions": [{"id": 262193, "label": "Liga Europa"}]})
        bet = parse_bet(_raw(1, 262193), {}, {1: "Futebol"}, {}, "America/Sao_Paulo", "X", names)
        self.assertEqual(bet.league, "Liga Europa")

    def test_unknown_id_is_hidden_and_text_is_kept(self) -> None:
        self.assertIsNone(parse_bet(_raw(1, 999), {}, {}, {}, "America/Sao_Paulo", "X", {}).league)
        self.assertEqual(parse_bet(_raw(1, "NBA"), {}, {}, {}, "America/Sao_Paulo", "X", {}).league, "NBA")


class OldBetSuppressionTests(unittest.TestCase):
    def test_old_bets_sliding_into_page_are_not_notified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = BetStateStore(Path(temp_dir) / "bets.sqlite3")
            store.initialize()
            settings = SimpleNamespace(
                target_tipster_names=("PeixeEsperto",),
                sport_names={1: "Futebol"},
                timezone="America/Sao_Paulo",
                target_tipster_name="PeixeEsperto",
                notify_existing_on_first_run=False,
                telegram_admin_user_id=None,
                telegram_chat_id="-100",
                reference_refresh_seconds=3600,
            )
            client = mock.Mock()
            notifier = mock.Mock()
            notifier.send_bet.return_value = SimpleNamespace(chat_id="-100", message_id=1, text="x")
            monitor = BetMonitor(settings, client, store, notifier)
            monitor._reference_cache = ReferenceCache(
                tipsters_by_id={262847: Tipster(262847, "PeixeEsperto")},
                bookmaker_names={},
                target_tipster_ids=(262847,),
                refreshed_at_monotonic=time.monotonic(),
                competition_names={},
            )
            monitor._delivery_chat_id = lambda: "-100"

            client.fetch_all_bets.return_value = [_raw(123586600), _raw(123586610)]
            monitor.poll_once()  # primeiro boot: marca como vistas
            notifier.send_bet.assert_not_called()

            client.fetch_all_bets.return_value = [_raw(110211629), _raw(123586610), _raw(123586657)]
            monitor.poll_once()

            sent_ids = [call.args[0].id for call in notifier.send_bet.call_args_list]
            self.assertEqual(sent_ids, [123586657])
            self.assertTrue(store.has_notified(110211629))


if __name__ == "__main__":
    unittest.main()
