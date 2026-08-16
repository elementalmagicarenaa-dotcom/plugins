"""
Event request plugin for Modmail.

This plugin follows Modmail's plugin contract:
    <repository-root>/event_requests/event_requests.py

Configure the reviewer user IDs and approved-events channel ID in CONFIG before
loading the plugin.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import discord
from discord.ext import commands


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventRequestConfig:
    """Values that are specific to the server using this plugin."""

    # Only members with this role can create event requests.
    event_host_role_id: int | None = None

    # Add Azv's and Humanity's Discord user IDs in this order.
    reviewer_user_ids: tuple[int, ...] = ()

    # Approved event announcements are posted in this channel.
    approved_events_channel_id: int | None = None


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
# Replace the empty values below with the correct IDs before installing.
CONFIG = EventRequestConfig(
    event_host_role_id=1463522255785955429,  # Replace with the Event Host role ID
    reviewer_user_ids=(
        1272561419061297184,  # Azv
        1268583625868447927,
    ),
    approved_events_channel_id=1538583496988299304,
)


OPEN_FORM_CUSTOM_ID = "event-requests:open-form"
APPROVE_CUSTOM_ID = "event-requests:approve"
DENY_CUSTOM_ID = "event-requests:deny"


def _new_request_id() -> str:
    """Create a short, human-readable ID for an event request."""

    return f"EVT-{uuid.uuid4().hex[:8].upper()}"


def _request_id_from_message(message: discord.Message | None) -> str | None:
    """Read the request ID from the footer of a reviewer embed."""

    if message is None or not message.embeds:
        return None

    footer = message.embeds[0].footer.text or ""
    prefix = "Event Request ID: "
    if not footer.startswith(prefix):
        return None
    return footer.removeprefix(prefix).strip() or None


def _safe_text(value: Any, fallback: str = "Not provided") -> str:
    """Prevent user-submitted text from creating accidental mentions."""

    text = str(value).strip() if value is not None else ""
    return discord.utils.escape_mentions(text or fallback)


class EventRequestOpenView(discord.ui.View):
    """Button shown by .eventrequest to open the request form."""

    def __init__(self, plugin: "EventRequests") -> None:
        super().__init__(timeout=None)
        self.plugin = plugin

    @discord.ui.button(
        label="Fill out event request",
        style=discord.ButtonStyle.primary,
        custom_id=OPEN_FORM_CUSTOM_ID,
    )
    async def open_form(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventRequestOpenView],
    ) -> None:
        if not self.plugin.member_has_event_host_role(interaction.user):
            await interaction.response.send_message(
                "Only members with the Event Host role can submit event requests.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(EventRequestForm(self.plugin))


class EventReviewView(discord.ui.View):
    """Persistent reviewer controls shared by every reviewer message."""

    def __init__(
        self,
        plugin: "EventRequests",
        *,
        disabled: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.plugin = plugin
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = disabled

    @discord.ui.button(
        label="Approve event",
        style=discord.ButtonStyle.success,
        custom_id=APPROVE_CUSTOM_ID,
    )
    async def approve(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventReviewView],
    ) -> None:
        await self.plugin.handle_review_decision(interaction, approved=True)

    @discord.ui.button(
        label="Decline event",
        style=discord.ButtonStyle.danger,
        custom_id=DENY_CUSTOM_ID,
    )
    async def deny(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventReviewView],
    ) -> None:
        request_id = _request_id_from_message(interaction.message)
        if request_id is None:
            await interaction.response.send_message(
                "This review message is missing its event request ID.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            EventDenialModal(self.plugin, request_id)
        )


class EventRequestForm(discord.ui.Modal, title="Event request"):
    """The event details submitted by a host."""

    event_type = discord.ui.TextInput(
        label="Type of event",
        placeholder="Session, Game Event, Quiz, or another event type",
        max_length=100,
        required=True,
    )
    prizes = discord.ui.TextInput(
        label="Prizes",
        placeholder="1st Place: ... | 2nd Place: ... | 3rd Place: ...",
        max_length=500,
        required=True,
    )
    description = discord.ui.TextInput(
        label="Event description",
        placeholder="Explain what the event is and how participants will take part",
        max_length=1500,
        required=True,
        style=discord.TextStyle.paragraph,
    )
    proposed_time = discord.ui.TextInput(
        label="Preferred date/time and timezone",
        placeholder="Example: 24 Aug 2026, 8:00 PM MYT (UTC+8)",
        max_length=200,
        required=True,
    )
    additional_details = discord.ui.TextInput(
        label="Additional event details",
        placeholder="Duration, participant limit, materials, rules, or other notes",
        max_length=1000,
        required=False,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, plugin: "EventRequests") -> None:
        super().__init__()
        self.plugin = plugin

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.plugin.receive_request(
            interaction,
            {
                "event_type": str(self.event_type.value).strip(),
                "prizes": str(self.prizes.value).strip(),
                "description": str(self.description.value).strip(),
                "proposed_time": str(self.proposed_time.value).strip(),
                "additional_details": str(self.additional_details.value).strip(),
            },
        )


class EventDenialModal(discord.ui.Modal, title="Decline event request"):
    """Collect the reason shown to the requester when an event is declined."""

    reason = discord.ui.TextInput(
        label="Reason for declining",
        placeholder="Explain why this event request was declined",
        max_length=1000,
        required=True,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, plugin: "EventRequests", request_id: str) -> None:
        super().__init__()
        self.plugin = plugin
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.plugin.handle_review_decision(
            interaction,
            approved=False,
            request_id=self.request_id,
            denial_reason=str(self.reason.value).strip(),
        )


class EventRequests(commands.Cog):
    """Collect, review, and publish event requests."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = self.bot.plugin_db.get_partition(self)

    async def cog_load(self) -> None:
        """Register persistent buttons when Modmail loads the plugin."""

        return None

    def _configuration_errors(self) -> list[str]:
        errors: list[str] = []
        if CONFIG.event_host_role_id is None:
            errors.append("set the Event Host role ID")
        if len(set(CONFIG.reviewer_user_ids)) < 2:
            errors.append("set both the Azv and Humanity Discord user IDs")
        if CONFIG.approved_events_channel_id is None:
            errors.append("set the approved events channel ID")
        return errors

    def member_has_event_host_role(self, user: discord.abc.User) -> bool:
        """Check whether a server member has the configured Event Host role."""

        if CONFIG.event_host_role_id is None or not isinstance(user, discord.Member):
            return False
        return any(role.id == CONFIG.event_host_role_id for role in user.roles)

    def _request_embed(
        self,
        request: dict[str, Any],
        *,
        title: str = "New Event Request",
        description: str = "Review the proposed event and choose approve or decline.",
        color: discord.Colour = discord.Colour.gold(),
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
        )
        embed.add_field(
            name="Event Type",
            value=_safe_text(request.get("event_type")),
            inline=True,
        )
        embed.add_field(
            name="Preferred Date/Time",
            value=_safe_text(request.get("proposed_time")),
            inline=True,
        )
        embed.add_field(
            name="Requested By",
            value=(
                f"<@{request.get('requester_id', '0')}> "
                f"({_safe_text(request.get('requester_tag'))})"
            ),
            inline=False,
        )
        embed.add_field(
            name="Prizes",
            value=_safe_text(request.get("prizes")),
            inline=False,
        )
        embed.add_field(
            name="Description",
            value=_safe_text(request.get("description")),
            inline=False,
        )
        embed.add_field(
            name="Additional Details",
            value=_safe_text(request.get("additional_details")),
            inline=False,
        )
        embed.set_footer(text=f"Event Request ID: {request['_id']}")
        return embed

    def _approved_event_embed(self, request: dict[str, Any]) -> discord.Embed:
        return self._request_embed(
            request,
            title="Approved Event",
            description=(
                "This event has been approved and is available for hosts to "
                "coordinate around."
            ),
            color=discord.Colour.green(),
        )

    @commands.command(name="eventrequest")
    async def event_request_command(self, ctx: commands.Context[Any]) -> None:
        """Show the button that opens the event request form."""

        errors = self._configuration_errors()
        if errors:
            await ctx.send(
                f"Event request plugin is not configured: {', '.join(errors)}."
            )
            return

        if not self.member_has_event_host_role(ctx.author):
            await ctx.send(
                "Only members with the Event Host role can submit event requests."
            )
            return

        embed = discord.Embed(
            title="Request an Event",
            description=(
                "Click the button below to submit an event request. "
                "Include your timezone with the preferred date and time."
            ),
            color=discord.Colour.blurple(),
        )
        await ctx.send(embed=embed, view=EventRequestOpenView(self))

    async def receive_request(
        self,
        interaction: discord.Interaction,
        details: dict[str, str],
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        errors = self._configuration_errors()
        if errors:
            await interaction.followup.send(
                f"Event request plugin is not configured: {', '.join(errors)}.",
                ephemeral=True,
            )
            return

        if not self.member_has_event_host_role(interaction.user):
            await interaction.followup.send(
                "Only members with the Event Host role can submit event requests.",
                ephemeral=True,
            )
            return

        request: dict[str, Any] = {
            "_id": _new_request_id(),
            "requester_id": str(interaction.user.id),
            "requester_tag": str(interaction.user),
            "status": "pending",
            "decision_by": None,
            "decision_by_tag": None,
            "decision_reason": None,
            "reviewed_at": None,
            "review_message_ids": {},
            **details,
        }
        await self.db.insert_one(request)

        reviewer_count = await self.notify_reviewers(request)
        await interaction.followup.send(
            (
                "Your event request has been sent to Azv and Humanity for review."
                if reviewer_count == len(CONFIG.reviewer_user_ids)
                else (
                    "Your event request was saved, but not every configured "
                    "reviewer could be notified."
                )
            ),
            ephemeral=True,
        )

    async def notify_reviewers(self, request: dict[str, Any]) -> int:
        sent = 0
        review_message_ids: dict[str, str] = {}

        for user_id in CONFIG.reviewer_user_ids:
            try:
                reviewer = self.bot.get_user(user_id) or await self.bot.fetch_user(
                    user_id
                )
                message = await reviewer.send(
                    embed=self._request_embed(request),
                    view=EventReviewView(self),
                )
                review_message_ids[str(user_id)] = str(message.id)
                sent += 1
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Could not DM event request reviewer %s.", user_id)

        await self.db.find_one_and_update(
            {"_id": request["_id"]},
            {"$set": {"review_message_ids": review_message_ids}},
        )
        request["review_message_ids"] = review_message_ids
        return sent

    async def _get_user(self, user_id: int) -> discord.User | None:
        user = self.bot.get_user(user_id)
        if user is not None:
            return user

        try:
            return await self.bot.fetch_user(user_id)
        except (discord.NotFound, discord.HTTPException):
            return None

    async def _notify_requester(
        self,
        request: dict[str, Any],
        *,
        approved: bool,
        denial_reason: str | None = None,
        approved_event_published: bool = False,
    ) -> bool:
        requester_id = int(request["requester_id"])
        requester = await self._get_user(requester_id)
        if requester is None:
            logger.warning("Could not find event request user %s.", requester_id)
            return False

        if approved:
            message = (
                "Your event request has been approved. You are able to host "
                "the event.\n"
                f"Event type: {_safe_text(request.get('event_type'))}\n"
                f"Preferred time: {_safe_text(request.get('proposed_time'))}"
            )
            if not approved_event_published:
                message += (
                    "\n\nThe request was approved, but the approved-events "
                    "channel could not be updated. Please contact an admin."
                )
        else:
            message = (
                "Your event request has been declined.\n"
                f"Reason: {_safe_text(denial_reason)}"
            )

        try:
            await requester.send(message)
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Could not DM event request user %s.", requester_id)
            return False

    async def _publish_approved_event(self, request: dict[str, Any]) -> bool:
        channel_id = CONFIG.approved_events_channel_id
        if channel_id is None:
            return False

        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            await channel.send(embed=self._approved_event_embed(request))
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Could not publish approved event request %s to channel %s.",
                request["_id"],
                channel_id,
            )
            return False

    async def _edit_reviewer_messages(
        self,
        request: dict[str, Any],
        *,
        decision_status: str,
        decision_by_tag: str,
        denial_reason: str | None = None,
    ) -> None:
        approved = decision_status == "approved"
        decision_embed = self._request_embed(
            request,
            title="Event Request Approved" if approved else "Event Request Declined",
            description=(
                f"Decision by {decision_by_tag}. "
                "No further action is required."
                + (
                    f"\nReason: {_safe_text(denial_reason)}"
                    if not approved
                    else ""
                )
            ),
            color=discord.Colour.green() if approved else discord.Colour.red(),
        )
        decision_view = EventReviewView(self, disabled=True)

        for user_id in CONFIG.reviewer_user_ids:
            message_id = request.get("review_message_ids", {}).get(str(user_id))
            if message_id is None:
                continue

            try:
                reviewer = await self._get_user(user_id)
                if reviewer is None:
                    continue
                dm_channel = await reviewer.create_dm()
                message = await dm_channel.fetch_message(int(message_id))
                await message.edit(embed=decision_embed, view=decision_view)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                logger.warning(
                    "Could not update event request %s review message for %s.",
                    request["_id"],
                    user_id,
                )

    async def handle_review_decision(
        self,
        interaction: discord.Interaction,
        *,
        approved: bool,
        request_id: str | None = None,
        denial_reason: str | None = None,
    ) -> None:
        if interaction.user.id not in CONFIG.reviewer_user_ids:
            await interaction.response.send_message(
                "You are not configured to review event requests.",
                ephemeral=True,
            )
            return

        request_id = request_id or _request_id_from_message(interaction.message)
        if request_id is None:
            await interaction.response.send_message(
                "This review message is missing its event request ID.",
                ephemeral=True,
            )
            return

        request = await self.db.find_one({"_id": request_id})
        if request is None:
            await interaction.response.send_message(
                "That event request could not be found.",
                ephemeral=True,
            )
            return

        if request.get("status") != "pending":
            await interaction.response.send_message(
                f"This event request has already been {request.get('status', 'processed')}.",
                ephemeral=True,
            )
            return

        if not approved and not denial_reason:
            await interaction.response.send_message(
                "A reason is required when declining an event request.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        status = "approved" if approved else "declined"
        decision_by_tag = str(interaction.user)
        await self.db.find_one_and_update(
            {"_id": request_id, "status": "pending"},
            {
                "$set": {
                    "status": status,
                    "decision_by": str(interaction.user.id),
                    "decision_by_tag": decision_by_tag,
                    "decision_reason": denial_reason if not approved else None,
                    "reviewed_at": discord.utils.utcnow().isoformat(),
                }
            },
        )
        request["status"] = status
        request["decision_by"] = str(interaction.user.id)
        request["decision_by_tag"] = decision_by_tag
        request["decision_reason"] = denial_reason if not approved else None

        approved_event_published = False
        if approved:
            approved_event_published = await self._publish_approved_event(request)

        await self._notify_requester(
            request,
            approved=approved,
            denial_reason=denial_reason,
            approved_event_published=approved_event_published,
        )
        await self._edit_reviewer_messages(
            request,
            decision_status=status,
            decision_by_tag=decision_by_tag,
            denial_reason=denial_reason,
        )
        await interaction.followup.send(
            f"Event request {request_id} has been {status}.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    plugin = EventRequests(bot)
    await bot.add_cog(plugin)

    # All custom IDs are fixed, so these views continue working after a restart.
    bot.add_view(EventRequestOpenView(plugin))
    bot.add_view(EventReviewView(plugin))
