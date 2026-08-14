"""
Staff payouts plugin for Modmail.

This plugin follows Modmail's plugin contract:
    <repository-root>/payouts/payouts.py

Configure the values in CONFIG before loading the plugin.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import discord
from discord.ext import commands

from core import checks
from core.checks import PermissionLevel


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PayoutConfig:
    """Values that are specific to the server using this plugin."""

    # Set this to the server where payouts are managed.
    guild_id: int | None = None

    # Add every role whose members should receive the open/close DMs.
    target_role_ids: tuple[int, ...] = ()

    # Add the Discord ID of the person who reviews payout requests.
    # Multiple IDs are supported if more than one reviewer should receive requests.
    reviewer_user_ids: tuple[int, ...] = ()

    # This is displayed in applicant approval/denial DMs.
    head_administrator_name: str = "im_azv"

    # Rank text containing one of these values receives the extra question.
    in_game_admin_rank_keywords: tuple[str, ...] = (
        "in-game admin",
        "ingame admin",
        "in game admin",
    )


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
# Replace the empty values below with your server's IDs before installing.
CONFIG = PayoutConfig(
    guild_id=None,  # Example: 123456789012345678
    target_role_ids=(
         1484613327442284795,
         1457039931351367872,
         1490692440146051092,
    ),
    reviewer_user_ids=(
         1272561419061297184,
    ),
    head_administrator_name="Azv",
)


OPEN_FORM_CUSTOM_ID = "payouts:open-form"
AMOUNT_FORM_CUSTOM_ID = "payouts:amount-form"
APPROVE_CUSTOM_ID = "payouts:approve"
DENY_CUSTOM_ID = "payouts:deny"


def _new_application_id() -> str:
    """Create a short, human-readable ID for a payout application."""

    return uuid.uuid4().hex[:10].upper()


def _is_in_game_admin(rank: str) -> bool:
    rank_text = rank.casefold()
    return any(keyword.casefold() in rank_text for keyword in CONFIG.in_game_admin_rank_keywords)


def _application_id_from_message(message: discord.Message | None) -> str | None:
    """Read the application ID from the footer of a reviewer embed."""

    if message is None or not message.embeds:
        return None

    footer = message.embeds[0].footer.text or ""
    prefix = "Application ID: "
    if not footer.startswith(prefix):
        return None
    return footer.removeprefix(prefix).strip() or None


class PayoutOpenView(discord.ui.View):
    """Persistent view delivered to eligible members when payouts open."""

    def __init__(self, plugin: "Payouts") -> None:
        super().__init__(timeout=None)
        self.plugin = plugin

    @discord.ui.button(
        label="Fill out payout form",
        style=discord.ButtonStyle.primary,
        custom_id=OPEN_FORM_CUSTOM_ID,
    )
    async def open_form(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PayoutOpenView],
    ) -> None:
        if not self.plugin.payouts_open:
            await interaction.response.send_message(
                "Staff payouts are currently closed.",
                ephemeral=True,
            )
            return

        if await self.plugin.has_active_application(interaction.user.id):
            await interaction.response.send_message(
                "You already have a payout application being reviewed.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(PayoutDetailsModal(self.plugin))


class PayoutAmountView(discord.ui.View):
    """Persistent view for the conditional In-Game Admin question."""

    def __init__(self, plugin: "Payouts") -> None:
        super().__init__(timeout=None)
        self.plugin = plugin

    @discord.ui.button(
        label="Continue application",
        style=discord.ButtonStyle.primary,
        custom_id=AMOUNT_FORM_CUSTOM_ID,
    )
    async def open_amount_form(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PayoutAmountView],
    ) -> None:
        if not self.plugin.payouts_open:
            await interaction.response.send_message(
                "Staff payouts are currently closed.",
                ephemeral=True,
            )
            return

        application = await self.plugin.find_waiting_for_amount(interaction.user.id)
        if application is None:
            await interaction.response.send_message(
                "That payout form is no longer waiting for this question.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(PayoutAmountModal(self.plugin))


class PayoutReviewView(discord.ui.View):
    """Persistent reviewer controls shared by every reviewer message."""

    def __init__(self, plugin: "Payouts", *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.plugin = plugin
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = disabled

    @discord.ui.button(
        label="Approve payout",
        style=discord.ButtonStyle.success,
        custom_id=APPROVE_CUSTOM_ID,
    )
    async def approve(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PayoutReviewView],
    ) -> None:
        await self.plugin.handle_review_decision(interaction, approved=True)

    @discord.ui.button(
        label="Deny payout",
        style=discord.ButtonStyle.danger,
        custom_id=DENY_CUSTOM_ID,
    )
    async def deny(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PayoutReviewView],
    ) -> None:
        await self.plugin.handle_review_decision(interaction, approved=False)


class PayoutDetailsModal(discord.ui.Modal, title="Staff payout application"):
    """The first four payout questions."""

    roblox_username = discord.ui.TextInput(
        label="Roblox Username",
        placeholder="Example: XxLeonCakeWoofxX",
        max_length=100,
        required=True,
    )
    discord_username = discord.ui.TextInput(
        label="Discord Username",
        placeholder="Example: slayinglooty",
        max_length=100,
        required=True,
    )
    discord_id = discord.ui.TextInput(
        label="Discord ID",
        placeholder="Enable Developer Mode to copy it",
        max_length=30,
        required=True,
    )
    current_rank = discord.ui.TextInput(
        label="Current Rank in Server",
        placeholder="Example: Bot Developer, In-Game Admin, or another staff rank",
        max_length=150,
        required=True,
    )

    def __init__(self, plugin: "Payouts") -> None:
        super().__init__()
        self.plugin = plugin

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.plugin.receive_details(
            interaction,
            {
                "roblox_username": str(self.roblox_username.value).strip(),
                "discord_username": str(self.discord_username.value).strip(),
                "discord_id": str(self.discord_id.value).strip(),
                "current_rank": str(self.current_rank.value).strip(),
            },
        )


class PayoutAmountModal(discord.ui.Modal, title="In-Game Admin details"):
    """The additional question shown only to In-Game Admins."""

    amount_moderated = discord.ui.TextInput(
        label="Amount of people moderated",
        placeholder="Enter the number of people you moderated",
        max_length=30,
        required=True,
    )

    def __init__(self, plugin: "Payouts") -> None:
        super().__init__()
        self.plugin = plugin

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.plugin.receive_amount(
            interaction,
            str(self.amount_moderated.value).strip(),
        )


class Payouts(commands.Cog):
    """Open, collect, and review staff payout applications."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = self.bot.plugin_db.get_partition(self)
        self.payouts_open = False

    async def cog_load(self) -> None:
        """Restore the open/closed state when Modmail loads the plugin."""

        state = await self.db.find_one({"_id": "state"})
        self.payouts_open = bool(state and state.get("payouts_open", False))

    def _configuration_errors(self) -> list[str]:
        errors: list[str] = []
        if not CONFIG.target_role_ids:
            errors.append("set at least one target role ID")
        if not CONFIG.reviewer_user_ids:
            errors.append("set at least one reviewer user ID")
        return errors

    async def _set_payout_state(self, is_open: bool) -> None:
        self.payouts_open = is_open
        await self.db.find_one_and_update(
            {"_id": "state"},
            {"$set": {"payouts_open": is_open}},
            upsert=True,
        )

    async def _get_guild(self, fallback_guild: discord.Guild | None = None) -> discord.Guild | None:
        guild_id = CONFIG.guild_id or (fallback_guild.id if fallback_guild else None)
        if guild_id is None:
            return None

        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            return guild

        try:
            return await self.bot.fetch_guild(guild_id)
        except discord.HTTPException:
            logger.exception("Unable to fetch configured payout guild %s", guild_id)
            return None

    async def _get_target_members(
        self,
        fallback_guild: discord.Guild | None = None,
    ) -> list[discord.Member]:
        guild = await self._get_guild(fallback_guild)
        if guild is None:
            return []

        # Role membership requires the members intent and a populated member cache.
        try:
            if not guild.chunked:
                await guild.chunk(cache=True)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Unable to populate members for payout role DMs. "
                "Enable the Server Members Intent for the Modmail bot."
            )

        members = guild.members
        if not members:
            try:
                members = [member async for member in guild.fetch_members(limit=None)]
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Unable to fetch members for payout role DMs.")
                return []

        target_role_ids = set(CONFIG.target_role_ids)
        return [
            member
            for member in members
            if not member.bot and any(role.id in target_role_ids for role in member.roles)
        ]

    async def _send_open_dm(self, member: discord.Member) -> bool:
        embed = discord.Embed(
            title="Staff Payouts Are Open",
            description=(
                "Staff payouts are now open. Click the button below to fill out "
                "your payout application."
            ),
            color=discord.Color.green(),
        )
        try:
            await member.send(embed=embed, view=PayoutOpenView(self))
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Could not DM payout opening notice to member %s.", member.id)
            return False

    async def _send_close_dm(self, member: discord.Member) -> bool:
        embed = discord.Embed(
            title="Staff Payouts Are Closed",
            description=(
                "Staff payouts have been closed. The administration team is no longer "
                "accepting payout applications."
            ),
            color=discord.Color.red(),
        )
        try:
            await member.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Could not DM payout closing notice to member %s.", member.id)
            return False

    @checks.has_permissions(PermissionLevel.OWNER)
    @commands.command(name="payoutsopen")
    async def payouts_open_command(self, ctx: commands.Context[Any]) -> None:
        """Open payouts and DM every member with a configured target role."""

        errors = self._configuration_errors()
        if errors:
            await ctx.send(f"Payouts plugin is not configured: {', '.join(errors)}.")
            return

        await self._set_payout_state(True)
        members = await self._get_target_members(ctx.guild)
        sent = sum([await self._send_open_dm(member) for member in members])
        await ctx.send(
            f"Staff payouts are now open. Sent {sent} opening DM(s) to eligible members."
        )

    @checks.has_permissions(PermissionLevel.OWNER)
    @commands.command(name="payoutsclose")
    async def payouts_close_command(self, ctx: commands.Context[Any]) -> None:
        """Close payouts and notify every member with a configured target role."""

        errors = self._configuration_errors()
        if errors:
            await ctx.send(f"Payouts plugin is not configured: {', '.join(errors)}.")
            return

        await self._set_payout_state(False)
        members = await self._get_target_members(ctx.guild)
        sent = sum([await self._send_close_dm(member) for member in members])
        await ctx.send(
            f"Staff payouts are now closed. Sent {sent} closing DM(s) to eligible members."
        )

    @checks.has_permissions(PermissionLevel.OWNER)
    @commands.command(name="payoutapproved")
    async def payout_approved_command(self, ctx: commands.Context[Any]) -> None:
        """Display every staff member whose payout application was approved."""

        applications = [
            application
            async for application in self.db.find({"status": "approved"})
        ]
        applications.sort(
            key=lambda application: (
                str(application.get("discord_username", "")).casefold(),
                str(application.get("_id", "")),
            )
        )

        if not applications:
            await ctx.send("No staff payout applications have been approved yet.")
            return

        lines = [
            f"**{index}. {application.get('discord_username', 'Unknown Discord user')}** "
            f"({application.get('roblox_username', 'Unknown Roblox user')})\n"
            f"Discord: <@{application.get('applicant_id', application.get('discord_id', '0'))}> "
            f"(`{application.get('discord_id', 'unknown')}`)\n"
            f"Rank: {application.get('current_rank', 'Unknown')}"
            for index, application in enumerate(applications, start=1)
        ]

        header = f"**Approved staff payouts ({len(applications)})**"
        chunks: list[str] = []
        current = header
        for line in lines:
            if len(current) + len(line) + 2 > 1900:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n\n{line}"
        chunks.append(current)

        for chunk in chunks:
            await ctx.send(chunk)

    async def has_active_application(self, user_id: int) -> bool:
        for status in ("awaiting_amount", "pending"):
            if await self.db.find_one(
                {"applicant_id": str(user_id), "status": status}
            ):
                return True
        return False

    async def find_waiting_for_amount(self, user_id: int) -> dict[str, Any] | None:
        return await self.db.find_one(
            {"applicant_id": str(user_id), "status": "awaiting_amount"}
        )

    async def receive_details(
        self,
        interaction: discord.Interaction,
        details: dict[str, str],
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not self.payouts_open:
            await interaction.followup.send(
                "Staff payouts are currently closed.",
                ephemeral=True,
            )
            return

        if await self.has_active_application(interaction.user.id):
            await interaction.followup.send(
                "You already have a payout application being reviewed.",
                ephemeral=True,
            )
            return

        application_id = _new_application_id()
        application: dict[str, Any] = {
            "_id": application_id,
            "applicant_id": str(interaction.user.id),
            "applicant_tag": str(interaction.user),
            "status": "pending",
            "amount_moderated": None,
            **details,
        }

        if _is_in_game_admin(details["current_rank"]):
            application["status"] = "awaiting_amount"
            await self.db.insert_one(application)
            await interaction.followup.send(
                "Because you selected an In-Game Admin rank, please answer the "
                "additional question below.",
                view=PayoutAmountView(self),
                ephemeral=True,
            )
            return

        await self.db.insert_one(application)
        reviewer_count = await self.notify_reviewers(application)
        await interaction.followup.send(
            "Your payout application has been submitted for review."
            if reviewer_count
            else (
                "Your payout application was saved, but no configured reviewer "
                "could be notified."
            ),
            ephemeral=True,
        )

    async def receive_amount(
        self,
        interaction: discord.Interaction,
        amount_moderated: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not self.payouts_open:
            await interaction.followup.send(
                "Staff payouts are currently closed.",
                ephemeral=True,
            )
            return

        application = await self.find_waiting_for_amount(interaction.user.id)
        if application is None:
            await interaction.followup.send(
                "That payout form is no longer waiting for an amount.",
                ephemeral=True,
            )
            return

        await self.db.find_one_and_update(
            {"_id": application["_id"], "status": "awaiting_amount"},
            {
                "$set": {
                    "status": "pending",
                    "amount_moderated": amount_moderated,
                }
            },
        )
        application["status"] = "pending"
        application["amount_moderated"] = amount_moderated
        reviewer_count = await self.notify_reviewers(application)
        await interaction.followup.send(
            "Your payout application has been submitted for review."
            if reviewer_count
            else (
                "Your payout application was saved, but no configured reviewer "
                "could be notified."
            ),
            ephemeral=True,
        )

    def _review_embed(self, application: dict[str, Any]) -> discord.Embed:
        embed = discord.Embed(
            title="New Staff Payout Request",
            description="Use the buttons below to approve or deny this request.",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Roblox Username",
            value=application["roblox_username"],
            inline=True,
        )
        embed.add_field(
            name="Discord Username",
            value=application["discord_username"],
            inline=True,
        )
        embed.add_field(
            name="Discord ID",
            value=application["discord_id"],
            inline=True,
        )
        embed.add_field(
            name="Current Rank in Server",
            value=application["current_rank"],
            inline=False,
        )
        if application.get("amount_moderated") is not None:
            embed.add_field(
                name="Amount of people moderated",
                value=str(application["amount_moderated"]),
                inline=False,
            )
        embed.add_field(
            name="Applicant",
            value=f"<@{application['applicant_id']}> ({application['applicant_tag']})",
            inline=False,
        )
        embed.set_footer(text=f"Application ID: {application['_id']}")
        return embed

    async def notify_reviewers(self, application: dict[str, Any]) -> int:
        sent = 0
        for user_id in CONFIG.reviewer_user_ids:
            try:
                reviewer = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                await reviewer.send(
                    embed=self._review_embed(application),
                    view=PayoutReviewView(self),
                )
                sent += 1
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Could not DM payout reviewer %s.", user_id)
        return sent

    async def handle_review_decision(
        self,
        interaction: discord.Interaction,
        *,
        approved: bool,
    ) -> None:
        if interaction.user.id not in CONFIG.reviewer_user_ids:
            await interaction.response.send_message(
                "You are not configured to review payout applications.",
                ephemeral=True,
            )
            return

        application_id = _application_id_from_message(interaction.message)
        if application_id is None:
            await interaction.response.send_message(
                "This review message is missing its application ID.",
                ephemeral=True,
            )
            return

        application = await self.db.find_one({"_id": application_id})
        if application is None:
            await interaction.response.send_message(
                "That payout application could not be found.",
                ephemeral=True,
            )
            return

        if application.get("status") != "pending":
            await interaction.response.send_message(
                f"This application has already been {application.get('status', 'processed')}.",
                ephemeral=True,
            )
            return

        status = "approved" if approved else "denied"
        await self.db.find_one_and_update(
            {"_id": application_id, "status": "pending"},
            {
                "$set": {
                    "status": status,
                    "reviewer_id": str(interaction.user.id),
                    "reviewed_at": discord.utils.utcnow().isoformat(),
                }
            },
        )

        await interaction.response.defer()
        applicant_id = int(application["applicant_id"])
        applicant = self.bot.get_user(applicant_id)
        if applicant is None:
            try:
                applicant = await self.bot.fetch_user(applicant_id)
            except (discord.NotFound, discord.HTTPException):
                applicant = None

        applicant_dm_sent = False
        if applicant is not None:
            if approved:
                message = (
                    "Your staff payout application has been approved. "
                    f"Please wait for a Head Administrator to DM you. "
                    f"Head Administrator: {CONFIG.head_administrator_name}"
                )
            else:
                message = (
                    "Your staff payout application has been denied. "
                    f"If there is a problem, DM {CONFIG.head_administrator_name}, "
                    "Head Administrator."
                )
            try:
                await applicant.send(message)
                applicant_dm_sent = True
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Could not DM payout decision to applicant %s.", applicant_id)

        decision_embed = self._review_embed(application)
        decision_embed.title = (
            "Staff Payout Request Approved" if approved else "Staff Payout Request Denied"
        )
        decision_embed.description = (
            f"Decision by {interaction.user}."
            + ("" if applicant_dm_sent else " The applicant could not be DM'd.")
        )
        decision_embed.color = discord.Color.green() if approved else discord.Color.red()
        await interaction.message.edit(
            embed=decision_embed,
            view=PayoutReviewView(self, disabled=True),
        )
        await interaction.followup.send(
            f"Payout application {application_id} has been {status}.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    plugin = Payouts(bot)
    await bot.add_cog(plugin)

    # All custom IDs are fixed, so these views continue working after a restart.
    bot.add_view(PayoutOpenView(plugin))
    bot.add_view(PayoutAmountView(plugin))
    bot.add_view(PayoutReviewView(plugin))
