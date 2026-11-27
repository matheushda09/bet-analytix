"""Sinais no formato em dolar (US$ com R$ entre parenteses) e variacoes.

Exemplos reais do canal Discord. A bankroll e em reais, entao a stake deve
ser sempre o valor R$ entre parenteses.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from userbot_signal_parser import (
    ODD_CHANGED_MARKER_PATTERN,
    UserbotSignalParseError,
    is_external_signal_message,
    parse_external_signal,
)


# FishBot: sem linha 🔮, ADM logo apos 🆓, linha ✅ depois do ADM, data "14h45".
FISHBOT_USD_SIGNAL = """🏠 99play
🆚 Olympiacos x Milan
⚽️ Futebol
📌 Total de escanteios - Menos de 8,5
🏷 2.15
🚦 Limite da aposta: US$388,03 (R$2.282,60)
🛑 0,27%
💰 US$4,59 (R$27,00)
🆓 Não
👨‍💻 ADM: FishBot
✅
📆 26/11/2026 14h45
🏆 Liga Europa

https://99play.me/sports#/sport/66/category/1133/championship/16809/event/18193907

Odd justa: 2.123

📊 Odd mudou? Clique AQUI e calcule quanto vale.

🦈 PLANILHAR COM SHARK TRACK"""

# FishBot sem ✅/❌, limite "US$??,00", links markdown e bloco de clones.
FISHBOT_UNKNOWN_LIMIT_SIGNAL = """🏠 LowVig
🆚 Gonzaga x Baylor
⚽️ Basquete
📌 Gonzaga (-3) - Handicap de pontos (c/ prorrog.)
🏷 1.943
🚦 Limite da aposta: US$??,00
🛑 0,75%
💰 US$12,75 (R$75,00)
🆓 Não
👨‍💻 ADM: FishBot
📆 27/11/2026 01h30
🏆 NCAA

https://sports.lowvig.ag/sportsbook/basketball/ncaa/game/491191495

Odd justa: 1.889

📊 Odd mudou? [Clique AQUI](https://calc.peixeesperto.com.br/?justa=1.889) e calcule quanto vale.

🧬 [CLONES DA LowVig](https://peixeesperto.com.br/clones/abc)

🦈 [PLANILHAR COM SHARK TRACK](https://t.me/SharkTrackAPP_bot?start=tip_01a218f3)"""

# Julio Bucci: linha 🔮 antes do ADM, 📆 e 🏆 vazios.
CRYSTAL_BALL_EMPTY_DATE_SIGNAL = """🏠 Cloudbet
🆚 PIT Steelers x DEN Broncos
⚽️ Futebol Americano
📌 Jonah Coleman menos de 19.5 - Tentativas de corrida
🏷 1.98
🚦 Limite da aposta: US$??,00
🛑 0,80%
💰 US$13,60 (R$80,00)
🆓 Não
🔮
👨‍💻 ADM: Julio Bucci
📆
🏆

https://cloudbet.com/go/pt-br/pulse?post=b774649b

Estatísticas nos comentários

Odd justa: 1.920

📊 Odd mudou? [Clique AQUI](https://calc.peixeesperto.com.br/?justa=1.920) e calcule quanto vale.

🦈 [PLANILHAR COM SHARK TRACK](https://t.me/SharkTrackAPP_bot?start=tip_01a21916)"""

CRYSTAL_BALL_WITH_DATE_SIGNAL = """🏠 99play
🆚 Braga x Kuopian Palloseura
⚽️ Futebol
📌 Menos de 4.5 - Total de cartões
🏷 1.50
🚦 Limite da aposta: US$8,50 (R$50,00)
🛑 1,89%
💰 US$8,50 (R$50,00)
🆓 Não
🔮
👨‍💻 ADM: Julio Bucci
📆 26/11/2026 17h00
🏆

https://99play.me/sports?shareCode=Z3IQOFBJFUR

Odd justa: 1.445

📊 Odd mudou? [Clique AQUI](https://calc.peixeesperto.com.br/?justa=1.445) e calcule quanto vale.

🦈 [PLANILHAR COM SHARK TRACK](https://t.me/SharkTrackAPP_bot?start=tip_01a2194c)"""

# Sem emojis e sem titulo SOBRECARGA; texto de tip convergente apos o link.
PLAIN_CONVERGENT_SIGNAL = """Stake.com
Dallas Cowboys x Philadelphia Eagles
Futebol americano
Vencedor (incluindo prorrogação) - Philadelphia Eagles
5.74
Limite da aposta: US$20,00 (R$117,65)
0,85%
US$1,53 (R$9,00)
Não
ADM: FishBot
26/11/2026 18h30
NFL

https://stake.com/sports/american-football/usa/nfl/46551948-dallas-cowboys-philadelphia-eagles

Já temos aposta enviada dia 26/11/2026, valendo 0,76% da banca, em seleção convergente. Esta tip aporta só a diferença.

Odd justa: 4.937

📊 Odd mudou? [Clique AQUI](https://calc.peixeesperto.com.br/?justa=4.937) e calcule quanto vale.

🦈 [PLANILHAR COM SHARK TRACK](https://t.me/SharkTrackAPP_bot?start=tip_01a21905)"""

# Sem emojis, linha em branco no lugar do 🔮, edge sem casa decimal.
PLAIN_FINTA_SIGNAL = """Finta
São Paulo x Remo
Futebol
São Paulo (empate anula) - Resultado final
5.00
Limite da aposta: US$1,02 (R$6,00)
10%
US$1,02 (R$6,00)
Não

ADM: victylty
26/11/2026 20h00

https://finta.win/#ap=eyJ2IjoxLCJzbyI6ImZsLXZiemphMnNjcWwzeC1zYWEwYmRkYTUifQ

Odd justa: 1.923

Não tem cadastro? Faça clicando [AQUI](https://partners.finta.win/t/xmrv454x?ir=/sportsbook)... Nos ajuda MUITO e você não perde nada!

📊 Odd mudou? [Clique AQUI](https://calc.peixeesperto.com.br/?justa=1.923) e calcule quanto vale.

🦈 [PLANILHAR COM SHARK TRACK](https://t.me/SharkTrackAPP_bot?start=tip_01a219c7)"""

# SOBRECARGA com stake so em reais e comentario com outra odd/link.
OVERLOAD_BRL_SIGNAL = """SOBRECARGA

Cloudbet
DAL Cowboys x PHI Eagles
Futebol Americano
KaVontae Turpin menos de 4.5 - Total de jardas por corridas
1.82
Limite da aposta: R$999,00
2,48%
R$248,00
Não

ADM: victylty

https://cloudbet.com/go/pt-br/pulse?post=f3d9188a

2,33 @1,81 https://stake.com/sports/american-football/usa/nfl/46551948-dallas-cowboys-philadelphia-eagles

Odd justa: 1.683

📊 Odd mudou? [Clique AQUI](https://calc.peixeesperto.com.br/?justa=1.683) e calcule quanto vale.

🦈 [PLANILHAR COM SHARK TRACK](https://t.me/SharkTrackAPP_bot?start=tip_01a21953)"""

NON_SIGNAL_MESSAGES = (
    """Atualmente temos esse parâmetro no bot.

o retorno potencial minimo é de 0,5% da banca.

Ou seja, entra odd 2.00 a 0,25%
odd 4.00 a 0,125% e por aí vai.""",
    "Senhores a betonline limita bem pouco. Em algumas apostas, a liquidez é menor "
    "(props passa uns 12-20USD às vezes). Use os clones lowvig/sportsbetting cujos links estão no post",
    "🐸 **CONTAGEM ENCERRADA, GIRINO!** 🦈 11 sharks reagiram com 👍 🐌 0 lerdíssimos",
)


class UsdLayoutTests(unittest.TestCase):
    def _parse(self, text: str):
        self.assertTrue(
            is_external_signal_message(text, signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN)
        )
        return parse_external_signal(text, signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN)

    def test_all_layouts_map_fields_using_brl_values(self) -> None:
        cases = (
            (
                FISHBOT_USD_SIGNAL,
                {
                    "source_bookmaker": "99play",
                    "event": "Olympiacos x Milan",
                    "sport": "Futebol",
                    "pick": "Total de escanteios - Menos de 8,5",
                    "odd": 2.15,
                    "limit": "R$2.282,60",
                    "edge": "0,27%",
                    "stake": 27.0,
                    "freebet": "Não",
                    "admin": "FishBot",
                    "fair_odd": "2.123",
                    "event_datetime": datetime(2026, 11, 26, 14, 45),
                },
            ),
            (
                FISHBOT_UNKNOWN_LIMIT_SIGNAL,
                {
                    "source_bookmaker": "LowVig",
                    "sport": "Basquete",
                    "pick": "Gonzaga (-3) - Handicap de pontos (c/ prorrog.)",
                    "odd": 1.943,
                    "limit": "US$??,00",
                    "stake": 75.0,
                    "admin": "FishBot",
                    "event_datetime": datetime(2026, 11, 27, 1, 30),
                },
            ),
            (
                CRYSTAL_BALL_EMPTY_DATE_SIGNAL,
                {
                    "source_bookmaker": "Cloudbet",
                    "event": "PIT Steelers x DEN Broncos",
                    "sport": "Futebol Americano",
                    "odd": 1.98,
                    "stake": 80.0,
                    "admin": "Julio Bucci",
                    "fair_odd": "1.920",
                    "event_datetime": None,
                },
            ),
            (
                CRYSTAL_BALL_WITH_DATE_SIGNAL,
                {
                    "limit": "R$50,00",
                    "edge": "1,89%",
                    "stake": 50.0,
                    "admin": "Julio Bucci",
                    "event_datetime": datetime(2026, 11, 26, 17, 0),
                },
            ),
            (
                PLAIN_CONVERGENT_SIGNAL,
                {
                    "source_bookmaker": "Stake.com",
                    "event": "Dallas Cowboys x Philadelphia Eagles",
                    "sport": "Futebol americano",
                    "pick": "Vencedor (incluindo prorrogação) - Philadelphia Eagles",
                    "odd": 5.74,
                    "limit": "R$117,65",
                    "edge": "0,85%",
                    "stake": 9.0,
                    "freebet": "Não",
                    "admin": "FishBot",
                    "fair_odd": "4.937",
                    "event_datetime": datetime(2026, 11, 26, 18, 30),
                },
            ),
            (
                PLAIN_FINTA_SIGNAL,
                {
                    "source_bookmaker": "Finta",
                    "odd": 5.0,
                    "limit": "R$6,00",
                    "edge": "10%",
                    "stake": 6.0,
                    "admin": "victylty",
                    "event_datetime": datetime(2026, 11, 26, 20, 0),
                },
            ),
            (
                OVERLOAD_BRL_SIGNAL,
                {
                    "source_bookmaker": "Cloudbet",
                    "odd": 1.82,
                    "limit": "R$999,00",
                    "edge": "2,48%",
                    "stake": 248.0,
                    "admin": "victylty",
                    "fair_odd": "1.683",
                    "event_datetime": None,
                },
            ),
        )
        for text, expected in cases:
            with self.subTest(first_line=text.splitlines()[0], pick=text.splitlines()[3]):
                signal = self._parse(text)
                for field, value in expected.items():
                    self.assertEqual(getattr(signal, field), value, field)

    def test_league_and_signal_exchange_rate(self) -> None:
        cases = (
            (FISHBOT_USD_SIGNAL, "Liga Europa", 5.8825),
            (FISHBOT_UNKNOWN_LIMIT_SIGNAL, "NCAA", 5.8824),
            (CRYSTAL_BALL_EMPTY_DATE_SIGNAL, None, 5.8824),
            (CRYSTAL_BALL_WITH_DATE_SIGNAL, None, 5.8824),
            (PLAIN_CONVERGENT_SIGNAL, "NFL", 5.8825),
            (PLAIN_FINTA_SIGNAL, None, 5.8824),
            (OVERLOAD_BRL_SIGNAL, None, None),
        )
        for text, league, rate in cases:
            with self.subTest(first_line=text.splitlines()[0], pick=text.splitlines()[3]):
                signal = self._parse(text)
                self.assertEqual(signal.league, league)
                self.assertEqual(signal.usd_brl_rate, rate)

    def test_large_brl_stake_with_thousands_separator(self) -> None:
        text = FISHBOT_USD_SIGNAL.replace("US$4,59 (R$27,00)", "US$212,49 (R$1.250,00)")

        self.assertEqual(self._parse(text).stake, 1250.0)

    def test_usd_only_stake_is_rejected(self) -> None:
        text = FISHBOT_USD_SIGNAL.replace("US$4,59 (R$27,00)", "US$4,59")

        with self.assertRaises(UserbotSignalParseError):
            parse_external_signal(text, signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN)

    def test_emoji_layout_rejects_unknown_line_before_admin(self) -> None:
        text = FISHBOT_USD_SIGNAL.replace("🆓 Não\n", "🆓 Não\ncomentario\n", 1)

        self.assertFalse(
            is_external_signal_message(text, signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN)
        )

    def test_convergent_text_date_is_not_used_as_event_date(self) -> None:
        text = PLAIN_CONVERGENT_SIGNAL.replace("26/11/2026 18h30\n", "", 1)

        self.assertIsNone(self._parse(text).event_datetime)

    def test_chat_messages_are_not_signals(self) -> None:
        for text in NON_SIGNAL_MESSAGES:
            with self.subTest(text=text[:30]):
                self.assertFalse(
                    is_external_signal_message(text, signal_marker_pattern=ODD_CHANGED_MARKER_PATTERN)
                )


if __name__ == "__main__":
    unittest.main()
