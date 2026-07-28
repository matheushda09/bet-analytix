"""Handlers de comandos Discord para o modulo de bankroll."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import discord

from bankroll_module import BankrollController


logger = logging.getLogger(__name__)


def _parse_money(value: str) -> Decimal:
    """Converte texto do usuario em Decimal (aceita R$ 1.234,56 ou 1234.56)."""

    cleaned = value.strip().upper()
    cleaned = cleaned.replace("R$", "").replace("$", "").strip()
    cleaned = cleaned.replace(".", "").replace(",", ".")
    return Decimal(cleaned)


def _looks_like_money(value: str) -> bool:
    """Verifica se um texto parece ser um valor monetario."""

    try:
        _parse_money(value)
        return True
    except InvalidOperation:
        return False


def _split_command_args(content: str, prefix: str) -> tuple[str, list[str]]:
    """Separa o comando dos argumentos."""

    body = content[len(prefix):].strip()
    parts = body.split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    args_text = parts[1].strip() if len(parts) > 1 else ""
    args = args_text.split() if args_text else []
    return command, args


def _format_summary_message(controller: BankrollController, summary: Any, bookmaker_balances: list[Any] | None = None) -> str:
    lines = [
        "📊 **Resumo da Bankroll**",
        "",
        f"💰 Capital atual: **{controller.format_money(summary.current_capital)}**",
        f"📌 Capital inicial: {controller.format_money(summary.start_capital)}",
        f"{'📈' if summary.is_profitable else '📉'} Lucro / Prejuízo: **{controller.format_money(summary.profit)}**",
        f"📊 ROI: {summary.roi:.2f}%",
        f"📈 Progressão: {summary.progression:.2f}%",
        f"⏳ Apostas pendentes: {summary.bets_pending} ({controller.format_money(summary.stake_pending)})",
        "",
        f"_Total: {summary.total_bets} apostas | ✅ {summary.bets_won} | ❌ {summary.bets_lost}_",
    ]

    if bookmaker_balances:
        total_available = sum((balance.available for balance in bookmaker_balances), Decimal("0"))
        total_in_play = sum((balance.in_play for balance in bookmaker_balances), Decimal("0"))
        lines.extend([
            "",
            f"💵 **Saldo real em casas: {controller.format_money(total_available)}**",
            f"⏳ **Em jogo nas casas: {controller.format_money(total_in_play)}**",
            "",
            "**Por casa:**",
        ])
        for balance in bookmaker_balances[:10]:
            lines.append(f"• **{balance.bookmaker_name}**: {controller.format_money(balance.available)} disponível")
        if len(bookmaker_balances) > 10:
            lines.append(f"• ... e mais {len(bookmaker_balances) - 10} casa(s)")

    return "\n".join(lines)


def _format_exposure_message(controller: BankrollController, exposures: list[Any], total_pending: Decimal) -> str:
    lines = [
        "💸 **Dinheiro na rua por casa**",
        "",
        f"Total em apostas pendentes: **{controller.format_money(total_pending)}**",
        "",
    ]
    if not exposures:
        lines.append("Nenhuma aposta pendente encontrada.")
        return "\n".join(lines)

    for exposure in exposures:
        lines.append(
            f"• **{exposure.bookmaker_name}**: {controller.format_money(exposure.pending_stake)} "
            f"({exposure.bet_count} aposta{'s' if exposure.bet_count != 1 else ''})"
        )
    return "\n".join(lines)


def _format_withdrawal_balance_message(controller: BankrollController, balances: list[Any], total: Decimal) -> str:
    lines = [
        "🏧 **Saldo disponível para saque por casa**",
        "",
    ]
    if not balances:
        lines.append("Nenhum saldo disponível para saque.")
        lines.append("")
        lines.append("Registre depósitos com `!b deposito <valor> <casa>` para começar.")
        return "\n".join(lines)

    lines.append(f"Total disponível: **{controller.format_money(total)}**")
    lines.append("")
    for balance in balances:
        in_play_text = f" (em jogo: {controller.format_money(balance.in_play)})" if balance.in_play > 0 else ""
        lines.append(f"• **{balance.bookmaker_name}**: {controller.format_money(balance.available)}{in_play_text}")
    return "\n".join(lines)


def _format_bookmaker_bets_message(
    controller: BankrollController,
    bookmaker_name: str,
    bets: list[Any],
    start: int = 0,
    limit: int = 10,
    status_filter: str | None = None,
) -> str:
    if not bets:
        status_label = f" ({status_filter})" if status_filter else ""
        return f"📭 **{bookmaker_name}{status_label}**\n\nNenhuma aposta encontrada."

    total_stake = sum((bet.stake for bet in bets), Decimal("0"))
    total_profit = sum((bet.profit for bet in bets), Decimal("0"))
    greens = [b for b in bets if b.is_green]
    reds = [b for b in bets if b.is_red]
    pending = [b for b in bets if b.is_pending]

    status_label = f" — {status_filter.upper()}" if status_filter else ""
    lines = [
        f"📋 **Apostas — {bookmaker_name}{status_label}**",
        "",
        f"📊 **Resumo geral:** {len(bets)} apostas | ✅ {len(greens)} greens | ❌ {len(reds)} reds | ⏳ {len(pending)} pendentes",
    ]

    if status_filter == "pendentes":
        lines.append(f"⏳ **Total em jogo:** **{controller.format_money(total_stake)}**")
    elif status_filter == "greens":
        lines.append(f"📈 **Lucro dos greens:** **{controller.format_money(total_profit)}**")
    elif status_filter == "reds":
        lines.append(f"📉 **Prejuízo dos reds:** **{controller.format_money(total_profit)}**")
    else:
        lines.append(f"💰 Total apostado: **{controller.format_money(total_stake)}**")
        lines.append(f"{'📈' if total_profit >= 0 else '📉'} Lucro/Prejuízo: **{controller.format_money(total_profit)}**")

    lines.extend(["", "**Últimas apostas:**", ""])

    end = min(start + limit, len(bets))
    for bet in bets[start:end]:
        icon = "✅" if bet.is_green else "❌" if bet.is_red else "⏳"
        date_str = bet.event_datetime.strftime("%d/%m %H:%M")
        result = ""
        if bet.is_green:
            result = f" | +{controller.format_money(bet.profit)}"
        elif bet.is_red:
            result = f" | {controller.format_money(bet.profit)}"

        label = bet.label
        if len(label) > 45:
            label = label[:42] + "..."

        lines.append(
            f"{icon} `{date_str}` | **{controller.format_money(bet.stake)}** @ {bet.odds:.2f}{result}\n"
            f"└ {label}"
        )

    if end < len(bets):
        remaining = len(bets) - end
        lines.append(f"\n_E mais {remaining} apostas. Envie `{controller._settings.command_prefix} apostas {bookmaker_name} --pagina 2` para ver._")

    return "\n".join(lines)


def _format_report_message(controller: BankrollController, report: Any) -> str:
    summary = report.summary
    lines = [
        "📈 **Relatório diário — Bankroll**",
        f"_Gerado em {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')}_",
        "",
        f"💰 Capital atual: **{controller.format_money(summary.current_capital)}**",
        f"{'📈' if summary.is_profitable else '📉'} Lucro / Prejuízo: **{controller.format_money(summary.profit)}**",
        f"📊 ROI: {summary.roi:.2f}%",
        f"⏳ Total na rua: **{controller.format_money(report.total_pending)}**",
        "",
    ]

    if report.exposures:
        lines.append("**Na rua por casa:**")
        for exposure in report.exposures[:10]:
            lines.append(f"• {exposure.bookmaker_name}: {controller.format_money(exposure.pending_stake)}")
        if len(report.exposures) > 10:
            lines.append(f"• ... e mais {len(report.exposures) - 10} casa(s)")
        lines.append("")

    if report.bookmaker_balances:
        lines.append("**Saldo real por casa:**")
        for balance in report.bookmaker_balances[:10]:
            in_play_text = f" (em jogo: {controller.format_money(balance.in_play)})" if balance.in_play > 0 else ""
            lines.append(f"• {balance.bookmaker_name}: {controller.format_money(balance.available)}{in_play_text}")
        if len(report.bookmaker_balances) > 10:
            lines.append(f"• ... e mais {len(report.bookmaker_balances) - 10} casa(s)")
        lines.append(f"\n💵 **Total em casas: {controller.format_money(report.total_bookmaker_balance)}**")
        lines.append("")

    mov_lines: list[str] = []
    if report.today_deposits > 0:
        mov_lines.append(f"⬆️ Depósitos: {controller.format_money(report.today_deposits)}")
    if report.today_withdrawals > 0:
        mov_lines.append(f"⬇️ Saques: {controller.format_money(report.today_withdrawals)}")
    if mov_lines:
        lines.append("**Movimentações do dia:**")
        lines.extend(mov_lines)
    else:
        lines.append("**Movimentações do dia:** nenhuma")

    posicao_liquida = summary.current_capital - report.total_pending
    lines.extend([
        "",
        f"💵 **Posição líquida (caixa): {controller.format_money(posicao_liquida)}**",
    ])
    return "\n".join(lines)


def _format_transactions_message(controller: BankrollController, transactions: list[Any], title: str) -> str:
    lines = [f"{title}", ""]
    if not transactions:
        lines.append("Nenhuma movimentação registrada no período.")
        return "\n".join(lines)

    for transaction in transactions[:25]:
        icon = "⬆️" if transaction.type == "deposit" else "⬇️" if transaction.type == "withdrawal" else "📝"
        when = datetime.fromtimestamp(transaction.created_at_ts, tz=timezone.utc).strftime("%d/%m %H:%M")
        desc = f" — {transaction.description}" if transaction.description else ""
        lines.append(f"{icon} `{when}` {controller.format_money(transaction.amount)}{desc}")

    if len(transactions) > 25:
        lines.append(f"\n_Mostrando 25 de {len(transactions)} movimentações._")
    return "\n".join(lines)


def _format_help_message(prefix: str) -> str:
    lines = [
        "🤖 **Comandos de bankroll**",
        f"_Use `{prefix} <comando>`. Apenas o admin configurado pode usar._",
        "",
        f"`{prefix} saldo` — Resumo financeiro da bankroll.",
        f"`{prefix} rua` / `{prefix} casas` — Dinheiro em apostas pendentes por bookmaker.",
        f"`{prefix} apostas <casa>` — Lista as apostas de uma casa específica.",
        f"`{prefix} apostas <casa> pendentes` — Só apostas pendentes.",
        f"`{prefix} apostas <casa> greens` — Só greens.",
        f"`{prefix} apostas <casa> reds` — Só reds.",
        f"`{prefix} apostas <casa> --pagina=2` — Paginação (8 apostas por página).",
        f"`{prefix} disponivel` — Saldo disponível para saque por casa (instantâneo).",
        f"`{prefix} sync` — Força sincronização com a API do Bet-Analytix.",
        f"`{prefix} deposito <valor> <casa> [descrição]` — Registra um depósito em uma casa.",
        f"`{prefix} saque <valor> [casa] [descrição]` — Registra um saque e abate do saldo da casa.",
        f"`{prefix} saque <casa> <valor> [descrição]` — Também aceita casa antes do valor.",
        f"`{prefix} historico [dias]` — Lista movimentações recentes (padrão 7 dias).",
        f"`{prefix} relatorio` — Força sincronização e envia o relatório consolidado.",
        f"`{prefix} resetgreens` — Redefine o baseline para contar apenas greens marcados daqui pra frente.",
        f"`{prefix} ajuda` — Mostra esta mensagem.",
    ]
    return "\n".join(lines)


async def dispatch_bankroll_command(
    message: discord.Message,
    controller: BankrollController,
    prefix: str,
) -> None:
    """Roteia comandos de bankroll e responde no Discord."""

    command, args = _split_command_args(message.content, prefix)
    logger.info("Processando comando de bankroll: %s (args=%s)", command, args)

    try:
        if command in {"saldo", "balance"}:
            summary = controller.fetch_summary()
            balances = controller.get_bookmaker_balances()
            await message.channel.send(_format_summary_message(controller, summary, balances))

        elif command in {"rua", "casas", "exposure", "pendentes"}:
            exposures = controller.fetch_pending_by_bookmaker()
            total_pending = sum((exposure.pending_stake for exposure in exposures), Decimal("0"))
            await message.channel.send(_format_exposure_message(controller, exposures, total_pending))

        elif command in {"apostas", "bets", "casa"}:
            if not args:
                await message.channel.send(
                    "⚠️ Informe a casa. Exemplos:\n"
                    "`!b apostas Betano`\n"
                    "`!b apostas Betano pendentes`\n"
                    "`!b apostas Betano greens --pagina=2`"
                )
                return

            # Suporte a paginacao: !b apostas Betano --pagina=2
            page = 1
            page_args = [a for a in args if a.startswith("--pagina=")]
            if page_args:
                try:
                    page = max(1, int(page_args[0].split("=", 1)[1]))
                except ValueError:
                    pass
                args = [a for a in args if not a.startswith("--pagina=")]

            # Detecta filtro de status
            status_filter: str | None = None
            status_keywords = {"pendentes", "pendente", "pending", "abertas"}
            green_keywords = {"greens", "green", "ganhas"}
            red_keywords = {"reds", "red", "perdidas"}
            all_status_keywords = status_keywords | green_keywords | red_keywords

            for i, arg in enumerate(args):
                lowered = arg.lower()
                if lowered in status_keywords:
                    status_filter = "pendentes"
                    args = [a for j, a in enumerate(args) if j != i]
                    break
                elif lowered in green_keywords:
                    status_filter = "greens"
                    args = [a for j, a in enumerate(args) if j != i]
                    break
                elif lowered in red_keywords:
                    status_filter = "reds"
                    args = [a for j, a in enumerate(args) if j != i]
                    break

            bookmaker_input = " ".join(args)
            if not bookmaker_input:
                await message.channel.send("⚠️ Informe a casa. Exemplo: `!b apostas Betano pendentes`")
                return

            try:
                bets = await asyncio.to_thread(controller.get_bets_by_bookmaker, bookmaker_input)
            except ValueError as exc:
                await message.channel.send(f"⚠️ {exc}")
                return

            # Aplica filtro
            if status_filter == "pendentes":
                bets = [b for b in bets if b.is_pending]
            elif status_filter == "greens":
                bets = [b for b in bets if b.is_green]
            elif status_filter == "reds":
                bets = [b for b in bets if b.is_red]

            resolved_id = controller.resolve_bookmaker_id(bookmaker_input)
            bookmaker_name = controller._writer.get_bookmaker_name(resolved_id) or bookmaker_input

            per_page = 8
            start = (page - 1) * per_page
            if start >= len(bets):
                await message.channel.send("⚠️ Página não encontrada.")
                return

            text = _format_bookmaker_bets_message(
                controller,
                bookmaker_name.title(),
                bets,
                start=start,
                limit=per_page,
                status_filter=status_filter,
            )
            # Garante que nao ultrapasse 1900 caracteres
            if len(text) > 1900:
                text = text[:1897] + "..."
            await message.channel.send(text)

        elif command in {"disponivel", "sacar", "withdraw"}:
            balances = controller.get_bookmaker_balances()
            total = sum((balance.available for balance in balances), Decimal("0"))
            await message.channel.send(_format_withdrawal_balance_message(controller, balances, total))

        elif command in {"sync", "sincronizar"}:
            await message.channel.send("🔄 Sincronizando com o Bet-Analytix, aguarde...")
            green_count, green_total, red_count, red_total = await asyncio.to_thread(controller.sync_green_bets)
            balances = controller.get_bookmaker_balances()
            total_available = sum((balance.available for balance in balances), Decimal("0"))
            total_in_play = sum((balance.in_play for balance in balances), Decimal("0"))
            lines = [
                "✅ Sincronizado!",
                f"• {green_count} green(s) novo(s): R$ {green_total:,.2f}",
                f"• {red_count} red(s) novo(s): R$ {red_total:,.2f}",
                f"• Saldo real em casas: R$ {total_available:,.2f}",
            ]
            if total_in_play > 0:
                lines.append(f"• Em jogo: R$ {total_in_play:,.2f}")
            await message.channel.send("\n".join(lines))

        elif command in {"deposito", "deposit", "dep"}:
            await _handle_deposit(message, controller, args)

        elif command in {"saque", "withdrawal"}:
            await _handle_withdrawal(message, controller, args)

        elif command in {"historico", "history", "extrato"}:
            await _handle_history(message, controller, args)

        elif command in {"relatorio", "report", "daily"}:
            await message.channel.send("🔄 Atualizando relatório, aguarde...")
            await asyncio.to_thread(controller.sync_green_bets)
            report = controller.build_report()
            await message.channel.send(_format_report_message(controller, report))

        elif command in {"resetgreens", "resetgreen", "resetar"}:
            new_baseline = controller.reset_green_baseline()
            await message.channel.send(
                f"🔄 **Rastreamento resetado.**\n\n"
                f"A partir de agora, so serao contabilizados greens/reds com ID maior que **{new_baseline}**.\n"
                f"O historico de apostas ja contabilizadas foi limpo.\n"
                f"Marque o resultado no Bet-Analytix e depois use `{prefix} relatorio` para forcar a sincronizacao."
            )

        elif command in {"ajuda", "help", "?"}:
            await message.channel.send(_format_help_message(prefix))

        else:
            await message.channel.send(
                f"❓ Comando `{command}` nao reconhecido. Use `{prefix} ajuda`."
            )

    except InvalidOperation as exc:
        logger.warning("Valor monetario invalido recebido: %s", exc)
        await message.channel.send("⚠️ Valor invalido. Use numeros como `1000` ou `1.234,56`.")
    except Exception as exc:
        logger.exception("Falha ao executar comando de bankroll: %s", command)
        await message.channel.send(f"❌ Erro ao processar comando: {exc}")


async def _handle_deposit(
    message: discord.Message,
    controller: BankrollController,
    args: list[str],
) -> None:
    if len(args) < 2:
        await message.channel.send(
            "⚠️ Informe o valor e a casa. Exemplo: `!b deposito 500 Betano Pix inicial`"
        )
        return

    # Primeiro argumento deve ser valor, segundo a casa
    if not _looks_like_money(args[0]):
        await message.channel.send("⚠️ O primeiro argumento deve ser o valor. Exemplo: `!b deposito 500 Betano`")
        return

    amount = _parse_money(args[0])
    if amount <= 0:
        await message.channel.send("⚠️ O valor deve ser maior que zero.")
        return

    bookmaker_input = args[1]
    bookmaker_id = controller.resolve_bookmaker_id(bookmaker_input)
    if bookmaker_id is None:
        await message.channel.send(f"⚠️ Casa não encontrada: {bookmaker_input}")
        return

    description = " ".join(args[2:]) if len(args) > 2 else None
    bookmaker_name = controller._writer.get_bookmaker_name(bookmaker_id) or bookmaker_input
    transaction_id = controller.record_transaction(
        transaction_type="deposit",
        amount=amount,
        created_by_user_id=message.author.id,
        description=description,
        bookmaker_id=bookmaker_id,
        bookmaker_name=bookmaker_name,
    )
    text = (
        f"⬆️ **Depósito registrado**\n\n"
        f"**{controller.format_money(amount)}** na **{bookmaker_name}**"
    )
    if description:
        text += f"\nDescrição: {description}"
    text += f"\n_ID #{transaction_id}_"
    await message.channel.send(text)


async def _handle_withdrawal(
    message: discord.Message,
    controller: BankrollController,
    args: list[str],
) -> None:
    if not args:
        await message.channel.send(
            "⚠️ Informe o valor e opcionalmente a casa.\n"
            "Exemplos:\n"
            "`!b saque 300 Betano Retirada parcial`\n"
            "`!b saque Betano 300 Retirada parcial`\n"
            "Se não informar a casa, o saque abate do saldo disponível das casas com maior saldo."
        )
        return

    # Aceita tanto "valor casa" quanto "casa valor"
    amount: Decimal | None = None
    bookmaker: str | int | None = None
    description_parts: list[str] = []

    if len(args) == 1:
        # Apenas valor
        amount = _parse_money(args[0])
    else:
        # Tenta identificar valor e casa nos dois primeiros argumentos
        first_is_money = _looks_like_money(args[0])
        second_is_money = _looks_like_money(args[1])

        if first_is_money and not second_is_money:
            amount = _parse_money(args[0])
            bookmaker = args[1]
            description_parts = args[2:]
        elif not first_is_money and second_is_money:
            bookmaker = args[0]
            amount = _parse_money(args[1])
            description_parts = args[2:]
        elif first_is_money and second_is_money:
            # Dois valores? Considera primeiro como valor e resto como descricao
            amount = _parse_money(args[0])
            description_parts = args[1:]
        else:
            # Nenhum parece dinheiro
            await message.channel.send("⚠️ Informe um valor válido. Exemplo: `!b saque 300 Betano`")
            return

    if amount is None or amount <= 0:
        await message.channel.send("⚠️ O valor deve ser maior que zero.")
        return

    description = " ".join(description_parts) if description_parts else None

    bookmaker_id: int | None = None
    bookmaker_name: str | None = None
    if bookmaker is not None:
        bookmaker_id = controller.resolve_bookmaker_id(bookmaker)
        if bookmaker_id is None:
            await message.channel.send(f"⚠️ Casa não encontrada: {bookmaker}")
            return
        bookmaker_name = controller._writer.get_bookmaker_name(bookmaker_id) or str(bookmaker)

    transaction_id = controller.record_transaction(
        transaction_type="withdrawal",
        amount=amount,
        created_by_user_id=message.author.id,
        description=description,
        bookmaker_id=bookmaker_id,
        bookmaker_name=bookmaker_name,
    )

    try:
        allocations = controller.allocate_withdrawal(amount=amount, bookmaker_name_or_id=bookmaker)
    except ValueError as exc:
        await message.channel.send(f"⚠️ {exc}")
        return

    text = f"⬇️ **Saque registrado**\n\n**{controller.format_money(amount)}**"
    if bookmaker_name:
        text += f"\nCasa: {bookmaker_name}"
    if description:
        text += f"\nDescrição: {description}"

    if allocations:
        text += "\n\nAbatido do saldo das casas:"
        for allocation in allocations:
            text += f"\n• {allocation.bookmaker_name}: {controller.format_money(allocation.available)}"
    else:
        text += "\n\n_Nenhum saldo disponível para abater (saque registrado apenas como movimentação)._"

    text += f"\n_ID #{transaction_id}_"
    await message.channel.send(text)


async def _handle_history(
    message: discord.Message,
    controller: BankrollController,
    args: list[str],
) -> None:
    days = 7
    if args:
        try:
            days = max(1, int(args[0]))
        except ValueError:
            await message.channel.send("⚠️ Informe um numero de dias valido.")
            return

    since_ts = int(time.time()) - days * 86400
    transactions = controller.list_transactions_since(since_ts)
    await message.channel.send(_format_transactions_message(controller, transactions, f"📒 Histórico ({days} dias)"))
