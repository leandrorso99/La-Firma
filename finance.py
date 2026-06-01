from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

import config


LOGGER = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent / 'acoes.db'


# ── helpers ────────────────────────────────────────────────────────────────────

def money_text(value: int) -> str:
    return f'R$ {value:,}'


def role_ids_from_config(name: str) -> set[int]:
    values = getattr(config, name, []) or []
    parsed: set[int] = set()
    for value in values:
        try:
            role_id = int(value)
        except (TypeError, ValueError):
            continue
        if role_id > 0:
            parsed.add(role_id)
    return parsed


def has_any_role(member: discord.Member, role_ids: set[int]) -> bool:
    if not role_ids:
        return False
    return any(role.id in role_ids for role in member.roles)


def is_manager(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    return has_any_role(interaction.user, role_ids_from_config('ACOES_MANAGER_ROLE_IDS'))


def _get_week_start() -> str:
    today = datetime.utcnow()
    return (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')


# ── database ───────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS action_types (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL UNIQUE,
                emoji       TEXT    NOT NULL DEFAULT '🎯',
                max_members INTEGER NOT NULL DEFAULT 4,
                min_members INTEGER NOT NULL DEFAULT 1,
                rules       TEXT    NOT NULL DEFAULT '',
                created_by  TEXT,
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS actions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type_id     INTEGER NOT NULL,
                action_type_name   TEXT    NOT NULL,
                participants       TEXT    NOT NULL,
                amount_per_member  INTEGER NOT NULL DEFAULT 0,
                result             TEXT    NOT NULL,
                registered_by_id   TEXT    NOT NULL,
                registered_by_name TEXT    NOT NULL,
                week_start         TEXT    NOT NULL,
                created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS action_type_images (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type_id INTEGER NOT NULL,
                url            TEXT    NOT NULL,
                position       INTEGER NOT NULL DEFAULT 0,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (action_type_id) REFERENCES action_types(id)
            );
        """)
        # migrations para bancos existentes
        for migration in (
            "ALTER TABLE action_types ADD COLUMN rules TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass


def _list_action_types() -> list[dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute(
            'SELECT * FROM action_types WHERE active = 1 ORDER BY name'
        ).fetchall()
    return [dict(r) for r in rows]


def _get_action_type(type_id: int) -> dict[str, Any] | None:
    with _db() as conn:
        row = conn.execute(
            'SELECT * FROM action_types WHERE id = ?', (type_id,)
        ).fetchone()
    return dict(row) if row else None


def _add_action_type(name: str, emoji: str, max_m: int, min_m: int, rules: str, created_by: str) -> None:
    with _db() as conn:
        existing = conn.execute(
            'SELECT id FROM action_types WHERE name = ?', (name,)
        ).fetchone()
        if existing:
            conn.execute(
                'UPDATE action_types SET emoji=?, max_members=?, min_members=?, rules=?, created_by=?, active=1 WHERE id=?',
                (emoji, max_m, min_m, rules, created_by, existing['id']),
            )
        else:
            conn.execute(
                'INSERT INTO action_types (name, emoji, max_members, min_members, rules, created_by) VALUES (?,?,?,?,?,?)',
                (name, emoji, max_m, min_m, rules, created_by),
            )


def _update_action_type(type_id: int, name: str, emoji: str, max_m: int, min_m: int, rules: str) -> bool:
    """Atualiza a ação. Retorna False se o novo nome já existe em outra ação."""
    with _db() as conn:
        conflict = conn.execute(
            'SELECT id FROM action_types WHERE name = ? AND id != ?', (name, type_id)
        ).fetchone()
        if conflict:
            return False
        conn.execute(
            'UPDATE action_types SET name=?, emoji=?, max_members=?, min_members=?, rules=? WHERE id=?',
            (name, emoji, max_m, min_m, rules, type_id),
        )
    return True


def _get_images(type_id: int) -> list[str]:
    with _db() as conn:
        rows = conn.execute(
            'SELECT url FROM action_type_images WHERE action_type_id = ? ORDER BY position, id',
            (type_id,),
        ).fetchall()
    return [r['url'] for r in rows]


def _add_image_urls(type_id: int, urls: list[str]) -> None:
    with _db() as conn:
        next_pos = (conn.execute(
            'SELECT COALESCE(MAX(position), -1) + 1 FROM action_type_images WHERE action_type_id = ?',
            (type_id,),
        ).fetchone()[0])
        conn.executemany(
            'INSERT INTO action_type_images (action_type_id, url, position) VALUES (?,?,?)',
            [(type_id, url, next_pos + i) for i, url in enumerate(urls)],
        )


def _clear_images(type_id: int) -> None:
    with _db() as conn:
        conn.execute('DELETE FROM action_type_images WHERE action_type_id = ?', (type_id,))


async def _archive_images(
    interaction: discord.Interaction,
    attachments: list[discord.Attachment],
    action_name: str,
) -> tuple[list[str], bool]:
    """
    Baixa as imagens e re-envia para o canal de arquivo (ou canal atual como fallback).
    Retorna (urls_permanentes, deve_deletar_mensagem_original).
    Baixar ANTES de qualquer deleção garante que as URLs sejam válidas.
    """
    images = [a for a in attachments if a.content_type and 'image' in a.content_type]
    if not images:
        return [], False

    # Baixa o conteúdo de todas as imagens ANTES de deletar qualquer mensagem
    files = [await a.to_file() for a in images]

    archive_id = int(getattr(config, 'ACOES_IMAGES_CHANNEL_ID', 0) or 0)
    target: discord.TextChannel | None = None

    if archive_id and interaction.guild:
        ch = interaction.guild.get_channel(archive_id)
        if isinstance(ch, discord.TextChannel):
            target = ch

    if target is None:
        # Sem canal configurado: re-sobe no próprio canal (mensagem fica permanente lá)
        if isinstance(interaction.channel, discord.TextChannel):
            target = interaction.channel

    if target is None:
        return [], False

    # Cabeçalho identificando a ação antes das imagens
    header = (
        f'🗺️ **Perímetro: {action_name}**\n'
        f'`{len(images)} imagem(ns)` • adicionado por {interaction.user.display_name} '
        f'em <t:{int(datetime.utcnow().timestamp())}:f>'
    )
    await target.send(header)

    # Re-upload em lotes de 10 (limite do Discord por mensagem)
    urls: list[str] = []
    for i in range(0, len(files), 10):
        sent = await target.send(files=files[i:i + 10])
        urls.extend(a.url for a in sent.attachments)

    # Só apaga a mensagem original se foi arquivado em canal separado
    should_delete = archive_id > 0 and target.id == archive_id
    return urls, should_delete


def _deactivate_action_type(type_id: int) -> None:
    with _db() as conn:
        conn.execute('UPDATE action_types SET active = 0 WHERE id = ?', (type_id,))


def _save_action(
    type_id: int,
    type_name: str,
    participants: list[dict[str, Any]],
    amount_per_member: int,
    result: str,
    registered_by_id: str,
    registered_by_name: str,
) -> None:
    with _db() as conn:
        conn.execute(
            """INSERT INTO actions
               (action_type_id, action_type_name, participants, amount_per_member,
                result, registered_by_id, registered_by_name, week_start)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                type_id, type_name, json.dumps(participants),
                amount_per_member, result,
                registered_by_id, registered_by_name, _get_week_start(),
            ),
        )


def _clear_actions() -> None:
    with _db() as conn:
        conn.execute('DELETE FROM actions')


def _get_report_data(days: int = 7) -> dict[str, Any]:
    since = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    with _db() as conn:
        rows = conn.execute(
            'SELECT * FROM actions WHERE created_at >= ?', (since,)
        ).fetchall()

    victories: dict[str, int] = {}
    defeats: dict[str, int] = {}
    total_amount = 0
    total_defeats = 0

    for row in rows:
        participants = json.loads(row['participants'])
        amount = row['amount_per_member']
        result = row['result']
        total_amount += amount * len(participants)

        for p in participants:
            name = p['name']
            if result == 'vitoria':
                victories[name] = victories.get(name, 0) + 1
            else:
                defeats[name] = defeats.get(name, 0) + 1
                total_defeats += 1

    return {
        'top_victories': sorted(victories.items(), key=lambda x: x[1], reverse=True)[:3],
        'top_defeats': sorted(defeats.items(), key=lambda x: x[1], reverse=True)[:3],
        'total_amount': total_amount,
        'family_cut': int(total_amount * 0.20),
        'total_defeats': total_defeats,
        'total_actions': len(rows),
        'days': days,
    }


def _build_report_embed(days: int = 7) -> discord.Embed:
    data = _get_report_data(days)

    embed = discord.Embed(
        title='📊 Relatório Semanal de Ações',
        color=0xF0B232,
        timestamp=datetime.utcnow(),
    )

    v_lines = '\n'.join(f'@{n}: **{c}** vitórias' for n, c in data['top_victories'])
    d_lines = '\n'.join(f'@{n}: **{c}** derrotas' for n, c in data['top_defeats'])

    embed.add_field(name='🏆 Top 3 – Mais Vitórias', value=v_lines or 'Nenhuma', inline=True)
    embed.add_field(name='💀 Top 3 – Mais Derrotas', value=d_lines or 'Nenhuma', inline=True)
    embed.add_field(name='​', value='​', inline=False)
    embed.add_field(
        name='💰 Arrecadação da Família (20%)',
        value=f'{money_text(data["family_cut"])} sujo',
        inline=True,
    )
    embed.add_field(
        name='📈 Ações Totais Perdidas',
        value=f'{data["total_defeats"]} ações',
        inline=True,
    )
    embed.set_footer(text=f'Dados dos últimos {days} dias.')
    return embed


# ── flow views ─────────────────────────────────────────────────────────────────

class ResultView(discord.ui.View):
    def __init__(self, action_data: dict[str, Any]) -> None:
        super().__init__(timeout=300)
        self.action_data = action_data

    @discord.ui.button(label='Vitória', style=discord.ButtonStyle.success, emoji='🏆')
    async def vitoria(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        await self._finish(interaction, 'vitoria')

    @discord.ui.button(label='Derrota', style=discord.ButtonStyle.danger, emoji='💀')
    async def derrota(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        await self._finish(interaction, 'derrota')

    async def _finish(self, interaction: discord.Interaction, result: str) -> None:
        d = self.action_data
        _save_action(
            type_id=d['action_type']['id'],
            type_name=d['action_type']['name'],
            participants=d['participants'],
            amount_per_member=d['amount_per_member'],
            result=result,
            registered_by_id=d['registered_by_id'],
            registered_by_name=d['registered_by_name'],
        )

        amount = d['amount_per_member']
        count = len(d['participants'])
        total = amount * count
        individual_repasse = int(amount * 0.20)
        total_repasse = int(total * 0.20)

        emoji = '🏆' if result == 'vitoria' else '💀'
        color = 0x57F287 if result == 'vitoria' else 0xED4245
        result_word = 'Vencida' if result == 'vitoria' else 'Perdida'

        embed = discord.Embed(
            title=f'{emoji} Ação {result_word}: {d["action_type"]["name"]}',
            color=color,
            timestamp=datetime.utcnow(),
        )

        participants_text = '\n'.join(f'• @{p["name"]}' for p in d['participants'])
        embed.add_field(name='👥 Participantes', value=participants_text, inline=False)

        if amount > 0:
            embed.add_field(name='💰 Ganho Individual', value=money_text(amount), inline=True)
            embed.add_field(name='💰 Ganho Total', value=money_text(total), inline=True)
            embed.add_field(name='​', value='​', inline=False)
            embed.add_field(name='💸 Repasse Individual (20%)', value=money_text(individual_repasse), inline=True)
            embed.add_field(name='🏦 Repasse Família (Total)', value=money_text(total_repasse), inline=True)

        embed.set_footer(text=f'Registrado por {d["registered_by_name"]}')

        delete_after = int(getattr(config, 'ACOES_EPHEMERAL_DELETE_AFTER_SECONDS', 0) or 0) or None
        await interaction.response.edit_message(embed=embed, view=None, delete_after=delete_after)

        log_channel_id = int(getattr(config, 'ACOES_LOG_CHANNEL_ID', 0) or 0)
        if log_channel_id and interaction.guild:
            ch = interaction.guild.get_channel(log_channel_id)
            if isinstance(ch, discord.TextChannel):
                log_delete = int(getattr(config, 'ACOES_LOG_DELETE_AFTER_SECONDS', 0) or 0) or None
                await ch.send(embed=embed, delete_after=log_delete)
                LOGGER.info('Ação registrada por %s no log channel.', d['registered_by_name'])


class AmountModal(discord.ui.Modal, title='Lucro da Ação'):
    amount = discord.ui.TextInput(
        label='Valor arrecadado (por membro)?',
        placeholder='Apenas números. Digite 0 se marcar Derrota.',
        required=True,
        max_length=12,
    )

    def __init__(self, action_type: dict[str, Any], participants: list[dict[str, Any]]) -> None:
        super().__init__()
        self.action_type = action_type
        self.participants = participants

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.amount.value.strip().replace('.', '').replace(',', '')
        if not raw.isdigit():
            await interaction.response.send_message(
                '❌ Valor inválido. Digite apenas números (ex: 50000).', ephemeral=True
            )
            return

        amount = int(raw)
        total = amount * len(self.participants)

        action_data = {
            'action_type': self.action_type,
            'participants': self.participants,
            'amount_per_member': amount,
            'registered_by_id': str(interaction.user.id),
            'registered_by_name': interaction.user.display_name,
        }

        embed = discord.Embed(
            title='🏁 Finalizar Registro',
            description='Selecione abaixo qual foi o resultado final desta ação.',
            color=0x5865F2,
        )
        embed.add_field(name='Ação', value=self.action_type['name'], inline=True)
        embed.add_field(name='Participantes', value=str(len(self.participants)), inline=True)
        embed.add_field(
            name='Arrecadação',
            value=f'{money_text(amount)}/membro  •  Total: {money_text(total)}',
            inline=False,
        )
        await interaction.response.edit_message(embed=embed, view=ResultView(action_data))


class MemberSelectView(discord.ui.View):
    def __init__(self, action_type: dict[str, Any]) -> None:
        super().__init__(timeout=300)
        self.action_type = action_type
        self.selected_members: list[discord.Member] = []

        user_select = discord.ui.UserSelect(
            placeholder=f'Selecione os participantes (Máx: {action_type["max_members"]})',
            min_values=action_type['min_members'],
            max_values=action_type['max_members'],
        )
        user_select.callback = self._on_select
        self._user_select = user_select
        self.add_item(user_select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.selected_members = list(self._user_select.values)
        await interaction.response.defer()

    @discord.ui.button(label='Continuar', style=discord.ButtonStyle.primary)
    async def continuar(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not self.selected_members:
            await interaction.response.send_message(
                '⚠️ Selecione pelo menos um participante.', ephemeral=True
            )
            return

        participants = [
            {'id': str(m.id), 'name': m.display_name}
            for m in self.selected_members
            if not m.bot
        ]
        if not participants:
            await interaction.response.send_message(
                '❌ Nenhum membro válido selecionado.', ephemeral=True
            )
            return

        await interaction.response.send_modal(AmountModal(self.action_type, participants))


class ActionTypeView(discord.ui.View):
    def __init__(self, action_types: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.selected_id: int | None = None

        options = [
            discord.SelectOption(
                label=at['name'],
                value=str(at['id']),
                description=f'Permite até {at["max_members"]} membros.',
                emoji=at['emoji'] or None,
            )
            for at in action_types
        ]
        select = discord.ui.Select(
            placeholder='Qual ação vocês fizeram?',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self._select = select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.selected_id = int(self._select.values[0])
        await interaction.response.defer()

    @discord.ui.button(label='Próximo Passo', style=discord.ButtonStyle.primary)
    async def proximo(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if self.selected_id is None:
            await interaction.response.send_message(
                '⚠️ Selecione uma ação primeiro.', ephemeral=True
            )
            return

        action_type = _get_action_type(self.selected_id)
        if not action_type:
            await interaction.response.send_message('❌ Ação não encontrada.', ephemeral=True)
            return

        embed = discord.Embed(
            title='👥 Participantes',
            description=(
                f'Ação: **{action_type["name"]}**\n'
                f'Selecione de {action_type["min_members"]} a {action_type["max_members"]} membros.'
            ),
            color=0x5865F2,
        )
        await interaction.response.edit_message(embed=embed, view=MemberSelectView(action_type))


# ── rules views ───────────────────────────────────────────────────────────────

class RulesSelectView(discord.ui.View):
    def __init__(self, action_types: list[dict[str, Any]]) -> None:
        super().__init__(timeout=120)

        options = [
            discord.SelectOption(
                label=at['name'],
                value=str(at['id']),
                description=f'Máx: {at["max_members"]} membros',
                emoji=at['emoji'] or None,
            )
            for at in action_types
        ]
        select = discord.ui.Select(
            placeholder='Selecione uma ação para ver as regras...',
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self._select = select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        at = _get_action_type(int(self._select.values[0]))
        if not at:
            await interaction.response.send_message('❌ Ação não encontrada.', ephemeral=True)
            return

        rules_text = at.get('rules', '').strip() or '_Nenhuma regra cadastrada para esta ação._'
        images = _get_images(at['id'])

        main_embed = discord.Embed(
            title=f'{at["emoji"]} Regras: {at["name"]}',
            description=rules_text,
            color=0x5865F2,
        )
        main_embed.add_field(
            name='👥 Participantes',
            value=f'Mínimo: {at["min_members"]}  •  Máximo: {at["max_members"]}',
            inline=False,
        )
        if images:
            main_embed.add_field(
                name='🗺️ Perímetros',
                value=f'{len(images)} imagem(ns) abaixo',
                inline=False,
            )
            main_embed.set_image(url=images[0])
        main_embed.set_footer(text='Sistema LA FIRMA')

        # embeds adicionais para as imagens extras (Discord suporta até 10 por mensagem)
        extra_embeds = [
            discord.Embed(color=0x5865F2).set_image(url=url)
            for url in images[1:]
        ]

        await interaction.response.edit_message(
            embeds=[main_embed, *extra_embeds], view=None
        )


# ── confirm zerar ──────────────────────────────────────────────────────────────

class ConfirmZerarView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=60)

    @discord.ui.button(label='Confirmar', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        _clear_actions()
        await interaction.response.edit_message(content='✅ Todas as ações foram zeradas.', view=None)

    @discord.ui.button(label='Cancelar', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        await interaction.response.edit_message(content='❌ Operação cancelada.', view=None)


# ── main panel (persistent) ────────────────────────────────────────────────────

class MainPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label='Registrar Ação',
        style=discord.ButtonStyle.danger,
        emoji='🎯',
        custom_id='acoes:registrar',
    )
    async def registrar(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        types = _list_action_types()
        if not types:
            await interaction.response.send_message(
                '⚠️ Nenhuma ação cadastrada. Peça ao admin para cadastrar.',
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title='📌 Registro de Ação',
            description='Selecione qual ação foi realizada:',
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, view=ActionTypeView(types), ephemeral=True)

    @discord.ui.button(
        label='Relatório Líderes',
        style=discord.ButtonStyle.primary,
        emoji='📊',
        custom_id='acoes:relatorio',
    )
    async def relatorio(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        embed = _build_report_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label='Regras',
        style=discord.ButtonStyle.secondary,
        emoji='📋',
        custom_id='acoes:regras',
    )
    async def regras(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        types = _list_action_types()
        if not types:
            await interaction.response.send_message(
                '⚠️ Nenhuma ação cadastrada ainda.', ephemeral=True
            )
            return
        embed = discord.Embed(
            title='📋 Regras das Ações',
            description='Selecione uma ação para ver as regras:',
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, view=RulesSelectView(types), ephemeral=True)

    @discord.ui.button(
        label='Zerar Ações',
        style=discord.ButtonStyle.secondary,
        emoji='🗑️',
        custom_id='acoes:zerar',
    )
    async def zerar(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not is_manager(interaction):
            await interaction.response.send_message(
                '❌ Apenas admins e gerentes podem zerar as ações.', ephemeral=True
            )
            return
        await interaction.response.send_message(
            '⚠️ Tem certeza que deseja zerar **todas** as ações?',
            view=ConfirmZerarView(),
            ephemeral=True,
        )


# ── admin panel (persistent) ───────────────────────────────────────────────────

class AddActionModal(discord.ui.Modal, title='Cadastrar Nova Ação'):
    name = discord.ui.TextInput(
        label='Nome da Ação',
        placeholder='Ex: Ammunation, Fleeca, Barbearia...',
        max_length=100,
    )
    emoji = discord.ui.TextInput(
        label='Emoji (opcional)',
        placeholder='Ex: 🔫 🏦 💈',
        required=False,
        max_length=8,
    )
    max_members = discord.ui.TextInput(
        label='Máximo de Membros',
        placeholder='Ex: 4',
        max_length=2,
    )
    min_members = discord.ui.TextInput(
        label='Mínimo de Membros',
        placeholder='Ex: 1',
        default='1',
        max_length=2,
    )
    rules = discord.ui.TextInput(
        label='Regras da Ação',
        placeholder='Ex: Proibido sair do local, esperar o líder sinalizar...',
        required=False,
        max_length=1000,
        style=discord.TextStyle.long,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.max_members.value.isdigit() or not self.min_members.value.isdigit():
            await interaction.response.send_message(
                '❌ Mínimo e máximo devem ser números inteiros.', ephemeral=True
            )
            return

        max_m, min_m = int(self.max_members.value), int(self.min_members.value)
        if min_m < 1 or max_m < 1 or min_m > max_m:
            await interaction.response.send_message(
                '❌ Valores inválidos. Mínimo deve ser ≥ 1 e ≤ máximo.', ephemeral=True
            )
            return

        name_val = self.name.value.strip()
        emoji_val = self.emoji.value.strip() or '🎯'
        rules_val = self.rules.value.strip()

        try:
            _add_action_type(
                name=name_val, emoji=emoji_val,
                max_m=max_m, min_m=min_m,
                rules=rules_val,
                created_by=interaction.user.display_name,
            )
            await interaction.response.send_message(
                f'✅ Ação **{name_val}** cadastrada!\nMembros: {min_m} – {max_m}  •  Emoji: {emoji_val}',
                ephemeral=True,
            )
        except Exception as exc:
            LOGGER.exception('Erro ao cadastrar ação "%s": %s', name_val, exc)
            await interaction.response.send_message(
                '❌ Erro ao cadastrar a ação. Tente novamente.', ephemeral=True
            )


class RemoveActionView(discord.ui.View):
    def __init__(self, action_types: list[dict[str, Any]]) -> None:
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(
                label=at['name'],
                value=str(at['id']),
                description=f'Máx: {at["max_members"]} membros',
                emoji=at['emoji'] or None,
            )
            for at in action_types
        ]
        select = discord.ui.Select(placeholder='Qual ação remover?', options=options)
        select.callback = self._on_select
        self._select = select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        action_id = int(self._select.values[0])
        at = _get_action_type(action_id)
        _deactivate_action_type(action_id)
        name = at['name'] if at else str(action_id)
        await interaction.response.edit_message(
            content=f'✅ Ação **{name}** removida com sucesso.', view=None
        )


class EditActionModal(discord.ui.Modal, title='Editar Ação'):
    def __init__(self, action: dict[str, Any]) -> None:
        super().__init__()
        self.action_id = action['id']

        self.name_input = discord.ui.TextInput(
            label='Nome da Ação',
            default=action['name'],
            max_length=100,
        )
        self.emoji_input = discord.ui.TextInput(
            label='Emoji',
            default=action['emoji'] or '🎯',
            required=False,
            max_length=8,
        )
        self.max_input = discord.ui.TextInput(
            label='Máximo de Membros',
            default=str(action['max_members']),
            max_length=2,
        )
        self.min_input = discord.ui.TextInput(
            label='Mínimo de Membros',
            default=str(action['min_members']),
            max_length=2,
        )
        self.rules_input = discord.ui.TextInput(
            label='Regras da Ação',
            default=action.get('rules', '') or '',
            required=False,
            max_length=1000,
            style=discord.TextStyle.long,
        )

        self.add_item(self.name_input)
        self.add_item(self.emoji_input)
        self.add_item(self.max_input)
        self.add_item(self.min_input)
        self.add_item(self.rules_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.max_input.value.isdigit() or not self.min_input.value.isdigit():
            await interaction.response.send_message(
                '❌ Mínimo e máximo devem ser números inteiros.', ephemeral=True
            )
            return

        max_m, min_m = int(self.max_input.value), int(self.min_input.value)
        if min_m < 1 or max_m < 1 or min_m > max_m:
            await interaction.response.send_message(
                '❌ Valores inválidos. Mínimo deve ser ≥ 1 e ≤ máximo.', ephemeral=True
            )
            return

        name_val = self.name_input.value.strip()
        emoji_val = self.emoji_input.value.strip() or '🎯'
        rules_val = self.rules_input.value.strip()

        ok = _update_action_type(
            type_id=self.action_id,
            name=name_val,
            emoji=emoji_val,
            max_m=max_m,
            min_m=min_m,
            rules=rules_val,
        )
        if not ok:
            await interaction.response.send_message(
                f'❌ Já existe outra ação com o nome **{name_val}**.', ephemeral=True
            )
            return

        await interaction.response.send_message(
            f'✅ Ação **{name_val}** atualizada!\nMembros: {min_m} – {max_m}  •  Emoji: {emoji_val}',
            ephemeral=True,
        )


class EditActionSelectView(discord.ui.View):
    def __init__(self, action_types: list[dict[str, Any]]) -> None:
        super().__init__(timeout=120)

        options = [
            discord.SelectOption(
                label=at['name'],
                value=str(at['id']),
                description=f'Máx: {at["max_members"]} membros',
                emoji=at['emoji'] or None,
            )
            for at in action_types
        ]
        select = discord.ui.Select(
            placeholder='Selecione a ação para editar...',
            options=options,
        )
        select.callback = self._on_select
        self._select = select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        at = _get_action_type(int(self._select.values[0]))
        if not at:
            await interaction.response.send_message('❌ Ação não encontrada.', ephemeral=True)
            return
        await interaction.response.send_modal(EditActionModal(at))


class ManageImagesView(discord.ui.View):
    """Exibe imagens atuais e oferece opções para adicionar ou limpar."""

    def __init__(self, action: dict[str, Any]) -> None:
        super().__init__(timeout=300)
        self.action = action

    @discord.ui.button(label='Adicionar Imagens', style=discord.ButtonStyle.primary, emoji='📸')
    async def adicionar(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        await interaction.response.send_message(
            f'📸 Envie as imagens do perímetro de **{self.action["name"]}** neste canal.\n'
            '> Pode enviar até **10 imagens** de uma vez. Você tem **3 minutos**.',
            ephemeral=True,
        )

        def check(m: discord.Message) -> bool:
            return (
                m.author.id == interaction.user.id
                and m.channel.id == interaction.channel.id
                and bool(m.attachments)
            )

        try:
            msg = await interaction.client.wait_for('message', timeout=180.0, check=check)
        except asyncio.TimeoutError:
            await interaction.followup.send('⏱️ Tempo esgotado. Nenhuma imagem foi salva.', ephemeral=True)
            return

        # _archive_images baixa os bytes ANTES de deletar qualquer mensagem
        urls, should_delete = await _archive_images(interaction, msg.attachments, self.action['name'])
        if not urls:
            await interaction.followup.send('❌ Nenhuma imagem válida encontrada na mensagem.', ephemeral=True)
            return

        _add_image_urls(self.action['id'], urls)

        # Só apaga a mensagem original se as imagens foram salvas em canal separado
        if should_delete:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

        total = len(_get_images(self.action['id']))
        await interaction.followup.send(
            f'✅ **{len(urls)}** imagem(ns) adicionada(s)! Total: {total} imagem(ns) no perímetro.',
            ephemeral=True,
        )

    @discord.ui.button(label='Limpar Todas', style=discord.ButtonStyle.danger, emoji='🗑️')
    async def limpar(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        _clear_images(self.action['id'])
        await interaction.response.send_message(
            f'✅ Todas as imagens de **{self.action["name"]}** foram removidas.',
            ephemeral=True,
        )


class ManageImagesSelectView(discord.ui.View):
    def __init__(self, action_types: list[dict[str, Any]]) -> None:
        super().__init__(timeout=120)

        options = [
            discord.SelectOption(
                label=at['name'],
                value=str(at['id']),
                description=f'Máx: {at["max_members"]} membros',
                emoji=at['emoji'] or None,
            )
            for at in action_types
        ]
        select = discord.ui.Select(
            placeholder='Selecione a ação para gerenciar imagens...',
            options=options,
        )
        select.callback = self._on_select
        self._select = select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        at = _get_action_type(int(self._select.values[0]))
        if not at:
            await interaction.response.send_message('❌ Ação não encontrada.', ephemeral=True)
            return

        images = _get_images(at['id'])
        count = len(images)

        embed = discord.Embed(
            title=f'🖼️ Perímetros: {at["name"]}',
            description=(
                f'**{count}** imagem(ns) cadastrada(s).\n\n'
                'Use os botões abaixo para adicionar ou limpar as imagens do perímetro.'
            ),
            color=0x5865F2,
        )
        if images:
            embed.set_image(url=images[0])
            if count > 1:
                embed.set_footer(text=f'Exibindo 1 de {count} imagens')

        await interaction.response.edit_message(embed=embed, view=ManageImagesView(at))


class AdminPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label='Adicionar Ação',
        style=discord.ButtonStyle.success,
        emoji='➕',
        custom_id='admin_acoes:adicionar',
    )
    async def adicionar(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not is_manager(interaction):
            await interaction.response.send_message('❌ Sem permissão.', ephemeral=True)
            return
        await interaction.response.send_modal(AddActionModal())

    @discord.ui.button(
        label='Editar Ação',
        style=discord.ButtonStyle.primary,
        emoji='✏️',
        custom_id='admin_acoes:editar',
    )
    async def editar(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not is_manager(interaction):
            await interaction.response.send_message('❌ Sem permissão.', ephemeral=True)
            return
        types = _list_action_types()
        if not types:
            await interaction.response.send_message('Nenhuma ação cadastrada.', ephemeral=True)
            return
        await interaction.response.send_message(
            'Selecione a ação para editar:',
            view=EditActionSelectView(types),
            ephemeral=True,
        )

    @discord.ui.button(
        label='Remover Ação',
        style=discord.ButtonStyle.danger,
        emoji='🗑️',
        custom_id='admin_acoes:remover',
    )
    async def remover(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not is_manager(interaction):
            await interaction.response.send_message('❌ Sem permissão.', ephemeral=True)
            return
        types = _list_action_types()
        if not types:
            await interaction.response.send_message('Nenhuma ação cadastrada.', ephemeral=True)
            return
        await interaction.response.send_message(
            'Selecione a ação para remover:',
            view=RemoveActionView(types),
            ephemeral=True,
        )

    @discord.ui.button(
        label='Imagens',
        style=discord.ButtonStyle.secondary,
        emoji='🖼️',
        custom_id='admin_acoes:imagens',
    )
    async def imagens(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not is_manager(interaction):
            await interaction.response.send_message('❌ Sem permissão.', ephemeral=True)
            return
        types = _list_action_types()
        if not types:
            await interaction.response.send_message('Nenhuma ação cadastrada.', ephemeral=True)
            return
        embed = discord.Embed(
            title='🖼️ Gerenciar Imagens de Perímetro',
            description='Selecione a ação para adicionar ou limpar imagens:',
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, view=ManageImagesSelectView(types), ephemeral=True)

    @discord.ui.button(
        label='Ver Ações',
        style=discord.ButtonStyle.secondary,
        emoji='📋',
        custom_id='admin_acoes:listar',
    )
    async def listar(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        types = _list_action_types()
        if not types:
            await interaction.response.send_message('Nenhuma ação cadastrada.', ephemeral=True)
            return
        embed = discord.Embed(title='📋 Ações Disponíveis', color=0x5865F2)
        for at in types:
            embed.add_field(
                name=f'{at["emoji"]} {at["name"]}',
                value=f'Mín: {at["min_members"]}  •  Máx: {at["max_members"]} membros',
                inline=True,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── cog ────────────────────────────────────────────────────────────────────────

class AcoesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        _init_db()
        LOGGER.info('AcoesCog iniciado – banco de dados pronto em %s', _DB_PATH)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.bot.add_view(MainPanelView())
        self.bot.add_view(AdminPanelView())
        LOGGER.info('Views persistentes de ações registradas.')

    acoes = app_commands.Group(name='acoes', description='Sistema de ações da facção')

    @acoes.command(name='painel', description='Envia o painel principal de ações neste canal')
    @app_commands.default_permissions(administrator=True)
    async def cmd_painel(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title='🎯 CENTRAL DE AÇÕES – LA FIRMA',
            description=(
                'Utilize este painel para registrar as ações da facção.\n\n'
                '**Como funciona:**\n'
                '1️⃣  Selecione o tipo de ação.\n'
                '2️⃣  Marque todos os membros que participaram.\n'
                '3️⃣  Insira o valor recebido e informe se foi vitória ou derrota.'
            ),
            color=0xED4245,
        )
        embed.set_footer(text='Sistema LA FIRMA')
        await interaction.channel.send(embed=embed, view=MainPanelView())
        await interaction.response.send_message('✅ Painel enviado!', ephemeral=True)

    @acoes.command(name='adminpainel', description='Envia o painel de gerenciamento de ações neste canal')
    @app_commands.default_permissions(administrator=True)
    async def cmd_admin_painel(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title='⚙️ GERENCIAR AÇÕES – LA FIRMA',
            description=(
                'Painel exclusivo para **admins e gerentes**.\n\n'
                'Cadastre, remova ou visualize as ações disponíveis para registro.'
            ),
            color=0x5865F2,
        )
        embed.set_footer(text='Sistema LA FIRMA | Admin')
        await interaction.channel.send(embed=embed, view=AdminPanelView())
        await interaction.response.send_message('✅ Painel admin enviado!', ephemeral=True)

    @acoes.command(name='relatorio', description='Exibe o relatório semanal de ações publicamente')
    async def cmd_relatorio(self, interaction: discord.Interaction) -> None:
        embed = _build_report_embed()
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AcoesCog(bot))
