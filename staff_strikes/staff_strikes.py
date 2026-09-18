"""Staff strike tracking for ModMail.

Commands:
    .strike <member> <duration>
    .rstrike <member>
    .mystrikes [member]
    .allstrikes

The prefix is controlled by ModMail, so the commands also work if the server
uses a prefix other than ".".
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord.ext import commands

try:
    # This works when the whole plugin package is present.
    from . import config
except ImportError:
    # Some ModMail plugin loaders copy/load only the extension file and do not
    # expose sibling modules. Keep a complete fallback so the plugin still
    # loads in that format.
    class _InlineConfig:
        STAFF_TEAM_ROLE_ID = 1461572126174875886
        GAME_ADMIN_ROLE_ID = 1490692440146051092
        STAFF_RANKS = [
            {"name": "Trial Moderator", "role_id": 1457047936465633381},
            {"name": "Moderator", "role_id": 1457049978030653460},
            {"name": "Senior Moderator", "role_id": 1458421728718880791},
            {"name": "Staff Management", "role_id": 1457039931351367872},
            {"name": "Overseer", "role_id": 1546847847067025509},
            {"name": "Head of Staff", "role_id": 1458892950309441709},
            {"name": "Admin", "role_id": 1424785285782438089},
            {"name": "Head Admin", "role_id": 1272561419061297184},
        ]
        STRIKE_AUTHORITY_RANK = "Staff Management"
        ALL_STRIKES_AUTHORITY_RANKS = {"Admin", "Head Admin"}
        EXTRA_STAFF_ROLE_IDS_TO_REMOVE = set()
        MAX_STRIKES = 3

    config = _InlineConfig()


UTC = timezone.utc
ACKNOWLEDGEMENT_WINDOW = timedelta(hours=24)
DURATION_PATTERN = re.compile(
    r"^(?P<amount>\d+(?:\.\d+)?)(?P<unit>s|m|h|d|w|mo|y)$",
    re.IGNORECASE,
)
DURATION_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
    # A calendar month cannot be represented exactly without a dependency.
    # Treating it as 30 days is predictable and is explained in the help text.
    "mo": "days",
    "y": "days",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_duration(value: str) -> Optional[timedelta]:
    """Parse a strike duration, or return None for a permanent strike."""
    normalized = value.strip().lower()
    if normalized in {"perm", "permanent", "forever"}:
        return None

    match = DURATION_PATTERN.fullmatch(normalized)
    if not match:
        raise ValueError(
            "Duration must look like `30m`, `12h`, `7d`, `2w`, `1mo`, "
            "`1y`, or `permanent`."
        )

    amount = float(match.group("amount"))
    unit = match.group("unit").lower()
    if amount <= 0:
        raise ValueError("Duration must be greater than zero.")

    keyword = DURATION_UNITS[unit]
    if unit == "mo":
        amount *= 30
    elif unit == "y":
        amount *= 365

    return timedelta(**{keyword: amount})


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize MongoDB's naive datetimes and timezone-aware datetimes."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def acknowledgement_deadline(strike: dict[str, Any]) -> Optional[datetime]:
    explicit_deadline = as_utc(strike.get("ack_deadline"))
    if explicit_deadline is not None:
        return explicit_deadline

    issued_at = as_utc(strike.get("issued_at"))
    return issued_at + ACKNOWLEDGEMENT_WINDOW if issued_at is not None else None


def format_duration(expires_at: Optional[datetime]) -> str:
    if expires_at is None:
        return "permanent"
    remaining = as_utc(expires_at) - utc_now()
    if remaining.total_seconds() <= 0:
        return "expired"
    seconds = int(remaining.total_seconds())
    if seconds < 60:
        return f"{seconds}s remaining"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m remaining"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h remaining"
    days = hours // 24
    return f"{days}d remaining"


@dataclass(frozen=True)
class Rank:
    name: str
    role_id: int
    level: int


class AcknowledgeView(discord.ui.View):
    """Persistent DM button for one strike notice."""

    def __init__(
        self,
        plugin: "StaffStrikes",
        guild_id: int,
        member_id: int,
        strike_id: str,
        acknowledged: bool = False,
    ):
        super().__init__(timeout=None)
        self.plugin = plugin
        self.guild_id = guild_id
        self.member_id = member_id
        self.strike_id = strike_id

        button = discord.ui.Button(
            label="Acknowledged" if acknowledged else "Acknowledge",
            style=discord.ButtonStyle.success if acknowledged else discord.ButtonStyle.primary,
            custom_id=f"staff_strike:ack:{guild_id}:{strike_id}",
            disabled=acknowledged,
        )
        button.callback = self.acknowledge
        self.add_item(button)

    async def acknowledge(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message(
                "Only the staff member who received this strike can acknowledge it.",
                ephemeral=True,
            )
            return

        result = await self.plugin.acknowledge_strike(
            self.guild_id, self.member_id, self.strike_id
        )
        if result == "missing":
            await interaction.response.send_message(
                "This strike could not be found. It may have been removed already.",
                ephemeral=True,
            )
            return
        if result == "removed":
            await interaction.response.send_message(
                "This strike has already been removed by staff.",
                ephemeral=True,
            )
            return
        if result == "expired":
            await interaction.response.send_message(
                "The 24-hour acknowledgement deadline has passed. "
                "This strike cannot be acknowledged now, and it will make the "
                "recipient ineligible for the next payout.",
                ephemeral=True,
            )
            return
        if result == "already":
            await interaction.response.send_message(
                "This strike has already been acknowledged.",
                ephemeral=True,
            )
            return

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.label = "Acknowledged"
                item.style = discord.ButtonStyle.success
                item.disabled = True

        await interaction.response.edit_message(
            content="Strike acknowledged.",
            view=self,
        )


class StaffStrikes(commands.Cog):
    """Track time-limited staff strikes in a ModMail plugin partition."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.collection = bot.api.get_plugin_partition(self)
        self.ranks = self._load_ranks()
        self.rank_by_name = {rank.name.casefold(): rank for rank in self.ranks}

    @staticmethod
    def _load_ranks() -> list[Rank]:
        ranks: list[Rank] = []
        for level, entry in enumerate(config.STAFF_RANKS):
            name = str(entry.get("name", "")).strip()
            role_id = int(entry.get("role_id", 0))
            if not name or role_id <= 0:
                continue
            ranks.append(Rank(name=name, role_id=role_id, level=level))
        return ranks

    @property
    def configured(self) -> bool:
        return (
            config.STAFF_TEAM_ROLE_ID > 0
            and bool(self.ranks)
            and config.STRIKE_AUTHORITY_RANK.casefold() in self.rank_by_name
        )

    @property
    def removable_staff_role_ids(self) -> set[int]:
        role_ids = {rank.role_id for rank in self.ranks}
        role_ids.add(config.STAFF_TEAM_ROLE_ID)
        if config.GAME_ADMIN_ROLE_ID > 0:
            role_ids.add(config.GAME_ADMIN_ROLE_ID)
        role_ids.update(
            role_id for role_id in config.EXTRA_STAFF_ROLE_IDS_TO_REMOVE if role_id > 0
        )
        return role_ids

    def highest_rank(self, member: discord.Member) -> Optional[Rank]:
        member_role_ids = {role.id for role in member.roles}
        matching = [rank for rank in self.ranks if rank.role_id in member_role_ids]
        return max(matching, key=lambda rank: rank.level, default=None)

    def is_staff(self, member: discord.Member) -> bool:
        role_ids = {role.id for role in member.roles}
        return bool(
            config.STAFF_TEAM_ROLE_ID in role_ids
            or config.GAME_ADMIN_ROLE_ID in role_ids
            or any(rank.role_id in role_ids for rank in self.ranks)
        )

    def has_strike_authority(self, member: discord.Member) -> bool:
        rank = self.highest_rank(member)
        required = self.rank_by_name.get(config.STRIKE_AUTHORITY_RANK.casefold())
        return rank is not None and required is not None and rank.level >= required.level

    def has_all_strikes_authority(self, member: discord.Member) -> bool:
        rank = self.highest_rank(member)
        allowed_levels = {
            self.rank_by_name[name.casefold()].level
            for name in config.ALL_STRIKES_AUTHORITY_RANKS
            if name.casefold() in self.rank_by_name
        }
        return rank is not None and rank.level in allowed_levels

    @commands.Cog.listener()
    async def on_plugins_ready(self) -> None:
        """Re-register acknowledgement buttons after a bot restart."""
        if not self.configured:
            return

        cursor = self.collection.find({})
        async for record in cursor:
            for strike in self.pending_acknowledgements(record):
                if not strike.get("acknowledged", False):
                    self.bot.add_view(
                        AcknowledgeView(
                            self,
                            int(record["guild_id"]),
                            int(record["user_id"]),
                            str(strike["id"]),
                        )
                    )

    @staticmethod
    def active_strikes(record: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
        if not record:
            return []
        now = utc_now()
        active: list[dict[str, Any]] = []
        for strike in record.get("strikes", []):
            if strike.get("removed_at") is not None:
                continue
            expires_at = as_utc(strike.get("expires_at"))
            if expires_at is None or expires_at > now:
                active.append(strike)
        return active

    @staticmethod
    def pending_acknowledgements(
        record: Optional[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not record:
            return []
        return [
            strike
            for strike in record.get("strikes", [])
            if not strike.get("acknowledged", False)
            and strike.get("removed_at") is None
        ]

    async def get_record(self, guild_id: int, member_id: int) -> Optional[dict[str, Any]]:
        return await self.collection.find_one(
            {"guild_id": str(guild_id), "user_id": str(member_id)}
        )

    async def save_record(
        self,
        guild_id: int,
        member_id: int,
        strikes: list[dict[str, Any]],
        display_name: str,
    ) -> dict[str, Any]:
        record = {
            "guild_id": str(guild_id),
            "user_id": str(member_id),
            "display_name": display_name,
            "strikes": strikes,
            "updated_at": utc_now(),
        }
        await self.collection.replace_one(
            {"guild_id": str(guild_id), "user_id": str(member_id)},
            record,
            upsert=True,
        )
        return record

    async def acknowledge_strike(
        self, guild_id: int, member_id: int, strike_id: str
    ) -> str:
        record = await self.get_record(guild_id, member_id)
        if not record:
            return "missing"
        for strike in record.get("strikes", []):
            if str(strike.get("id")) == strike_id:
                if strike.get("removed_at") is not None:
                    return "removed"
                if strike.get("acknowledged", False):
                    return "already"
                deadline = acknowledgement_deadline(strike)
                if deadline is not None and deadline <= utc_now():
                    return "expired"
                strike["acknowledged"] = True
                strike["acknowledged_at"] = utc_now()
                await self.save_record(
                    guild_id,
                    member_id,
                    record.get("strikes", []),
                    record.get("display_name", str(member_id)),
                )
                return "acknowledged"
        return "missing"

    async def payout_block_reason(
        self,
        guild_id: int,
        member_id: int,
        payout_cycle_id: Optional[str] = None,
    ) -> Optional[str]:
        """Return a reason when a missed strike acknowledgement blocks payout."""
        record = await self.get_record(guild_id, member_id)
        now = utc_now()
        changed = False
        for strike in self.pending_acknowledgements(record):
            deadline = acknowledgement_deadline(strike)
            if deadline is not None and deadline <= now:
                blocked_cycle_id = strike.get("payout_blocked_cycle_id")
                if (
                    payout_cycle_id is not None
                    and blocked_cycle_id is not None
                    and blocked_cycle_id != payout_cycle_id
                ):
                    continue
                if payout_cycle_id is not None and blocked_cycle_id != payout_cycle_id:
                    strike["payout_blocked_cycle_id"] = payout_cycle_id
                    changed = True
                if changed:
                    await self.save_record(
                        guild_id,
                        member_id,
                        record.get("strikes", []),
                        record.get("display_name", str(member_id)),
                    )
                return (
                    "Payout withheld: a staff strike was not acknowledged within "
                    f"24 hours (deadline {discord.utils.format_dt(deadline, 'F')}). "
                    "This applies to the current payout cycle only."
                )
        return None

    async def ensure_ready(self, ctx: commands.Context) -> bool:
        if self.configured:
            return True
        await ctx.send(
            "Staff strikes are not configured yet. Add the staff role IDs and "
            "rank order in `staff_strikes/config.py`."
        )
        return False

    async def resolve_member(self, ctx: commands.Context, value: str) -> discord.Member:
        try:
            return await commands.MemberConverter().convert(ctx, value)
        except commands.MemberNotFound:
            raise commands.BadArgument(
                "I could not find that server member. Use their username, mention, or ID."
            )

    async def remove_staff_roles(self, member: discord.Member) -> list[str]:
        roles = [
            role for role in member.roles if role.id in self.removable_staff_role_ids
        ]
        if not roles:
            return []
        await member.remove_roles(
            *roles,
            reason="Reached the maximum number of active staff strikes",
        )
        return [role.name for role in roles]

    @commands.command(
        name="strike",
        help="Issue a timed staff strike: .strike <username/id> <duration>.",
    )
    @commands.guild_only()
    async def strike(
        self, ctx: commands.Context, member_value: str, duration_value: str
    ) -> None:
        if not await self.ensure_ready(ctx):
            return
        issuer = ctx.author
        if not isinstance(issuer, discord.Member) or not self.has_strike_authority(issuer):
            await ctx.send("Only Staff Management and higher ranks can issue strikes.")
            return

        member = await self.resolve_member(ctx, member_value)
        if not self.is_staff(member):
            await ctx.send("Strikes can only be applied to staff team members.")
            return

        issuer_rank = self.highest_rank(issuer)
        target_rank = self.highest_rank(member)
        if issuer_rank is None or target_rank is None:
            await ctx.send("Both the issuer and target must have a configured staff rank.")
            return
        if target_rank.level > issuer_rank.level:
            await ctx.send("You cannot strike a staff member ranked higher than you.")
            return

        try:
            duration = parse_duration(duration_value)
        except ValueError as error:
            await ctx.send(str(error))
            return

        now = utc_now()
        expires_at = now + duration if duration is not None else None
        record = await self.get_record(ctx.guild.id, member.id)
        stored_strikes = list(record.get("strikes", [])) if record else []
        strikes = self.active_strikes(record)
        if len(strikes) >= config.MAX_STRIKES:
            await ctx.send(
                f"{member.mention} already has {config.MAX_STRIKES} active strikes "
                "and has had their staff roles removed."
            )
            return

        strike = {
            "id": uuid.uuid4().hex,
            "issued_by_id": str(issuer.id),
            "issued_by_name": str(issuer),
            "issued_at": now,
            "expires_at": expires_at,
            "ack_deadline": now + ACKNOWLEDGEMENT_WINDOW,
            "acknowledged": False,
            "acknowledged_at": None,
        }
        stored_strikes.append(strike)
        await self.save_record(ctx.guild.id, member.id, stored_strikes, str(member))
        active_count = len(strikes) + 1

        view = AcknowledgeView(self, ctx.guild.id, member.id, strike["id"])
        self.bot.add_view(view)
        dm_sent = True
        try:
            await member.send(
                embed=discord.Embed(
                    title="Staff strike issued",
                    description=(
                        f"You have received **strike {active_count}/{config.MAX_STRIKES}** "
                        f"in **{ctx.guild.name}**."
                    ),
                    color=discord.Color.red(),
                    timestamp=now,
                )
                .add_field(name="Issued by", value=issuer.mention)
                .add_field(name="Duration", value=format_duration(expires_at))
                .add_field(
                    name="Acknowledge by",
                    value=discord.utils.format_dt(strike["ack_deadline"], "F"),
                )
                .set_footer(
                    text="You have 24 hours to acknowledge this notice. "
                    "Missing the deadline makes you ineligible for the next payout."
                ),
                view=view,
            )
        except (discord.Forbidden, discord.HTTPException):
            dm_sent = False

        roles_removed: list[str] = []
        if active_count >= config.MAX_STRIKES:
            try:
                roles_removed = await self.remove_staff_roles(member)
            except (discord.Forbidden, discord.HTTPException):
                await ctx.send(
                    "The strike was recorded, but I could not remove the staff roles. "
                    "Check that my bot role is above those roles."
                )
                roles_removed = []

        message = (
            f"Recorded strike {active_count}/{config.MAX_STRIKES} for {member.mention} "
            f"({format_duration(expires_at)})."
        )
        if roles_removed:
            message += " Maximum reached; removed: " + ", ".join(roles_removed) + "."
        if not dm_sent:
            message += " I could not DM them; their privacy settings may block DMs."
        await ctx.send(message)

    @commands.command(
        name="rstrike",
        help="Remove the most recent active strike: .rstrike <username/id>.",
    )
    @commands.guild_only()
    async def remove_strike(self, ctx: commands.Context, member_value: str) -> None:
        if not await self.ensure_ready(ctx):
            return
        issuer = ctx.author
        if not isinstance(issuer, discord.Member) or not self.has_strike_authority(issuer):
            await ctx.send("Only Staff Management and higher ranks can remove strikes.")
            return

        member = await self.resolve_member(ctx, member_value)
        if not self.is_staff(member):
            await ctx.send("Strikes can only be managed for staff team members.")
            return

        issuer_rank = self.highest_rank(issuer)
        target_rank = self.highest_rank(member)
        if issuer_rank is None or target_rank is None:
            await ctx.send("Both the issuer and target must have a configured staff rank.")
            return
        if target_rank.level > issuer_rank.level:
            await ctx.send("You cannot remove a strike from a staff member ranked higher than you.")
            return

        record = await self.get_record(ctx.guild.id, member.id)
        stored_strikes = list(record.get("strikes", [])) if record else []
        strikes = self.active_strikes(record)
        if not strikes:
            await ctx.send(f"{member.mention} has no active strikes.")
            return

        removed = strikes[-1]
        removed["removed_at"] = utc_now()
        removed["removed_by_id"] = str(issuer.id)
        await self.save_record(ctx.guild.id, member.id, stored_strikes, str(member))
        remaining_count = len(strikes) - 1
        await ctx.send(
            f"Removed the most recent active strike from {member.mention}. "
            f"They now have {remaining_count}/{config.MAX_STRIKES} active strikes."
        )

    @commands.command(
        name="mystrikes",
        help="View your strikes, or another staff member's strikes: .mystrikes [username/id].",
    )
    @commands.guild_only()
    async def my_strikes(
        self, ctx: commands.Context, member_value: Optional[str] = None
    ) -> None:
        if not await self.ensure_ready(ctx):
            return
        if not isinstance(ctx.author, discord.Member) or not self.is_staff(ctx.author):
            await ctx.send("Only staff team members can use this command.")
            return

        member = (
            await self.resolve_member(ctx, member_value)
            if member_value
            else ctx.author
        )
        if not self.is_staff(member):
            await ctx.send("Strikes can only be viewed for staff team members.")
            return

        record = await self.get_record(ctx.guild.id, member.id)
        strikes = self.active_strikes(record)
        lines = [
            f"**{index}.** {format_duration(strike.get('expires_at'))} "
            f"— {'acknowledged' if strike.get('acknowledged') else 'not acknowledged'}"
            for index, strike in enumerate(strikes, start=1)
        ]
        description = "\n".join(lines) if lines else "No active strikes."
        await ctx.send(
            embed=discord.Embed(
                title=f"Strikes for {member}",
                description=description,
                color=discord.Color.orange() if strikes else discord.Color.green(),
            ).set_footer(text=f"{len(strikes)}/{config.MAX_STRIKES} active strikes"),
        )

    @commands.command(
        name="allstrikes",
        help="View every staff member with active strikes.",
    )
    @commands.guild_only()
    async def all_strikes(self, ctx: commands.Context) -> None:
        if not await self.ensure_ready(ctx):
            return
        if not isinstance(ctx.author, discord.Member) or not self.has_all_strikes_authority(
            ctx.author
        ):
            await ctx.send("Only Admin and Head Admin can use this command.")
            return

        cursor = self.collection.find({"guild_id": str(ctx.guild.id)})
        lines: list[str] = []
        async for record in cursor:
            strikes = self.active_strikes(record)
            if strikes:
                lines.append(
                    f"<@{record['user_id']}> — {len(strikes)}/{config.MAX_STRIKES} "
                    f"active strike(s)"
                )

        description = "\n".join(lines) if lines else "No staff members have active strikes."
        await ctx.send(
            embed=discord.Embed(
                title="All active staff strikes",
                description=description,
                color=discord.Color.orange() if lines else discord.Color.green(),
            )
        )

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: `{error.param.name}`.")
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("These commands can only be used inside the server.")
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffStrikes(bot))
