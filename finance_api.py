from __future__ import annotations

import logging
from urllib.parse import urlencode

import discord
from discord import app_commands
from discord.ext import commands

import config
from services.finance_api import FinanceApiClient, FinanceApiError


LOGGER = logging.getLogger(__name__)


def _xp_fmt(value: float | None) -> str:
    if value is None:
        return '—'
    return f'{value:,.0f} XP'.replace(',', '.')


def _money_fmt(value: float | None) -> str:
    if value is None:
        return '—'
    return f'R$ {value:,.0f}'.replace(',', '.')


def _build_embed(player: dict) -> discord.Embed:
    name = player.get('name') or '?'
    week_xp = player.get('week_xp')
    total_xp = player.get('total_xp')
    goal = player.get('goal', 250)
    goal_status = player.get('goal_status', 'no_comparison')
    missing_xp = player.get('missing_xp')
    weekly_rank = player.get('weekly_rank', '—')

    cash_delivered = player.get('cash_delivered', 0.0) or 0.0
    cash_target    = player.get('cash_target', 100000) or 100000
    cash_status    = player.get('cash_status', 'pending')
    cash_missing   = player.get('cash_missing', cash_target) or cash_target

    if goal_status == 'met':
        status_line = f'✅ Meta de {_xp_fmt(goal)} **atingida!**'
        color = discord.Color.green()
    elif goal_status == 'pending':
        status_line = f'⏳ Faltam **{_xp_fmt(missing_xp)}** para a meta de {_xp_fmt(goal)}'
        color = discord.Color.orange()
    else:
        status_line = f'❓ Sem dados suficientes (meta: {_xp_fmt(goal)})'
        color = discord.Color.greyple()

    if cash_status == 'met':
        cash_line = f'✅ {_money_fmt(cash_delivered)} entregues — **meta atingida!**'
    elif cash_status == 'partial':
        cash_line = f'⏳ {_money_fmt(cash_delivered)} de {_money_fmt(cash_target)} — faltam **{_money_fmt(cash_missing)}**'
    else:
        cash_line = f'❌ Nenhuma entrega registrada — faltam **{_money_fmt(cash_target)}**'

    embed = discord.Embed(title=f'📊 XP da Semana — {name}', color=color)
    embed.add_field(name='XP esta semana', value=_xp_fmt(week_xp), inline=True)
    embed.add_field(name='Rank semanal', value=f'#{weekly_rank}', inline=True)
    embed.add_field(name='XP total', value=_xp_fmt(total_xp), inline=True)
    embed.add_field(name='Meta de XP', value=status_line, inline=False)
    embed.add_field(name='💰 Sujo da semana', value=cash_line, inline=False)

    history: list[str] = []
    for label, key in [('S-1', 'week_1'), ('S-2', 'week_2'), ('S-3', 'week_3'), ('S-4', 'week_4')]:
        history.append(f'**{label}:** {_xp_fmt(player.get(key))}')
    if history:
        embed.add_field(name='Histórico XP', value=' | '.join(history), inline=False)

    return embed


class XpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.api = FinanceApiClient()

    async def cog_unload(self) -> None:
        await self.api.close()

    @app_commands.command(name='xp', description='Consulta o seu XP desta semana e o progresso da meta.')
    async def xp_semana(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        discord_user_id = str(interaction.user.id)
        query = urlencode({'discord_user_id': discord_user_id})

        try:
            data = await self.api.get(f'discord_xp_query.php?{query}')
        except FinanceApiError as exc:
            LOGGER.warning('xp_semana: erro na API para %s: %s', discord_user_id, exc)
            await interaction.followup.send(
                f'❌ Não foi possível consultar a API: {exc}',
                ephemeral=True,
            )
            return

        player = data.get('player')
        if not player:
            await interaction.followup.send(
                '❌ Você não está vinculado a nenhum jogador no sistema.\n'
                'Peça a um administrador para vincular o seu Discord.',
                ephemeral=True,
            )
            return

        embed = _build_embed(player)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(XpCog(bot))
