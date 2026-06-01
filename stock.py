from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from services.finance_api import FinanceApiClient, FinanceApiError


LOGGER = logging.getLogger(__name__)


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


def format_rental_date(value: Any) -> str:
    raw = str(value or '').strip()
    if raw == '':
        return ''
    for fmt in ('%Y-%m-%d', '%d/%m/%Y'):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.strftime('%d/%m/%Y')
        except ValueError:
            continue
    return raw


def build_rental_notification_message(rental_row: dict[str, Any], reminder_days: int, *, simulation: bool = False) -> str:
    house_name = str(rental_row.get('house_name', 'Casa')).strip() or 'Casa'
    duration_days = int(rental_row.get('duration_days', 0) or 0)
    purchased_at = format_rental_date(rental_row.get('purchased_at')) or 'Não informado'
    expires_at = format_rental_date(rental_row.get('expires_at')) or 'Não informado'

    if reminder_days <= 0:
        headline = 'vence hoje'
    elif reminder_days == 1:
        headline = 'vence amanhã'
    else:
        headline = f'vence em {reminder_days} dias'

    lines = [
        'Aviso de vencimento de aluguel por diamantes.',
        '',
        f'Casa: {house_name}',
        f'Comprada em: {purchased_at}',
        f'Duração: {duration_days} dias',
        f'Vencimento: {expires_at}',
        f'Status: {headline}.',
        '',
        'Renove o aluguel para evitar perder o imóvel.',
    ]
    if simulation:
        lines.insert(0, '[SIMULAÇÃO]')
    return '\n'.join(lines)


class RentalRegisterModal(discord.ui.Modal):
    def __init__(self, cog: 'RentalCog') -> None:
        super().__init__(title='Cadastrar Vencimento de Aluguel')
        self.cog = cog
        self.house_name = discord.ui.TextInput(
            label='Nome da casa',
            placeholder='Ex: Casa Vinewood 01',
            required=True,
            max_length=191,
        )
        self.purchased_at = discord.ui.TextInput(
            label='Data da compra',
            placeholder='Ex: 07/04/2026',
            required=True,
            max_length=10,
        )
        self.duration_days = discord.ui.TextInput(
            label='Dias do aluguel',
            placeholder='Ex: 7, 15, 30 ou outro valor',
            required=True,
            max_length=4,
        )
        self.add_item(self.house_name)
        self.add_item(self.purchased_at)
        self.add_item(self.duration_days)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            duration_days = int(str(self.duration_days).strip())
        except ValueError:
            await self.cog.send_ephemeral_message(interaction, content='Informe um número válido de dias para o aluguel.')
            return

        try:
            await interaction.response.defer(ephemeral=True)
            await self.cog.register_rental(
                interaction,
                str(self.house_name).strip(),
                str(self.purchased_at).strip(),
                duration_days,
            )
        except FinanceApiError as exc:
            await self.cog.send_ephemeral_message(interaction, content=f'Falha ao cadastrar aluguel: {exc}')


class RentalRecipientSelect(discord.ui.UserSelect):
    def __init__(self, parent_view: 'RentalRecipientView') -> None:
        self.parent_view = parent_view
        super().__init__(placeholder='Selecione quem vai receber os avisos', min_values=1, max_values=10)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.selected_user_ids = [str(user.id) for user in self.values]
        self.parent_view.selected_labels = [user.display_name for user in self.values]
        await interaction.response.edit_message(embed=self.parent_view.current_embed(), view=self.parent_view)


class RentalRecipientView(discord.ui.View):
    def __init__(self, cog: 'RentalCog', selected_user_ids: list[str]) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.selected_user_ids = selected_user_ids[:]
        self.selected_labels: list[str] = []
        self.add_item(RentalRecipientSelect(self))

    def current_embed(self) -> discord.Embed:
        description = 'Selecione os usuários do Discord que receberão DM automática quando um aluguel estiver perto de vencer.'
        if self.selected_user_ids:
            selected_text = '\n'.join(f'- <@{user_id}>' for user_id in self.selected_user_ids)
        else:
            selected_text = 'Nenhum usuário selecionado.'
        embed = discord.Embed(title='Destinatários dos Avisos de Aluguel', description=description, color=0xF0AD4E)
        embed.add_field(name='Selecionados', value=selected_text, inline=False)
        return embed

    @discord.ui.button(label='Salvar Destinatários', style=discord.ButtonStyle.success, emoji='💾')
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.cog.save_recipients(interaction, self.selected_user_ids)

    @discord.ui.button(label='Cancelar', style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content='Seleção de destinatários cancelada.', embed=None, view=None)


class RentalSimulationSelect(discord.ui.Select):
    def __init__(self, cog: 'RentalCog', rows: list[dict[str, Any]]) -> None:
        self.cog = cog
        self.rows = rows
        options: list[discord.SelectOption] = []

        for row in rows[:25]:
            rental_id = int(row.get('id', 0) or 0)
            if rental_id <= 0:
                continue
            house_name = str(row.get('house_name', 'Casa')).strip() or 'Casa'
            expires_at = format_rental_date(row.get('expires_at'))
            days_remaining = int(row.get('days_remaining', 0) or 0)
            if days_remaining < 0:
                description = f'Venceu em {expires_at}'
            elif days_remaining == 0:
                description = f'Vence hoje | {expires_at}'
            elif days_remaining == 1:
                description = f'Vence amanhã | {expires_at}'
            else:
                description = f'Vence em {days_remaining} dias | {expires_at}'
            options.append(discord.SelectOption(label=house_name[:100], value=str(rental_id), description=description[:100]))

        super().__init__(
            placeholder='Selecione a casa para simular o aviso',
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        rental_id = int(self.values[0])
        selected = next((row for row in self.rows if int(row.get('id', 0) or 0) == rental_id), None)
        if not selected:
            await self.cog.send_ephemeral_message(interaction, content='Casa não encontrada na lista atual.')
            return

        try:
            await interaction.response.defer(ephemeral=True)
            await self.cog.simulate_rental_notification(interaction, selected)
        except FinanceApiError as exc:
            await self.cog.send_ephemeral_message(interaction, content=f'Falha ao simular aviso: {exc}')


class RentalSimulationView(discord.ui.View):
    def __init__(self, cog: 'RentalCog', rows: list[dict[str, Any]]) -> None:
        super().__init__(timeout=120)
        self.add_item(RentalSimulationSelect(cog, rows))


class RentalDeleteSelect(discord.ui.Select):
    def __init__(self, cog: 'RentalCog', rows: list[dict[str, Any]]) -> None:
        self.cog = cog
        self.rows = rows
        options: list[discord.SelectOption] = []

        for row in rows[:25]:
            rental_id = int(row.get('id', 0) or 0)
            if rental_id <= 0:
                continue
            house_name = str(row.get('house_name', 'Casa')).strip() or 'Casa'
            expires_at = format_rental_date(row.get('expires_at')) or 'Sem vencimento'
            options.append(discord.SelectOption(label=house_name[:100], value=str(rental_id), description=f'Vence em: {expires_at}'[:100]))

        super().__init__(
            placeholder='Selecione a casa para remover',
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        rental_id = int(self.values[0])
        selected = next((row for row in self.rows if int(row.get('id', 0) or 0) == rental_id), None)
        if not selected:
            await self.cog.send_ephemeral_message(interaction, content='Casa não encontrada na lista atual.')
            return

        try:
            await interaction.response.defer(ephemeral=True)
            await self.cog.delete_rental(interaction, selected)
        except FinanceApiError as exc:
            await self.cog.send_ephemeral_message(interaction, content=f'Falha ao remover casa: {exc}')


class RentalDeleteView(discord.ui.View):
    def __init__(self, cog: 'RentalCog', rows: list[dict[str, Any]]) -> None:
        super().__init__(timeout=120)
        self.add_item(RentalDeleteSelect(cog, rows))


class RentalPanelView(discord.ui.View):
    def __init__(self, cog: 'RentalCog') -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label='Cadastrar Casa', style=discord.ButtonStyle.primary, emoji='🏠', custom_id='rentals:register')
    async def register_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.cog.user_can_manage(interaction):
            await self.cog.send_ephemeral_message(interaction, content='Apenas ADM/gerência pode cadastrar vencimentos de aluguel.')
            return
        await interaction.response.send_modal(RentalRegisterModal(self.cog))

    @discord.ui.button(label='Definir Destinatários', style=discord.ButtonStyle.secondary, emoji='👥', custom_id='rentals:recipients')
    async def recipients_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.cog.user_can_manage(interaction):
            await self.cog.send_ephemeral_message(interaction, content='Apenas ADM/gerência pode definir os destinatários.')
            return
        try:
            await self.cog.start_recipient_flow(interaction)
        except FinanceApiError as exc:
            await self.cog.send_ephemeral_message(interaction, content=f'Falha ao abrir destinatários: {exc}')

    @discord.ui.button(label='Status dos Vencimentos', style=discord.ButtonStyle.success, emoji='⏰', custom_id='rentals:status')
    async def status_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            await self.cog.show_status(interaction)
        except FinanceApiError as exc:
            await self.cog.send_ephemeral_message(interaction, content=f'Falha ao carregar vencimentos: {exc}')

    @discord.ui.button(label='Simular Aviso', style=discord.ButtonStyle.danger, emoji='🧪', custom_id='rentals:simulate')
    async def simulate_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.cog.user_can_manage(interaction):
            await self.cog.send_ephemeral_message(interaction, content='Apenas ADM/gerência pode simular aviso de vencimento.')
            return
        try:
            await self.cog.start_simulation_flow(interaction)
        except FinanceApiError as exc:
            await self.cog.send_ephemeral_message(interaction, content=f'Falha ao abrir simulação: {exc}')

    @discord.ui.button(label='Remover Casa', style=discord.ButtonStyle.danger, emoji='🗑️', custom_id='rentals:delete')
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.cog.user_can_manage(interaction):
            await self.cog.send_ephemeral_message(interaction, content='Apenas ADM/gerência pode remover casa cadastrada.')
            return
        try:
            await self.cog.start_delete_flow(interaction)
        except FinanceApiError as exc:
            await self.cog.send_ephemeral_message(interaction, content=f'Falha ao abrir remoção: {exc}')


class RentalCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.api = FinanceApiClient()
        self.manager_role_ids = role_ids_from_config('RENTAL_MANAGER_ROLE_IDS')

    async def cog_load(self) -> None:
        self.bot.add_view(RentalPanelView(self))
        if not self.notification_loop.is_running():
            self.notification_loop.start()

    async def cog_unload(self) -> None:
        if self.notification_loop.is_running():
            self.notification_loop.cancel()
        await self.api.close()

    def ephemeral_delete_after(self) -> float | None:
        value = float(getattr(config, 'RENTAL_EPHEMERAL_DELETE_AFTER_SECONDS', 120) or 0)
        return value if value > 0 else None

    def log_delete_after(self) -> float | None:
        value = float(getattr(config, 'RENTAL_LOG_DELETE_AFTER_SECONDS', 0) or 0)
        return value if value > 0 else None

    def reminder_days(self) -> list[int]:
        values = getattr(config, 'RENTAL_REMINDER_DAYS', [3, 1, 0]) or [3, 1, 0]
        parsed: list[int] = []
        for value in values:
            try:
                day = int(value)
            except (TypeError, ValueError):
                continue
            if day >= 0 and day not in parsed:
                parsed.append(day)
        return parsed or [3, 1, 0]

    async def delete_message_later(self, message: discord.Message | discord.InteractionMessage | discord.WebhookMessage | None, delay: float | None) -> None:
        if message is None or delay is None:
            return
        try:
            await asyncio.sleep(delay)
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def send_ephemeral_message(self, interaction: discord.Interaction, *, content: str | None = None, embed: discord.Embed | None = None, view: discord.ui.View | None = None) -> None:
        delete_after = self.ephemeral_delete_after()
        kwargs: dict[str, Any] = {'ephemeral': True}
        if content is not None:
            kwargs['content'] = content
        if embed is not None:
            kwargs['embed'] = embed
        if view is not None:
            kwargs['view'] = view

        if interaction.response.is_done():
            message = await interaction.followup.send(wait=True, **kwargs)
            if delete_after is not None:
                asyncio.create_task(self.delete_message_later(message, delete_after))
            return

        await interaction.response.send_message(**kwargs)
        if delete_after is not None:
            try:
                message = await interaction.original_response()
            except (discord.NotFound, discord.HTTPException):
                return
            asyncio.create_task(self.delete_message_later(message, delete_after))

    def user_can_manage(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            return False
        if member.guild_permissions.administrator:
            return True
        return has_any_role(member, self.manager_role_ids)

    async def log_destination(self, interaction: discord.Interaction | None = None) -> discord.abc.Messageable | None:
        channel_id = int(getattr(config, 'RENTAL_LOG_CHANNEL_ID', 0) or 0)
        if channel_id > 0:
            try:
                return self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                LOGGER.warning('Nao foi possivel acessar RENTAL_LOG_CHANNEL_ID=%s', channel_id)
                return interaction.channel if interaction is not None else None
        return interaction.channel if interaction is not None else None

    async def send_log(self, interaction: discord.Interaction | None, title: str, color: int, fields: list[tuple[str, str, bool]]) -> None:
        destination = await self.log_destination(interaction)
        if destination is None:
            return
        embed = discord.Embed(title=title, color=color)
        if interaction is not None:
            embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
        try:
            message = await destination.send(embed=embed)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            LOGGER.warning('Falha ao enviar log de aluguel no Discord.')
            return
        delete_after = self.log_delete_after()
        if delete_after is not None:
            asyncio.create_task(self.delete_message_later(message, delete_after))

    async def fetch_rentals_payload(self, include_expired: bool = True) -> dict[str, Any]:
        endpoint = 'rentals_list.php?include_expired=1' if include_expired else 'rentals_list.php?include_expired=0'
        data = await self.api.get(endpoint)
        return data if isinstance(data, dict) else {}

    async def fetch_rentals(self, include_expired: bool = True) -> list[dict[str, Any]]:
        data = await self.fetch_rentals_payload(include_expired=include_expired)
        rows = data.get('rows', [])
        if not isinstance(rows, list):
            raise FinanceApiError('Resposta inválida ao buscar os vencimentos de aluguel.')
        return [row for row in rows if isinstance(row, dict)]

    async def fetch_settings(self) -> dict[str, Any]:
        data = await self.fetch_rentals_payload(include_expired=True)
        settings = data.get('settings', {})
        return settings if isinstance(settings, dict) else {}

    async def register_rental(self, interaction: discord.Interaction, house_name: str, purchased_at: str, duration_days: int) -> None:
        response = await self.api.post('rental_register.php', {
            'house_name': house_name,
            'purchased_at': purchased_at,
            'duration_days': duration_days,
            'actor_name': interaction.user.display_name,
            'actor_discord_id': str(interaction.user.id),
        })
        row = response.get('rental', {})
        if not isinstance(row, dict):
            row = {}
        expires_at = format_rental_date(row.get('expires_at'))
        await self.send_ephemeral_message(interaction, content=f'Casa registrada: {house_name} | Vence em: {expires_at or "data indisponível"}')
        await self.send_log(interaction, 'Aluguel cadastrado/atualizado', 0x0D6EFD, [
            ('Casa', house_name, True),
            ('Compra', format_rental_date(purchased_at) or purchased_at, True),
            ('Dias', str(duration_days), True),
            ('Vencimento', expires_at or 'Indisponível', True),
        ])

    async def save_recipients(self, interaction: discord.Interaction, recipient_ids: list[str]) -> None:
        normalized = [value for value in recipient_ids if str(value).strip()]
        response = await self.api.post('rental_notification_settings_save.php', {
            'recipient_discord_ids': normalized,
            'actor_name': interaction.user.display_name,
            'actor_discord_id': str(interaction.user.id),
        })
        settings = response.get('settings', {})
        saved_ids = settings.get('recipient_discord_ids', []) if isinstance(settings, dict) else []
        mention_text = ', '.join(f'<@{user_id}>' for user_id in saved_ids) if saved_ids else 'nenhum destinatário'
        await self.send_ephemeral_message(interaction, content=f'Destinatários salvos: {mention_text}')
        await self.send_log(interaction, 'Destinatários atualizados', 0xF0AD4E, [('Usuários', mention_text, False)])

    async def start_delete_flow(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await self.fetch_rentals(include_expired=True)
        if not rows:
            await self.send_ephemeral_message(interaction, content='Nenhuma casa foi cadastrada para remoção.')
            return
        await self.send_ephemeral_message(
            interaction,
            content='Selecione a casa que deseja remover do cadastro de vencimentos.',
            view=RentalDeleteView(self, rows),
        )

    async def delete_rental(self, interaction: discord.Interaction, rental_row: dict[str, Any]) -> None:
        response = await self.api.post('rental_delete.php', {
            'rental_id': int(rental_row.get('id', 0) or 0),
        })
        row = response.get('rental', {})
        if not isinstance(row, dict):
            row = rental_row
        house_name = str(row.get('house_name', rental_row.get('house_name', 'Casa'))).strip() or 'Casa'
        expires_at = format_rental_date(row.get('expires_at')) or 'Indisponível'
        await self.send_ephemeral_message(interaction, content=f'Casa removida do cadastro: {house_name}')
        await self.send_log(interaction, 'Casa removida do cadastro', 0xDC3545, [
            ('Casa', house_name, True),
            ('Vencimento', expires_at, True),
        ])

    async def start_simulation_flow(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await self.fetch_rentals(include_expired=True)
        if not rows:
            await self.send_ephemeral_message(interaction, content='Nenhuma casa foi cadastrada para simulação.')
            return
        await self.send_ephemeral_message(
            interaction,
            content='Selecione a casa para disparar uma simulação do aviso de vencimento. A mensagem será enviada por DM aos destinatários configurados.',
            view=RentalSimulationView(self, rows),
        )

    async def simulate_rental_notification(self, interaction: discord.Interaction, rental_row: dict[str, Any]) -> None:
        settings = await self.fetch_settings()
        recipient_ids = settings.get('recipient_discord_ids', []) if isinstance(settings, dict) else []
        if not isinstance(recipient_ids, list) or not recipient_ids:
            raise FinanceApiError('Nenhum destinatário configurado para receber a simulação.')

        house_name = str(rental_row.get('house_name', 'Casa')).strip() or 'Casa'
        days_remaining = int(rental_row.get('days_remaining', 0) or 0)
        reminder_days = days_remaining if days_remaining >= 0 else 0
        message_text = build_rental_notification_message(rental_row, reminder_days, simulation=True)

        sent = 0
        failed = 0
        failed_ids: list[str] = []
        for recipient_id in recipient_ids:
            discord_user_id = str(recipient_id).strip()
            if discord_user_id == '':
                continue
            try:
                user = self.bot.get_user(int(discord_user_id))
                if user is None:
                    user = await self.bot.fetch_user(int(discord_user_id))
                await user.send(message_text)
                sent += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                failed += 1
                failed_ids.append(discord_user_id)

        result_lines = [f'Simulação enviada para a casa: {house_name}', f'Entregues: {sent}', f'Falhas: {failed}']
        if failed_ids:
            result_lines.append('Falharam: ' + ', '.join(f'<@{user_id}>' for user_id in failed_ids))

        await self.send_ephemeral_message(interaction, content='\n'.join(result_lines))
        await self.send_log(interaction, 'Simulação de aviso de aluguel', 0xDC3545, [
            ('Casa', house_name, True),
            ('Entregues', str(sent), True),
            ('Falhas', str(failed), True),
        ])

    async def start_recipient_flow(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        settings = await self.fetch_settings()
        selected_ids = settings.get('recipient_discord_ids', []) if isinstance(settings.get('recipient_discord_ids', []), list) else []
        view = RentalRecipientView(self, [str(value) for value in selected_ids])
        await self.send_ephemeral_message(interaction, embed=view.current_embed(), view=view)

    async def enqueue_notifications(self, source: str = 'auto') -> dict[str, Any]:
        data = await self.api.post('rental_notifications_enqueue.php', {
            'source': source,
            'reminder_days': self.reminder_days(),
        })
        result = data.get('result')
        if not isinstance(result, dict):
            raise FinanceApiError('Resposta inválida ao enfileirar avisos de aluguel.')
        return result

    async def claim_notification_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        data = await self.api.get(f'rental_notifications_claim.php?limit={int(limit)}')
        jobs = data.get('jobs', [])
        if not isinstance(jobs, list):
            raise FinanceApiError('Resposta inválida ao buscar a fila de avisos de aluguel.')
        return [job for job in jobs if isinstance(job, dict)]

    async def complete_notification_jobs(self, results: list[dict[str, Any]]) -> None:
        if not results:
            return
        await self.api.post('rental_notifications_complete.php', {'results': results})

    async def send_notification_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = int(job.get('id', 0) or 0)
        discord_user_id = str(job.get('recipient_discord_id', '') or '').strip()
        message_text = str(job.get('message_text', '') or '').strip()
        house_name = str(job.get('house_name', '') or 'Casa').strip() or 'Casa'
        if job_id <= 0 or discord_user_id == '' or message_text == '':
            return {'job_id': job_id, 'status': 'failed', 'error_message': 'Job inválido para envio.'}

        try:
            user = self.bot.get_user(int(discord_user_id))
            if user is None:
                user = await self.bot.fetch_user(int(discord_user_id))
            sent_message = await user.send(message_text)
            return {'job_id': job_id, 'status': 'sent', 'delivery_message_id': str(sent_message.id)}
        except (discord.NotFound, discord.Forbidden) as exc:
            await self.send_log(None, 'Falha ao enviar aviso de aluguel', 0xDC3545, [
                ('Casa', house_name, True),
                ('Discord ID', discord_user_id, True),
                ('Erro', str(exc), False),
            ])
            return {'job_id': job_id, 'status': 'failed', 'error_message': str(exc)}
        except discord.HTTPException as exc:
            error_message = f'HTTPException: {exc}'
            await self.send_log(None, 'Falha ao enviar aviso de aluguel', 0xDC3545, [
                ('Casa', house_name, True),
                ('Discord ID', discord_user_id, True),
                ('Erro', error_message, False),
            ])
            return {'job_id': job_id, 'status': 'failed', 'error_message': error_message}

    async def process_notification_queue(self, limit: int = 20) -> dict[str, int]:
        jobs = await self.claim_notification_jobs(limit=limit)
        if not jobs:
            return {'claimed': 0, 'sent': 0, 'failed': 0}

        results: list[dict[str, Any]] = []
        sent = 0
        failed = 0
        for job in jobs:
            result = await self.send_notification_job(job)
            results.append(result)
            if result.get('status') == 'sent':
                sent += 1
            else:
                failed += 1

        await self.complete_notification_jobs(results)
        return {'claimed': len(jobs), 'sent': sent, 'failed': failed}

    @tasks.loop(seconds=300)
    async def notification_loop(self) -> None:
        try:
            await self.enqueue_notifications(source='auto')
            await self.process_notification_queue(limit=20)
        except FinanceApiError as exc:
            LOGGER.warning('Loop de notificações de aluguel adiado: %s', exc)
        except Exception:
            LOGGER.exception('Falha no loop de notificações de aluguel.')

    @notification_loop.before_loop
    async def before_notification_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def show_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        payload = await self.fetch_rentals_payload(include_expired=True)
        rows = payload.get('rows', []) if isinstance(payload.get('rows'), list) else []
        settings = payload.get('settings', {}) if isinstance(payload.get('settings'), dict) else {}

        active_rows = [row for row in rows if isinstance(row, dict) and int(row.get('days_remaining', 0) or 0) >= 0]
        expired_rows = [row for row in rows if isinstance(row, dict) and int(row.get('days_remaining', 0) or 0) < 0]
        recipient_ids = settings.get('recipient_discord_ids', []) if isinstance(settings.get('recipient_discord_ids', []), list) else []

        lines = [
            f'🏠 Ativos: {len(active_rows)}',
            f'⚠️ Expirados: {len(expired_rows)}',
            f'👥 Destinatários: {len(recipient_ids)}',
            '',
        ]

        if active_rows:
            lines.append('Próximos vencimentos:')
            for row in active_rows[:10]:
                house_name = str(row.get('house_name', 'Casa')).strip() or 'Casa'
                expires_at = format_rental_date(row.get('expires_at'))
                days_remaining = int(row.get('days_remaining', 0) or 0)
                if days_remaining == 0:
                    status = 'vence hoje'
                elif days_remaining == 1:
                    status = 'vence amanhã'
                else:
                    status = f'vence em {days_remaining} dias'
                lines.append(f'- {house_name} | {expires_at} | {status}')
            lines.append('')

        if expired_rows:
            lines.append('Já vencidos:')
            for row in expired_rows[:5]:
                house_name = str(row.get('house_name', 'Casa')).strip() or 'Casa'
                expires_at = format_rental_date(row.get('expires_at'))
                lines.append(f'- {house_name} | venceu em {expires_at}')
            lines.append('')

        if recipient_ids:
            lines.append('Destinatários atuais:')
            for recipient_id in recipient_ids:
                lines.append(f'- <@{recipient_id}>')

        embed = discord.Embed(title='Vencimentos de Aluguel', description='\n'.join(lines).strip(), color=0xF0AD4E)
        await self.send_ephemeral_message(interaction, embed=embed)

    def panel_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title='🏠 VENCIMENTOS DE ALUGUEL',
            description='Painel para registrar casas compradas com diamantes e enviar avisos automáticos antes do vencimento.',
            color=0xF0AD4E,
        )
        embed.set_footer(text='Aluguéis por diamantes | ADM/GERÊNCIA')
        return embed

    def panel_view(self) -> discord.ui.View:
        return RentalPanelView(self)

    async def send_panel(self, destination: discord.abc.Messageable) -> None:
        await destination.send(embed=self.panel_embed(), view=self.panel_view())

    async def publish_panel(self, interaction: discord.Interaction, configured_channel_id: int) -> str:
        fallback_channel = interaction.channel
        if configured_channel_id <= 0:
            if fallback_channel is None:
                raise FinanceApiError('Canal atual indisponível para enviar o painel de aluguel.')
            await self.send_panel(fallback_channel)
            return 'current'

        try:
            channel = self.bot.get_channel(configured_channel_id) or await self.bot.fetch_channel(configured_channel_id)
            await self.send_panel(channel)
            return 'configured'
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            if fallback_channel is None:
                raise FinanceApiError('O bot não conseguiu acessar o canal configurado do painel de aluguel.')
            await self.send_panel(fallback_channel)
            return 'fallback'

    @app_commands.command(name='painelaluguel', description='Envia o painel de vencimentos de aluguel.')
    async def painel_aluguel(self, interaction: discord.Interaction) -> None:
        if not self.user_can_manage(interaction):
            await self.send_ephemeral_message(interaction, content='Apenas ADM/gerência pode publicar o painel de aluguel.')
            return
        await interaction.response.defer(ephemeral=True)
        channel_id = int(getattr(config, 'RENTAL_PANEL_CHANNEL_ID', 0) or 0)
        try:
            destination = await self.publish_panel(interaction, channel_id)
        except FinanceApiError as exc:
            await self.send_ephemeral_message(interaction, content=f'Falha ao publicar painel de aluguel: {exc}')
            return
        if destination == 'configured':
            await self.send_ephemeral_message(interaction, content='Painel de aluguel enviado no canal configurado.')
            return
        if destination == 'fallback' and channel_id > 0:
            await self.send_ephemeral_message(interaction, content='O canal configurado não pôde ser acessado. Enviei o painel de aluguel neste canal.')
            return
        await self.send_ephemeral_message(interaction, content='Painel de aluguel enviado neste canal.')


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RentalCog(bot))