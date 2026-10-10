"""Staff strike tracking for ModMail.

Install as a normal ModMail plugin Cog. Persistent records are stored in the
plugin's MongoDB partition; no direct access to ``bot.api`` is used.
"""

from __future__ import annotations

import asyncio
import logginga
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Mapping

import discord
from discord.ext import commands, tasks

from . import config

try:
    from .logic import (
        Rank,
        StrikeScope,
        build_ranks,
        can_manage_strike,
        has_named_rank,
        highest_rank,
    )
except ModuleNotFoundError as exc:
    # Some ModMail plugin deployments load only the extension and config files.
    # Keep the Cog loadable when the optional helper module was not included.
    if not exc.name or not exc.name.endswith(".logic"):
        raise

    StrikeScope = Literal["staff", "game_admin"]

    @dataclass(frozen=True)
    class Rank:
        name: str
        role_id: int
        level: int

    def build_ranks(
        raw_ranks: Iterable[Mapping[str, object]],
    ) -> tuple[Rank, ...]:
        ranks: list[Rank] = []
        seen_names: set[str] = set()
        seen_role_ids: set[int] = set()
        for level, raw_rank in enumerate(raw_ranks):
            name = str(raw_rank.get("name", "")).strip()
            try:
                role_id = int(raw_rank["role_id"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"STAFF_RANKS entry {level + 1} needs an integer role_id"
                ) from error
            if not name:
                raise ValueError(f"STAFF_RANKS entry {level + 1} needs a name")
            if role_id <= 0:
                raise ValueError(f"STAFF_RANKS role_id for {name!r} must be positive")
            if name.casefold() in seen_names:
                raise ValueError(f"Duplicate staff rank name: {name}")
            if role_id in seen_role_ids:
                raise ValueError(f"Duplicate staff role ID: {role_id}")
            seen_names.add(name.casefold())
            seen_role_ids.add(role_id)
            ranks.append(Rank(name=name, role_id=role_id, level=level))
        return tuple(ranks)

    def highest_rank(role_ids: Iterable[int], ranks: Iterable[Rank]) -> Rank | None:
        role_id_set = set(role_ids)
        return next(
            (rank for rank in reversed(tuple(ranks)) if rank.role_id in role_id_set),
            None,
        )

    def has_named_rank(
        role_ids: Iterable[int], ranks: Iterable[Rank], names: Iterable[str]
    ) -> bool:
        role_id_set = set(role_ids)
        name_set = {name.casefold() for name in names}
        return any(
            rank.role_id in role_id_set and rank.name.casefold() in name_set
            for rank in ranks
        )

    def can_manage_strike(
        issuer_role_ids: Iterable[int],
        target_role_ids: Iterable[int],
        ranks: Iterable[Rank],
        authority_rank_name: str,
        game_admin_manager_role_id: int,
        scope: StrikeScope,
    ) -> bool:
        issuer_ids = set(issuer_role_ids)
        if scope == "game_admin":
            return (
                game_admin_manager_role_id > 0
                and game_admin_manager_role_id in issuer_ids
            )
        rank_list = tuple(ranks)
        issuer_rank = highest_rank(issuer_ids, rank_list)
        target_rank = highest_rank(target_role_ids, rank_list)
        authority_rank = next(
            (
                rank
                for rank in rank_list
                if rank.name.casefold() == authority_rank_name.casefold()
            ),
            None,
        )
        if issuer_rank is None or target_rank is None or authority_rank is None:
            return False
        return (
            issuer_rank.level >= authority_rank.level
            and target_rank.level <= issuer_rank.level
        )

log = logging.getLogger(__name__)
UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso_timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat()


def acknowledgement_deadline(strike: dict[str, Any]) -> datetime | None:
    """Return a strike's stored acknowledgement deadline, if it has one."""
    return as_utc_datetime(strike.get("acknowledgement_deadline_at"))


def pending_acknowledgements(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        strike
        for strike in record.get("strikes", [])
        if strike.get("status") == "active"
        and strike.get("acknowledged_at") is None
        and acknowledgement_deadline(strike) is not None
    ]


class StrikeAcknowledgementView(discord.ui.View):
    """Persistent DM button that lets only the strike recipient acknowledge."""

    def __init__(
        self,
        cog: StaffStrikes,
        guild_id: int,
        member_id: int,
        strike_id: str,
        *,
        disabled: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.member_id = member_id
        self.strike_id = strike_id

        button = discord.ui.Button(
            label="Acknowledged" if disabled else "Acknowledge",
            style=discord.ButtonStyle.success,
            custom_id=f"staffstrike:ack:{guild_id}:{member_id}:{strike_id}",
            disabled=disabled,
        )
        button.callback = self.acknowledge
        self.add_item(button)

    async def acknowledge(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message(
                "Only the member who received this strike can acknowledge it.",
                ephemeral=True,
            )
            return

        strike = await self.cog._mark_strike_acknowledged(
            self.guild_id,
            self.member_id,
            self.strike_id,
        )
        if strike is None:
            await interaction.response.send_message(
                "This strike is no longer awaiting acknowledgement.",
                ephemeral=True,
            )
            return

        acknowledged_view = self.cog._acknowledgement_view(
            self.guild_id,
            self.member_id,
            self.strike_id,
            disabled=True,
        )
        self.cog._ack_views_by_message[interaction.message.id] = acknowledged_view
        await interaction.response.edit_message(
            content="Strike acknowledged.",
            view=acknowledged_view,
        )


class StaffStrikes(commands.Cog, name="StaffStrikes"):
    """Track staff strikes, acknowledgements, and role removal at the limit."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.ranks: tuple[Rank, ...] = build_ranks(config.STAFF_RANKS)
        self.rank_by_name = {rank.name.casefold(): rank for rank in self.ranks}
        self.staff_team_role_id = self._positive_id("STAFF_TEAM_ROLE_ID")
        self.notification_user_id = self._positive_id("NOTIFICATION_USER_ID")
        self.game_admin_role_id = self._positive_id("GAME_ADMIN_ROLE_ID")
        self.game_admin_manager_role_id = self._positive_id(
            "GAME_ADMIN_MANAGER_ROLE_ID"
        )
        if self.game_admin_manager_role_id == self.game_admin_role_id:
            raise ValueError(
                "GAME_ADMIN_MANAGER_ROLE_ID must be different from GAME_ADMIN_ROLE_ID"
            )
        self.max_strikes = int(config.MAX_STRIKES)
        self.acknowledgement_hours = int(config.ACKNOWLEDGEMENT_HOURS)
        self.acknowledgement_check_minutes = int(config.ACKNOWLEDGEMENT_CHECK_MINUTES)
        self.extra_staff_role_ids = {
            int(role_id) for role_id in config.EXTRA_STAFF_ROLE_IDS_TO_REMOVE
        }
        self.strike_authority_rank_name = str(config.STRIKE_AUTHORITY_RANK)
        self.all_strikes_authority_ranks = {
            str(name).casefold() for name in config.ALL_STRIKES_AUTHORITY_RANKS
        }

        if self.strike_authority_rank_name.casefold() not in self.rank_by_name:
            raise ValueError(
                "STRIKE_AUTHORITY_RANK must match a name in STAFF_RANKS"
            )
        if self.max_strikes < 1:
            raise ValueError("MAX_STRIKES must be at least 1")
        if self.acknowledgement_hours < 1:
            raise ValueError("ACKNOWLEDGEMENT_HOURS must be at least 1")
        if self.acknowledgement_check_minutes < 1:
            raise ValueError("ACKNOWLEDGEMENT_CHECK_MINUTES must be at least 1")

        # ModMail's plugin API gives each plugin an isolated Mongo collection.
        self.collection = bot.api.get_plugin_partition(self)
        self._locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._ack_views_by_message: dict[int, StrikeAcknowledgementView] = {}

    @staticmethod
    def _positive_id(setting_name: str) -> int:
        value = int(getattr(config, setting_name))
        if value <= 0:
            raise ValueError(f"{setting_name} must be a positive Discord ID")
        return value

    async def cog_load(self) -> None:
        self.acknowledgement_monitor.change_interval(
            minutes=self.acknowledgement_check_minutes
        )
        await self._register_pending_acknowledgement_views()
        self.acknowledgement_monitor.start()

    def cog_unload(self) -> None:
        self.acknowledgement_monitor.cancel()
        for message_id, view in self._ack_views_by_message.items():
            self.bot.remove_view(view, message_id=message_id)
        self._ack_views_by_message.clear()

    async def _register_pending_acknowledgement_views(self) -> None:
        async for record in self.collection.find({}):
            guild_id = int(record.get("guild_id", 0))
            member_id = int(record.get("member_id", 0))
            if not guild_id or not member_id:
                continue

            for strike in record.get("strikes", []):
                message_id = strike.get("ack_message_id")
                strike_id = str(strike.get("id", ""))
                if (
                    strike.get("status") != "active"
                    or strike.get("acknowledged_at") is not None
                    or not message_id
                    or not strike_id
                ):
                    continue

                view = self._acknowledgement_view(
                    guild_id,
                    member_id,
                    strike_id,
                )
                message_id = int(message_id)
                self.bot.add_view(view, message_id=message_id)
                self._ack_views_by_message[message_id] = view

    def _acknowledgement_view(
        self,
        guild_id: int,
        member_id: int,
        strike_id: str,
        *,
        disabled: bool = False,
    ) -> StrikeAcknowledgementView:
        return StrikeAcknowledgementView(
            self,
            guild_id,
            member_id,
            strike_id,
            disabled=disabled,
        )

    @tasks.loop(minutes=5)
    async def acknowledgement_monitor(self) -> None:
        async for stale_record in self.collection.find({}):
            guild_id = int(stale_record.get("guild_id", 0))
            member_id = int(stale_record.get("member_id", 0))
            if not guild_id or not member_id:
                continue

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue

            async with self._record_lock(guild_id, member_id):
                record = await self.get_record(guild_id, member_id)
                now = utc_now()
                for strike in pending_acknowledgements(record):
                    deadline = acknowledgement_deadline(strike)
                    if deadline is None or deadline > now:
                        continue

                    # Deliveries have independent markers. A channel-log failure
                    # must not cause a successful notification DM to be repeated.
                    if strike.get("missed_ack_notified_at") is None:
                        if await self.notify_missed_acknowledgement(guild, record, strike):
                            strike["missed_ack_notified_at"] = iso_timestamp(now)
                            await self.save_record(record)

                    if strike.get("missed_ack_log_sent_at") is None:
                        if await self.log_missed_acknowledgement(guild, record, strike):
                            strike["missed_ack_log_sent_at"] = iso_timestamp(now)
                            await self.save_record(record)

    @acknowledgement_monitor.before_loop
    async def before_acknowledgement_monitor(self) -> None:
        await self.bot.wait_until_ready()

    def _record_lock(self, guild_id: int, member_id: int) -> asyncio.Lock:
        return self._locks[(guild_id, member_id)]

    @staticmethod
    def _role_ids(member: discord.Member) -> set[int]:
        return {role.id for role in member.roles}

    def highest_rank(self, member: discord.Member) -> Rank | None:
        return highest_rank(self._role_ids(member), self.ranks)

    def has_staff_role(self, member: discord.Member) -> bool:
        role_ids = self._role_ids(member)
        return (
            self.staff_team_role_id in role_ids
            or self.game_admin_role_id in role_ids
            or any(rank.role_id in role_ids for rank in self.ranks)
        )

    def has_game_admin_role(self, member: discord.Member) -> bool:
        return self.game_admin_role_id in self._role_ids(member)

    def has_strike_authority(self, member: discord.Member) -> bool:
        rank = self.highest_rank(member)
        required = self.rank_by_name[self.strike_authority_rank_name.casefold()]
        return rank is not None and rank.level >= required.level

    def has_all_strikes_authority(self, member: discord.Member) -> bool:
        return has_named_rank(
            self._role_ids(member), self.ranks, self.all_strikes_authority_ranks
        )

    def can_manage_strike_target(
        self,
        issuer: discord.Member,
        target: discord.Member,
        scope: StrikeScope,
    ) -> bool:
        return can_manage_strike(
            issuer_role_ids=self._role_ids(issuer),
            target_role_ids=self._role_ids(target),
            ranks=self.ranks,
            authority_rank_name=self.strike_authority_rank_name,
            game_admin_manager_role_id=self.game_admin_manager_role_id,
            scope=scope,
        )

    @staticmethod
    def _record_id(guild_id: int, member_id: int) -> str:
        return f"{guild_id}:{member_id}"

    async def get_record(self, guild_id: int, member_id: int) -> dict[str, Any]:
        record = await self.collection.find_one(
            {"_id": self._record_id(guild_id, member_id)}
        )
        if record is None:
            return {
                "_id": self._record_id(guild_id, member_id),
                "guild_id": guild_id,
                "member_id": member_id,
                "strikes": [],
            }
        record.setdefault("strikes", [])
        return record

    async def save_record(self, record: dict[str, Any]) -> None:
        record["updated_at"] = iso_timestamp()
        await self.collection.replace_one(
            {"_id": record["_id"]}, record, upsert=True
        )

    @staticmethod
    def active_strikes(record: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            strike
            for strike in record.get("strikes", [])
            if strike.get("status") == "active"
        ]

    def _strike_roles_to_remove(self, member: discord.Member) -> list[discord.Role]:
        role_ids = {rank.role_id for rank in self.ranks}
        role_ids.update(
            {
                self.staff_team_role_id,
                self.game_admin_role_id,
                *self.extra_staff_role_ids,
            }
        )
        return [role for role in member.roles if role.id in role_ids]

    async def remove_staff_roles(
        self, member: discord.Member, record: dict[str, Any]
    ) -> str | None:
        """Remove configured staff roles after the strike limit is reached."""
        roles = self._strike_roles_to_remove(member)
        if not roles:
            record["roles_removed_at"] = record.get("roles_removed_at") or iso_timestamp()
            await self.save_record(record)
            return None

        try:
            await member.remove_roles(
                *roles,
                reason=f"Reached {self.max_strikes} active staff strikes",
            )
        except discord.HTTPException as exc:
            record["role_removal_error"] = str(exc)[:500]
            await self.save_record(record)
            return str(exc)

        record["roles_removed_at"] = iso_timestamp()
        record.pop("role_removal_error", None)
        await self.save_record(record)
        return None

    async def notify_missed_acknowledgement(
        self,
        guild: discord.Guild,
        record: dict[str, Any],
        strike: dict[str, Any],
    ) -> bool:
        user = self.bot.get_user(self.notification_user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(self.notification_user_id)
            except discord.HTTPException:
                log.warning(
                    "Could not fetch missed-ack notification user %s",
                    self.notification_user_id,
                )
                return False

        member_id = int(record["member_id"])
        member = guild.get_member(member_id)
        member_label = member.mention if member else f"<@{member_id}>"
        try:
            await user.send(
                f"Missed strike acknowledgement: {member_label} did not acknowledge "
                f"strike `{strike['id']}` before its deadline. Reason: "
                f"{strike.get('reason', 'No reason supplied')}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning(
                "Could not DM missed-ack notification user %s",
                self.notification_user_id,
            )
            return False
        return True

    async def log_missed_acknowledgement(
        self,
        guild: discord.Guild,
        record: dict[str, Any],
        strike: dict[str, Any],
    ) -> bool:
        channel_id = strike.get("issued_in_channel_id")
        if not channel_id:
            return False

        channel = guild.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except discord.HTTPException:
                log.warning("Could not fetch strike audit channel %s", channel_id)
                return False

        member_id = int(record["member_id"])
        try:
            await channel.send(
                f"Strike `{strike['id']}` for <@{member_id}> was not acknowledged "
                f"by its deadline.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning("Could not post missed-ack log to channel %s", channel_id)
            return False
        return True

    @staticmethod
    def _format_remaining(deadline: datetime | None) -> str:
        if deadline is None:
            return "not configured"
        seconds = int((deadline - utc_now()).total_seconds())
        if seconds <= 0:
            return "Expired"

        days, remainder = divmod(seconds, 24 * 60 * 60)
        hours, remainder = divmod(remainder, 60 * 60)
        minutes = remainder // 60
        if days:
            return f"{days}d {hours}h remaining" if hours else f"{days}d remaining"
        if hours:
            return f"{hours}h {minutes}m remaining" if minutes else f"{hours}h remaining"
        return f"{max(1, minutes)}m remaining"

    def _build_strike_embed(
        self,
        target: discord.Member,
        strike: dict[str, Any],
        active_count: int,
    ) -> discord.Embed:
        deadline = acknowledgement_deadline(strike)
        deadline_text = (
            discord.utils.format_dt(deadline, style="F") if deadline else "not configured"
        )
        embed = discord.Embed(
            title="Staff strike issued",
            description=(
                f"You have received strike **{active_count}/{self.max_strikes}** "
                f"in **{target.guild.name}**."
            ),
            color=discord.Color.red(),
        )
        embed.add_field(
            name="Issued by",
            value=f"<@{strike['issued_by']}>",
            inline=True,
        )
        embed.add_field(
            name="Duration",
            value=self._format_remaining(deadline),
            inline=True,
        )
        embed.add_field(
            name="Acknowledge by",
            value=deadline_text,
            inline=True,
        )
        embed.add_field(
            name="Reason",
            value=strike.get("reason", "No reason supplied"),
            inline=False,
        )
        embed.add_field(
            name="\u200b",
            value=(
                f"You have **{self.acknowledgement_hours} hours** to acknowledge "
                "this notice. Missing the deadline makes you ineligible for the "
                "next payout."
            ),
            inline=False,
        )
        embed.set_footer(text=f"Strike ID: {strike['id']}")
        return embed

    async def _send_strike_dm(
        self,
        target: discord.Member,
        strike: dict[str, Any],
        guild_id: int,
        active_count: int,
    ) -> discord.Message | None:
        view = self._acknowledgement_view(
            guild_id,
            target.id,
            str(strike["id"]),
        )
        try:
            message = await target.send(
                embed=self._build_strike_embed(target, strike, active_count),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            return None

        self._ack_views_by_message[message.id] = view
        return message

    @commands.command(name="strike")
    @commands.guild_only()
    async def issue_strike(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason supplied",
    ) -> None:
        """Issue a strike to a ranked staff member or Game Admin."""
        await self._issue_strike(ctx, member, reason, forced_scope=None)

    @commands.command(name="gameadminstrike")
    @commands.guild_only()
    async def issue_game_admin_strike(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason supplied",
    ) -> None:
        """Issue a Game Admin-scoped strike, including to a dual-role member."""
        await self._issue_strike(ctx, member, reason, forced_scope="game_admin")

    async def _issue_strike(
        self,
        ctx: commands.Context,
        member: discord.Member,
        reason: str,
        *,
        forced_scope: StrikeScope | None,
    ) -> None:
        issuer = ctx.author
        if not isinstance(issuer, discord.Member):
            await ctx.send("This command must be used in a server.")
            return
        if member.id == issuer.id:
            await ctx.send("You cannot issue a strike to yourself.")
            return
        if not self.has_staff_role(member):
            await ctx.send("Strikes can only be issued to configured staff members.")
            return

        target_rank = self.highest_rank(member)
        if forced_scope == "game_admin":
            if not self.has_game_admin_role(member):
                await ctx.send(
                    "A Game Admin-scoped strike requires the target to have the "
                    "configured Game Admin role."
                )
                return
            scope: StrikeScope = "game_admin"
        elif target_rank is not None:
            scope: StrikeScope = "staff"
        elif self.has_game_admin_role(member):
            scope = "game_admin"
        else:
            await ctx.send(
                "This member has a staff role but no configured rank or Game Admin role."
            )
            return

        if not self.can_manage_strike_target(issuer, member, scope):
            if scope == "game_admin":
                message = (
                    "Only holders of the configured Game Admin Manager role can "
                    "manage Game Admin strikes."
                )
            else:
                message = (
                    "You cannot manage this target's staff strikes. "
                    f"{self.strike_authority_rank_name} and higher ranks may manage "
                    "staff members at or below their own rank."
                )
            await ctx.send(message)
            return

        clean_reason = reason.strip()[:1000] or "No reason supplied"
        guild_id = ctx.guild.id
        async with self._record_lock(guild_id, member.id):
            record = await self.get_record(guild_id, member.id)
            now = utc_now()
            strike = {
                "id": uuid.uuid4().hex[:8],
                "scope": scope,
                "status": "active",
                "reason": clean_reason,
                "issued_at": iso_timestamp(now),
                "issued_by": issuer.id,
                "issued_in_channel_id": ctx.channel.id,
                "acknowledgement_deadline_at": iso_timestamp(
                    now + timedelta(hours=self.acknowledgement_hours)
                ),
                "acknowledged_at": None,
            }
            record.setdefault("strikes", []).append(strike)
            await self.save_record(record)

            active_count = len(self.active_strikes(record))
            role_removal_error = None
            if active_count >= self.max_strikes and record.get("roles_removed_at") is None:
                role_removal_error = await self.remove_staff_roles(member, record)

        dm_message = await self._send_strike_dm(
            member,
            strike,
            guild_id,
            active_count,
        )
        if dm_message is not None:
            async with self._record_lock(guild_id, member.id):
                latest_record = await self.get_record(guild_id, member.id)
                saved_strike = next(
                    (
                        item
                        for item in latest_record.get("strikes", [])
                        if item.get("id") == strike["id"]
                    ),
                    None,
                )
                if saved_strike is not None:
                    saved_strike["ack_channel_id"] = dm_message.channel.id
                    saved_strike["ack_message_id"] = dm_message.id
                    await self.save_record(latest_record)

        dm_sent = dm_message is not None
        response = (
            f"Issued strike `{strike['id']}` to {member.mention} "
            f"({active_count}/{self.max_strikes} active). "
            f"Scope: **{scope.replace('_', ' ')}**. Reason: {clean_reason}"
        )
        if active_count >= self.max_strikes and role_removal_error is None:
            response += " Configured staff roles were removed."
        elif role_removal_error:
            response += (
                " The strike was recorded, but Discord could not remove the staff "
                f"roles: {role_removal_error}"
            )
        if not dm_sent:
            response += " I could not DM the member; they may have DMs disabled."
        await ctx.send(
            response,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="strikes")
    @commands.guild_only()
    async def show_strikes(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show your own strikes, or a target's strikes if you can manage them."""
        issuer = ctx.author
        if not isinstance(issuer, discord.Member):
            await ctx.send("This command must be used in a server.")
            return
        target = member or issuer

        record = await self.get_record(ctx.guild.id, target.id)
        strikes = record.get("strikes", [])
        if target.id != issuer.id:
            visible_scopes = self._viewable_scopes(issuer, target)
            if not visible_scopes:
                await ctx.send("You cannot view this member's strike record.")
                return
            strikes = [
                strike
                for strike in strikes
                if strike.get("scope", "staff") in visible_scopes
            ]
        if not strikes:
            await ctx.send(f"{target.display_name} has no recorded strikes.")
            return

        lines = [
            f"**{target.display_name} — "
            f"{len(self.active_strikes(record))}/{self.max_strikes} active strikes**"
        ]
        for strike in sorted(strikes, key=lambda item: item.get("issued_at", "")):
            status = strike.get("status", "active")
            acknowledgement = (
                "acknowledged"
                if strike.get("acknowledged_at")
                else "acknowledgement pending"
            )
            lines.append(
                f"`{strike.get('id', '?')}` [{strike.get('scope', 'staff')}] "
                f"**{status}** — {strike.get('reason', 'No reason supplied')} "
                f"({acknowledgement})"
            )
        await self._send_lines(ctx, lines)

    @commands.command(name="allstrikes")
    @commands.guild_only()
    async def show_all_strikes(self, ctx: commands.Context) -> None:
        """List all active strikes; restricted to configured Admin ranks."""
        issuer = ctx.author
        if not isinstance(issuer, discord.Member) or not self.has_all_strikes_authority(
            issuer
        ):
            await ctx.send(
                "Only the configured Admin and Head Admin ranks may use allstrikes."
            )
            return

        rows: list[tuple[int, str, list[dict[str, Any]]]] = []
        async for record in self.collection.find({"guild_id": ctx.guild.id}):
            active = self.active_strikes(record)
            if active:
                rows.append(
                    (
                        int(record.get("member_id", 0)),
                        str(record.get("member_id", "Unknown")),
                        active,
                    )
                )

        if not rows:
            await ctx.send("There are no active strikes.")
            return

        lines = ["**Active staff strikes**"]
        for member_id, _, active in sorted(rows, key=lambda row: row[1]):
            member = ctx.guild.get_member(member_id)
            name = member.display_name if member else f"User {member_id}"
            for strike in active:
                lines.append(
                    f"**{name}** — `{strike.get('id', '?')}` "
                    f"[{strike.get('scope', 'staff')}] "
                    f"{strike.get('reason', 'No reason supplied')}"
                )
        await self._send_lines(ctx, lines)

    @commands.command(name="removestrike")
    @commands.guild_only()
    async def remove_strike(
        self,
        ctx: commands.Context,
        member: discord.Member,
        strike_id: str | None = None,
    ) -> None:
        """Remove an active strike, optionally selecting its short ID."""
        issuer = ctx.author
        if not isinstance(issuer, discord.Member):
            await ctx.send("This command must be used in a server.")
            return

        guild_id = ctx.guild.id
        async with self._record_lock(guild_id, member.id):
            record = await self.get_record(guild_id, member.id)
            active = self.active_strikes(record)
            if not active:
                await ctx.send(f"{member.display_name} has no active strikes.")
                return

            if strike_id:
                matches = [
                    strike
                    for strike in active
                    if str(strike.get("id", "")).casefold().startswith(
                        strike_id.casefold()
                    )
                ]
                if len(matches) != 1:
                    await ctx.send(
                        "That strike ID is missing or ambiguous. Use `strikes` to "
                        "copy the full ID."
                    )
                    return
                strike = matches[0]
            else:
                strike = max(active, key=lambda item: item.get("issued_at", ""))

            scope = strike.get("scope", "staff")
            if scope not in {"staff", "game_admin"}:
                await ctx.send("This strike has an unknown scope and cannot be removed.")
                return
            if not self.can_manage_strike_target(issuer, member, scope):
                await ctx.send("You cannot manage this target's strikes.")
                return

            strike["status"] = "removed"
            strike["removed_at"] = iso_timestamp()
            strike["removed_by"] = issuer.id
            await self.save_record(record)

        await ctx.send(
            f"Removed strike `{strike['id']}` from {member.mention}. "
            f"{len(self.active_strikes(record))}/{self.max_strikes} active remain.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="ackstrike")
    @commands.guild_only()
    async def acknowledge_strike(
        self, ctx: commands.Context, strike_id: str | None = None
    ) -> None:
        """Acknowledge your most recent pending strike or a specified strike."""
        member = ctx.author
        if not isinstance(member, discord.Member):
            await ctx.send("This command must be used in a server.")
            return

        guild_id = ctx.guild.id
        async with self._record_lock(guild_id, member.id):
            record = await self.get_record(guild_id, member.id)
            pending = [
                strike
                for strike in self.active_strikes(record)
                if strike.get("acknowledged_at") is None
            ]
            if strike_id:
                pending = [
                    strike
                    for strike in pending
                    if str(strike.get("id", "")).casefold().startswith(
                        strike_id.casefold()
                    )
                ]
            if not pending:
                await ctx.send("You have no matching strike awaiting acknowledgement.")
                return
            if len(pending) > 1:
                await ctx.send(
                    "You have multiple pending strikes. Include the strike ID from "
                    "your strike notice."
                )
                return

            selected_strike_id = str(pending[0]["id"])

        strike = await self._mark_strike_acknowledged(
            guild_id,
            member.id,
            selected_strike_id,
        )
        if strike is None:
            await ctx.send("That strike is no longer awaiting acknowledgement.")
            return

        await self._edit_acknowledgement_message(guild_id, member.id, strike)
        await ctx.send(f"Strike `{strike['id']}` acknowledged.")

    async def _mark_strike_acknowledged(
        self,
        guild_id: int,
        member_id: int,
        strike_id: str,
    ) -> dict[str, Any] | None:
        async with self._record_lock(guild_id, member_id):
            record = await self.get_record(guild_id, member_id)
            strike = next(
                (
                    item
                    for item in self.active_strikes(record)
                    if str(item.get("id", "")).casefold() == strike_id.casefold()
                    and item.get("acknowledged_at") is None
                ),
                None,
            )
            if strike is None:
                return None

            strike["acknowledged_at"] = iso_timestamp()
            await self.save_record(record)

        await self._notify_acknowledgement(guild_id, member_id, strike)
        return strike

    async def _notify_acknowledgement(
        self,
        guild_id: int,
        member_id: int,
        strike: dict[str, Any],
    ) -> None:
        notification_user = self.bot.get_user(self.notification_user_id)
        if notification_user is None:
            try:
                notification_user = await self.bot.fetch_user(
                    self.notification_user_id
                )
            except discord.HTTPException:
                log.warning(
                    "Could not fetch acknowledgement notification user %s",
                    self.notification_user_id,
                )
                return

        member = self.bot.get_user(member_id)
        mention = member.mention if member is not None else f"<@{member_id}>"
        try:
            await notification_user.send(
                f"{mention} acknowledged strike `{strike['id']}` "
                f"({strike.get('scope', 'staff')}) in guild `{guild_id}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning(
                "Could not DM acknowledgement notification user %s",
                self.notification_user_id,
            )

    async def _edit_acknowledgement_message(
        self,
        guild_id: int,
        member_id: int,
        strike: dict[str, Any],
    ) -> None:
        channel_id = strike.get("ack_channel_id")
        message_id = strike.get("ack_message_id")
        if not channel_id or not message_id:
            return

        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))
            message = await channel.fetch_message(int(message_id))
            view = self._acknowledgement_view(
                guild_id,
                member_id,
                str(strike["id"]),
                disabled=True,
            )
            await message.edit(
                content="Strike acknowledged.",
                view=view,
            )
            self._ack_views_by_message[int(message_id)] = view
        except discord.HTTPException:
            log.warning(
                "Could not update acknowledgement DM for strike %s",
                strike.get("id"),
            )

    def _can_view_target(
        self, issuer: discord.Member, target: discord.Member
    ) -> bool:
        return bool(self._viewable_scopes(issuer, target))

    def _viewable_scopes(
        self, issuer: discord.Member, target: discord.Member
    ) -> set[str]:
        if self.has_all_strikes_authority(issuer):
            return {"staff", "game_admin"}
        scopes: tuple[StrikeScope, ...] = ("staff", "game_admin")
        return {
            scope
            for scope in scopes
            if self.can_manage_strike_target(issuer, target, scope)
        }

    @staticmethod
    async def _send_lines(ctx: commands.Context, lines: list[str]) -> None:
        page = ""
        for line in lines:
            candidate = f"{page}\n{line}" if page else line
            if len(candidate) > 1800:
                await ctx.send(
                    page,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                page = line
            else:
                page = candidate
        if page:
            await ctx.send(
                page,
                allowed_mentions=discord.AllowedMentions.none(),
            )


async def setup(bot: commands.Bot) -> None:
    """Entry point required by ModMail's extension loader."""
    await bot.add_cog(StaffStrikes(bot))
