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
