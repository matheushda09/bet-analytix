from __future__ import annotations

import unittest

from userbot_signal_parser import (
    ODD_CHANGED_MARKER_PATTERN,
    UserbotSignalParseError,
    is_external_signal_message,
    parse_external_signal,
)


MANAGEMENT_SIGNAL = """🏠 Donald Bet
🆚 O'Higgins x Boca Juniors
⚽️ Futebol
📌 Boca Juniors - Resultado final
🏷 2.22
🚦 Limite da aposta: R$25,00
🛑 0,80%
💰 R$25,00
🆓 Não
🔮
👨💻 ADM: victylty

https://donald.bet.br/esportes?bscode=ZJC3GC

Odd justa: 2.137

Não tem cadastro? Faça clicando AQUI... Nos ajuda MUITO e você não perde nada!

📊 Odd mudou? Clique AQUI e calcule quanto vale.

🦈 PLANILHAR COM SHARK TRACK"""


OVERLOAD_SIGNAL = """SOBRECARGA

Donald Bet
Corinthians x Athletico Paranaense
Futebol
Corinthians - Resultado do 1º tempo
3.02
Limite da aposta: R$25,00
0,38%
R$25,00
Não

ADM: victylty

https://donald.bet.br/esportes?bscode=RIGP2O

Odd justa: 2.930

Não tem cadastro? Faça clicando AQUI... Nos ajuda MUITO e você não perde nada!

📊 Odd mudou? Clique AQUI e calcule quanto vale.

🦈 PLANILHAR COM SHARK TRACK"""

OVERLOAD_SIGNAL_WITH_ATTRIBUTION = """SOBRECARGA

Pitaco
Internacional x Flamengo
Futebol
Pedro 1+ - Finalizações no gol / Flamengo mais de 2.5 - Defesas de goleiro / Mais de 1.5 - Total de
3.47
Limite da aposta: R$999,00
0,86%
R$86,00
Não
@galomatouoduarte
ADM: victylty

https://pitaco.bet.br/betting/events/13331880643

n tenho conta, ve limite pfv

Odd justa: 3.198

📊 Odd mudou? [Clique AQUI](https://calc.peixeesperto.com.br/?justa=3.198) e calcule quanto vale.

🦈 [PLANILHAR COM SHARK TRACK](https://t.me/SharkTrackAPP_bot?start=tip_019fafa0-e855-727a-b792-90d35767c989)"""

OVERLOAD_ICE_BET_SIGNAL = """SOBRECARGA

ICE BET
Tigre x Nacional Montevideo
Futebol
Tigre - Resultado final / Mais de 2.5 - Total de gols / Sim - Ambas marcam
6.20
Limite da aposta: R$50,00
0,39%
R$39,00
Não

ADM: paterra

https://ice.bet.br/sports/1/821352502514806784/867599274622586880

Odd justa: 5.735

📊 Odd mudou? [Clique AQUI](https://calc.peixeesperto.com.br/?justa=5.735) e calcule quanto vale.

🦈 [PLANILHAR COM SHARK TRACK](https://t.me/SharkTrackAPP_bot?start=tip_019faab8-696e-7063-86bd-8a75b1d3eb34)"""

OVERLOAD_LOTTU_SIGNAL = """SOBRECARGA

Lottu
Fluminense x Bahia
Futebol
Fluminense - Resultado final / Hulk - Para marcar a qualquer momento
4.60
Limite da aposta: R$100,00
0,48%
R$48,00
Não

ADM: victylty

https://www.lottu.bet.br/bet/share/6501701

Essa eh sobrecarga no Hulk e no Flu, cuidado

Odd justa: 4.303

📊 Odd mudou? [Clique AQUI](https://calc.peixeesperto.com.br/?justa=4.303) e calcule quanto vale.

🦈 [PLANILHAR COM SHARK TRACK](https://t.me/SharkTrackAPP_bot?start=tip_019faa63-6669-73da-85e3-009d164b26d1)"""


class SignalLayoutTests(unittest.TestCase):
    def _parse(self, text: str):
        return parse_external_signal(
            text,
            signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN,
        )

    def test_management_layout_keeps_existing_behavior(self) -> None:
        signal = self._parse(MANAGEMENT_SIGNAL)

        self.assertEqual(signal.source_bookmaker, "Donald Bet")
        self.assertEqual(signal.bookmaker, "Donald Bet")
        self.assertEqual(signal.event, "O'Higgins x Boca Juniors")
        self.assertEqual(signal.sport, "Futebol")
        self.assertEqual(signal.pick, "Boca Juniors - Resultado final")
        self.assertEqual(signal.odd, 2.22)
        self.assertEqual(signal.limit, "R$25,00")
        self.assertEqual(signal.edge, "0,80%")
        self.assertEqual(signal.stake, 25.0)
        self.assertEqual(signal.freebet, "Não")
        self.assertEqual(signal.admin, "victylty")
        self.assertEqual(signal.fair_odd, "2.137")
        self.assertEqual(signal.note("channel", 1), "Odd justa: 2.137")

    def test_overload_layout_maps_every_field_to_the_same_model(self) -> None:
        signal = self._parse(OVERLOAD_SIGNAL)

        self.assertEqual(signal.source_bookmaker, "Donald Bet")
        self.assertEqual(signal.bookmaker, "Donald Bet")
        self.assertEqual(signal.event, "Corinthians x Athletico Paranaense")
        self.assertEqual(signal.sport, "Futebol")
        self.assertEqual(signal.pick, "Corinthians - Resultado do 1º tempo")
        self.assertEqual(signal.odd, 3.02)
        self.assertEqual(signal.limit, "R$25,00")
        self.assertEqual(signal.edge, "0,38%")
        self.assertEqual(signal.stake, 25.0)
        self.assertEqual(signal.freebet, "Não")
        self.assertEqual(signal.admin, "victylty")
        self.assertEqual(signal.fair_odd, "2.930")
        self.assertEqual(signal.note("channel", 1), "Odd justa: 2.930")

    def test_overload_variants_map_all_fields(self) -> None:
        cases = (
            (
                OVERLOAD_SIGNAL_WITH_ATTRIBUTION,
                {
                    "bookmaker": "Pitaco",
                    "event": "Internacional x Flamengo",
                    "pick": (
                        "Pedro 1+ - Finalizações no gol / Flamengo mais de 2.5 - "
                        "Defesas de goleiro / Mais de 1.5 - Total de"
                    ),
                    "odd": 3.47,
                    "limit": "R$999,00",
                    "edge": "0,86%",
                    "stake": 86.0,
                    "admin": "victylty",
                    "fair_odd": "3.198",
                },
            ),
            (
                OVERLOAD_SIGNAL,
                {
                    "bookmaker": "Donald Bet",
                    "event": "Corinthians x Athletico Paranaense",
                    "pick": "Corinthians - Resultado do 1º tempo",
                    "odd": 3.02,
                    "limit": "R$25,00",
                    "edge": "0,38%",
                    "stake": 25.0,
                    "admin": "victylty",
                    "fair_odd": "2.930",
                },
            ),
            (
                OVERLOAD_ICE_BET_SIGNAL,
                {
                    "bookmaker": "ICE BET",
                    "event": "Tigre x Nacional Montevideo",
                    "pick": (
                        "Tigre - Resultado final / Mais de 2.5 - Total de gols / "
                        "Sim - Ambas marcam"
                    ),
                    "odd": 6.20,
                    "limit": "R$50,00",
                    "edge": "0,39%",
                    "stake": 39.0,
                    "admin": "paterra",
                    "fair_odd": "5.735",
                },
            ),
            (
                OVERLOAD_LOTTU_SIGNAL,
                {
                    "bookmaker": "Lottu",
                    "event": "Fluminense x Bahia",
                    "pick": "Fluminense - Resultado final / Hulk - Para marcar a qualquer momento",
                    "odd": 4.60,
                    "limit": "R$100,00",
                    "edge": "0,48%",
                    "stake": 48.0,
                    "admin": "victylty",
                    "fair_odd": "4.303",
                },
            ),
        )

        for text, expected in cases:
            with self.subTest(bookmaker=expected["bookmaker"]):
                self.assertTrue(
                    is_external_signal_message(
                        text,
                        signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN,
                    )
                )
                signal = self._parse(text)
                self.assertEqual(signal.sport, "Futebol")
                self.assertEqual(signal.freebet, "Não")
                for field, value in expected.items():
                    self.assertEqual(getattr(signal, field), value)

    def test_overload_accepts_sim_as_freebet(self) -> None:
        signal = self._parse(OVERLOAD_SIGNAL.replace("\nNão\n", "\nSim\n", 1))

        self.assertEqual(signal.freebet, "Sim")

    def test_overload_accepts_discord_user_mention_before_admin(self) -> None:
        text = OVERLOAD_SIGNAL.replace("\nADM:", "\n<@!123456789>\nADM:", 1)

        signal = self._parse(text)

        self.assertEqual(signal.admin, "victylty")

    def test_overload_rejects_arbitrary_text_before_admin(self) -> None:
        text = OVERLOAD_SIGNAL.replace("\nADM:", "\ncomentario solto\nADM:", 1)

        self.assertFalse(
            is_external_signal_message(
                text,
                signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN,
            )
        )
        with self.assertRaises(UserbotSignalParseError):
            self._parse(text)

    def test_overload_title_and_labels_are_case_insensitive(self) -> None:
        text = (
            OVERLOAD_SIGNAL
            .replace("SOBRECARGA", "sobrecarga")
            .replace("Limite da aposta:", "LIMITE DA APOSTA:")
            .replace("ADM:", "adm:")
        )

        signal = self._parse(text)

        self.assertEqual(signal.source_bookmaker, "Donald Bet")
        self.assertEqual(signal.admin, "victylty")

    def test_overload_accepts_discord_bold_markdown_title(self) -> None:
        text = (
            OVERLOAD_SIGNAL
            .replace("SOBRECARGA", "**SOBRECARGA**", 1)
            .replace(
                "📊 Odd mudou? Clique AQUI e calcule quanto vale.",
                "📊 Odd mudou? [Clique AQUI](https://calc.peixeesperto.com.br/?justa=2.930) "
                "e calcule quanto vale.",
            )
        )

        self.assertTrue(
            is_external_signal_message(
                text,
                signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN,
            )
        )
        signal = self._parse(text)
        self.assertEqual(signal.source_bookmaker, "Donald Bet")
        self.assertEqual(signal.fair_odd, "2.930")

    def test_both_layouts_are_recognized_before_reaction_processing(self) -> None:
        for text in (MANAGEMENT_SIGNAL, OVERLOAD_SIGNAL):
            with self.subTest(first_line=text.splitlines()[0]):
                self.assertTrue(
                    is_external_signal_message(
                        text,
                        signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN,
                    )
                )

    def test_emoji_less_layout_requires_overload_title(self) -> None:
        text = OVERLOAD_SIGNAL.replace("SOBRECARGA\n\n", "", 1)

        self.assertFalse(
            is_external_signal_message(
                text,
                signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN,
            )
        )
        with self.assertRaises(UserbotSignalParseError):
            self._parse(text)

    def test_overload_rejects_shifted_or_missing_fields(self) -> None:
        text = OVERLOAD_SIGNAL.replace("0,38%\n", "", 1)

        self.assertFalse(
            is_external_signal_message(
                text,
                signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN,
            )
        )
        with self.assertRaises(UserbotSignalParseError):
            self._parse(text)


if __name__ == "__main__":
    unittest.main()
