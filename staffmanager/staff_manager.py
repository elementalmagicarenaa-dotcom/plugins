"""
Staff Manager — Modmail Plugin
===============================
Features
--------
* Tracks Dyno mod actions and logs them to #mod-action-log with rich embeds.
* Posts a weekly Mod Activity Report every Monday to #mod-activity-log.
* /inactivityreq command — submits a Leave of Absence request with Accept/Decline buttons.
  Sends DM on decision and DM when the duration expires.
* /promotionreq command — auto-fills achievements from tracked data, sends to
  #promotion-request with Accept/Decline buttons, DM on decision, and logs to
  #promotion-log.
* Tracks role changes and automatically logs promotions/demotions.

Environment variables (set with ?config set KEY VALUE in Modmail, or in .env):
  MOD_ACTION_LOG_CHANNEL        — Channel ID for mod action logs
  MOD_ACTIVITY_LOG_CHANNEL      — Channel ID for weekly activity report
  INACTIVITY_CHANNEL            — Channel ID for inactivity requests
  PROMOTION_REQUEST_CHANNEL     — Channel ID for promotion requests
  PROMOTION_LOG_CHANNEL         — Channel ID for promotion logs
  DYNO_ID                       — Dyno's user ID (default: 155149108183695360)
  STAFF_IDS                     — Comma-separated staff user IDs for weekly report
  STAFF_MANAGEMENT_ROLE_IDS     — Comma-separated role IDs that can approve LOA requests
  HIGH_RANK_ROLE_IDS            — Comma-separated role IDs that can approve promotions
  TRIAL_MODERATOR_ROLE_ID
  MODERATOR_ROLE_ID
  SENIOR_MODERATOR_ROLE_ID
  STAFF_MANAGEMENT_ROLE_ID
  HEAD_OF_STAFF_ROLE_ID
  ADMIN_ROLE_ID
  HEAD_ADMIN_ROLE_ID
  WEEKLY_REPORT_HOUR            — UTC hour (0–23) for the Monday report (default: 9)
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import ui
from discord.ext import commands, tasks

# ---------------------------------------------------------------------------
# Constants & paths
# ---------------------------------------------------------------------------

DYNO_DEFAULT_ID = 155149108183695360

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
MOD_ACTIONS_FILE = os.path.join(DATA_DIR, "mod_actions.json")
INACTIVITY_FILE = os.path.join(DATA_DIR, "inactivity.json")
ROLE_HISTORY_FILE = os.path.join(DATA_DIR, "role_history.json")
PENDING_PROMOTIONS_FILE = os.path.join(DATA_DIR, "pending_promotions.json")

os.makedirs(DATA_DIR, exist_ok=True)

# Rank hierarchy — ordered lowest → highest
RANK_HIERARCHY = [
    "Trial Moderator",
    "Moderator",
    "Senior Moderator",
    "Staff Management",
    "Head of Staff",
    "Admin",
    "Head Admin",
]

RANK_EMOJIS = {
    "Trial Moderator": "🛡️",
    "Moderator": "⚔️",
    "Senior Moderator": "🔱",
    "Staff Management": "👑",
    "Head of Staff": "🌟",
    "Admin": "💫",
    "Head Admin": "🏆",
}

ACTION_COLORS = {
    "warn":        0xF1C40F,
    "mute":        0xE67E22,
    "unmute":      0x2ECC71,
    "kick":        0xE74C3C,
    "ban":         0x992D22,
    "unban":       0x1ABC9C,
    "softban":     0xC0392B,
    "deafen":      0x9B59B6,
    "undeafen":    0xAED6F1,
    "voicemute":   0xD35400,
    "voiceunmute": 0x27AE60,
    "note":        0x95A5A6,
}

ACTION_ICONS = {
    "warn":        "⚠️",
    "mute":        "🔇",
    "unmute":      "🔊",
    "kick":        "👟",
    "ban":         "🔨",
    "unban":       "🔓",
    "softban":     "🪃",
    "deafen":      "🎧",
    "undeafen":    "🎙️",
    "voicemute":   "🎤",
    "voiceunmute": "📣",
    "note":        "📝",
}

# Keywords in Dyno embed titles → normalised action key
DYNO_TITLE_MAP = {
    "warned":       "warn",
    "warn":         "warn",
    "muted":        "mute",
    "mute":         "mute",
    "unmuted":      "unmute",
    "unmute":       "unmute",
    "kicked":       "kick",
    "kick":         "kick",
    "banned":       "ban",
    "ban":          "ban",
    "unbanned":     "unban",
    "unban":        "unban",
    "softbanned":   "softban",
    "softban":      "softban",
    "deafened":     "deafen",
    "deafen":       "deafen",
    "undeafened":   "undeafen",
    "undeafen":     "undeafen",
    "voice muted":  "voicemute",
    "voice unmuted":"voiceunmute",
    "noted":        "note",
    "note":         "note",
}

# Human-readable labels for activity report rows
ACTION_LABELS = {
    "warn":        "Warnings",
    "mute":        "Mutes",
    "unmute":      "Unmutes",
    "kick":        "Kicks",
    "ban":         "Bans",
    "unban":       "Unbans",
    "softban":     "Softbans",
    "deafen":      "Deafens",
    "undeafen":    "Undeafens",
    "voicemute":   "Voice Mutes",
    "voiceunmute": "Voice Unmutes",
    "note":        "Notes",
}

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _load(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

DURATION_RE = re.compile(
    r"(?:(\d+)\s*w(?:eeks?)?)?"
    r"(?:(\d+)\s*d(?:ays?)?)?"
    r"(?:(\d+)\s*h(?:ours?)?)?"
    r"(?:(\d+)\s*m(?:inutes?|ins?)?)?"
    r"(?:(\d+)\s*s(?:econds?|ecs?)?)?",
    re.IGNORECASE,
)


def parse_duration(text: str) -> Optional[timedelta]:
    """Parse a duration string like '5d', '2h30m', '1w' into a timedelta."""
    text = text.strip()
    m = DURATION_RE.fullmatch(text)
    if not m or not any(m.groups()):
        return None
    weeks, days, hours, minutes, seconds = (int(v or 0) for v in m.groups())
    td = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return td if td.total_seconds() > 0 else None


def format_duration(td: timedelta) -> str:
    """Return a human-readable string like '2 weeks, 3 days, 4 hours'."""
    total_seconds = int(td.total_seconds())
    weeks, remainder = divmod(total_seconds, 604800)
    days, remainder = divmod(remainder, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if weeks:
        parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds and not parts:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return ", ".join(parts) if parts else "0 seconds"


def discord_timestamp(dt: datetime, style: str = "F") -> str:
    """Return a Discord timestamp string, e.g. <t:1234567890:F>."""
    return f"<t:{int(dt.timestamp())}:{style}>"


# ---------------------------------------------------------------------------
# ENV helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(key, default)


def _env_int(key: str, default: int = 0) -> int:
    v = os.environ.get(key)
    return int(v) if v and v.isdigit() else default


def _env_list(key: str) -> list[int]:
    v = os.environ.get(key, "")
    return [int(x.strip()) for x in v.split(",") if x.strip().isdigit()]


# ---------------------------------------------------------------------------
# Dyno embed parser
# ---------------------------------------------------------------------------

def _parse_dyno_embed(embed: discord.Embed) -> Optional[dict]:
    """
    Try to extract mod-action data from a Dyno embed.
    Returns a dict with keys: action, user_id, user_tag, moderator, reason
    or None if the embed doesn't look like a Dyno mod action.
    """
    title = (embed.title or embed.author.name or "").lower()

    action = None
    for keyword, act in DYNO_TITLE_MAP.items():
        if keyword in title:
            action = act
            break
    if action is None:
        return None

    data: dict = {"action": action, "user_id": None, "user_tag": None,
                  "moderator": None, "reason": "No reason provided."}

    # Helper to search fields by name (case-insensitive)
    def field(name: str) -> Optional[str]:
        name_lower = name.lower()
        for f in embed.fields:
            if name_lower in f.name.lower():
                return f.value
        return None

    # Try to extract user info from description or fields
    desc = embed.description or ""

    # User field — various Dyno formats
    user_val = (field("user") or field("member") or field("target") or "")
    # Strip Discord mention markup
    user_val = re.sub(r"[<@!>]", "", user_val).strip()
    if user_val:
        # Could be "Username#0000 (123456)" or just an ID
        id_match = re.search(r"\((\d{17,20})\)", user_val)
        if id_match:
            data["user_id"] = int(id_match.group(1))
        elif re.fullmatch(r"\d{17,20}", user_val):
            data["user_id"] = int(user_val)
        # Tag portion (everything before the parenthesis)
        tag_part = re.sub(r"\s*\(\d+\)\s*$", "", user_val).strip()
        if tag_part:
            data["user_tag"] = tag_part

    # Fall back to parsing description
    if not data["user_id"]:
        for pattern in [
            r"\*\*User:\*\*\s*(?:<@!?)?(\d{17,20})>?",
            r"User:\s*(?:<@!?)?(\d{17,20})>?",
        ]:
            m = re.search(pattern, desc, re.IGNORECASE)
            if m:
                data["user_id"] = int(m.group(1))
                break

    # Moderator field
    mod_val = (field("moderator") or field("responsible moderator")
               or field("mod") or field("staff") or "")
    mod_val = re.sub(r"[<@!>]", "", mod_val).strip()
    if mod_val:
        data["moderator"] = mod_val
    else:
        # Try description
        m = re.search(r"(?:Responsible Moderator|Moderator):\s*(.+)", desc, re.IGNORECASE)
        if m:
            data["moderator"] = re.sub(r"[<@!>]", "", m.group(1)).strip()

    # Reason field
    reason_val = field("reason")
    if reason_val:
        data["reason"] = reason_val.strip() or "No reason provided."

    return data


# ---------------------------------------------------------------------------
# Views — buttons
# ---------------------------------------------------------------------------

class InactivityView(ui.View):
    """Persistent Accept/Decline buttons for an inactivity (LOA) request."""

    def __init__(self, cog: "StaffManagerCog", request_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id
        self.accept_button.custom_id = f"loa_accept_{request_id}"
        self.decline_button.custom_id = f"loa_decline_{request_id}"

    @ui.button(label="✅  Accept", style=discord.ButtonStyle.success, row=0)
    async def accept_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog._handle_loa_decision(interaction, self.request_id, accepted=True)

    @ui.button(label="❌  Decline", style=discord.ButtonStyle.danger, row=0)
    async def decline_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog._handle_loa_decision(interaction, self.request_id, accepted=False)


class PromotionView(ui.View):
    """Persistent Accept/Decline buttons for a promotion request."""

    def __init__(self, cog: "StaffManagerCog", request_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id
        self.accept_button.custom_id = f"promo_accept_{request_id}"
        self.decline_button.custom_id = f"promo_decline_{request_id}"

    @ui.button(label="✅  Accept Promotion", style=discord.ButtonStyle.success, row=0)
    async def accept_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog._handle_promotion_decision(interaction, self.request_id, accepted=True)

    @ui.button(label="❌  Decline Promotion", style=discord.ButtonStyle.danger, row=0)
    async def decline_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog._handle_promotion_decision(interaction, self.request_id, accepted=False)


# ---------------------------------------------------------------------------
# Main Cog
# ---------------------------------------------------------------------------

class StaffManagerCog(commands.Cog, name="Staff Manager"):
    """Comprehensive staff management and moderation logging for Modmail servers."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._mod_actions: dict = _load(MOD_ACTIONS_FILE)
        self._inactivity: dict = _load(INACTIVITY_FILE)
        self._role_history: dict = _load(ROLE_HISTORY_FILE)
        self._pending_promotions: dict = _load(PENDING_PROMOTIONS_FILE)
        self._last_weekly_report_date: Optional[str] = None

    async def cog_load(self) -> None:
        self.weekly_report_task.start()
        self.check_inactivity_expiry.start()

    async def cog_unload(self) -> None:
        self.weekly_report_task.cancel()
        self.check_inactivity_expiry.cancel()

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Read from bot config first, then env."""
        try:
            v = self.bot.config.get(key)
            if v:
                return str(v)
        except Exception:
            pass
        return _env(key, default)

    def _cfg_int(self, key: str, default: int = 0) -> int:
        v = self._cfg(key)
        return int(v) if v and str(v).isdigit() else default

    def _cfg_list(self, key: str) -> list[int]:
        v = self._cfg(key, "")
        return [int(x.strip()) for x in (v or "").split(",") if x.strip().isdigit()]

    @property
    def dyno_id(self) -> int:
        return self._cfg_int("DYNO_ID", DYNO_DEFAULT_ID)

    def _get_channel(self, key: str) -> Optional[discord.TextChannel]:
        ch_id = self._cfg_int(key)
        if not ch_id:
            return None
        return self.bot.get_channel(ch_id)

    def _staff_role_map(self) -> dict[str, int]:
        """Return a mapping of rank name → role ID from config."""
        keys = {
            "Trial Moderator":   "TRIAL_MODERATOR_ROLE_ID",
            "Moderator":         "MODERATOR_ROLE_ID",
            "Senior Moderator":  "SENIOR_MODERATOR_ROLE_ID",
            "Staff Management":  "STAFF_MANAGEMENT_ROLE_ID",
            "Head of Staff":     "HEAD_OF_STAFF_ROLE_ID",
            "Admin":             "ADMIN_ROLE_ID",
            "Head Admin":        "HEAD_ADMIN_ROLE_ID",
        }
        return {rank: self._cfg_int(env_key) for rank, env_key in keys.items()
                if self._cfg_int(env_key)}

    def _get_member_rank(self, member: discord.Member) -> Optional[str]:
        """Return the highest staff rank the member currently holds."""
        role_map = self._staff_role_map()
        member_role_ids = {r.id for r in member.roles}
        for rank in reversed(RANK_HIERARCHY):
            rid = role_map.get(rank)
            if rid and rid in member_role_ids:
                return rank
        return None

    def _next_rank(self, current_rank: str) -> Optional[str]:
        """Return the rank directly above the given one, or None at the top."""
        try:
            idx = RANK_HIERARCHY.index(current_rank)
            return RANK_HIERARCHY[idx + 1] if idx + 1 < len(RANK_HIERARCHY) else None
        except ValueError:
            return None

    def _time_in_rank(self, user_id: int, rank: str) -> Optional[timedelta]:
        """Return how long the user has held their current rank."""
        uid = str(user_id)
        history = self._role_history.get(uid, {})
        rank_since_str = history.get(rank)
        if not rank_since_str:
            return None
        try:
            since = datetime.fromisoformat(rank_since_str)
            return datetime.now(timezone.utc) - since
        except ValueError:
            return None

    def _user_action_totals(self, user_id: int) -> dict[str, int]:
        """Return lifetime mod action counts for a user across all weeks."""
        uid = str(user_id)
        totals: dict[str, int] = {}
        for _week, staff_data in self._mod_actions.items():
            for action, count in staff_data.get(uid, {}).items():
                totals[action] = totals.get(action, 0) + count
        return totals

    def _week_key(self, dt: Optional[datetime] = None) -> str:
        dt = dt or datetime.now(timezone.utc)
        # ISO week: YYYY-WNN
        return dt.strftime("%Y-W%W")

    def _record_action(self, moderator_id: int, action: str) -> None:
        week = self._week_key()
        uid = str(moderator_id)
        self._mod_actions.setdefault(week, {}).setdefault(uid, {})
        self._mod_actions[week][uid][action] = (
            self._mod_actions[week][uid].get(action, 0) + 1
        )
        _save(MOD_ACTIONS_FILE, self._mod_actions)

    # ------------------------------------------------------------------
    # Dyno listener — mod action detection
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.id != self.dyno_id:
            return
        if not message.embeds:
            return

        channel = self._get_channel("MOD_ACTION_LOG_CHANNEL")
        if not channel:
            return

        for embed in message.embeds:
            data = _parse_dyno_embed(embed)
            if data is None:
                continue

            await self._post_mod_action_log(channel, data, message)

    async def _post_mod_action_log(
        self,
        log_channel: discord.TextChannel,
        data: dict,
        source_message: discord.Message,
    ) -> None:
        action = data["action"]
        color = ACTION_COLORS.get(action, 0x2F3136)
        icon = ACTION_ICONS.get(action, "🔹")
        label = ACTION_LABELS.get(action, action.capitalize())

        # Try to resolve user object for richer display
        user_mention = f"<@{data['user_id']}>" if data["user_id"] else "Unknown"
        user_display = data.get("user_tag") or (f"ID: {data['user_id']}" if data["user_id"] else "Unknown")

        mod_display = data.get("moderator") or "Unknown"
        # Try to resolve moderator mention
        mod_mention = f"**{mod_display}**"
        if data["user_id"]:
            # Attempt to record action against the moderator ID
            # We may not know their ID at this point — we use their display name
            pass

        now = datetime.now(timezone.utc)

        embed = discord.Embed(
            title=f"{icon}  Moderation Action — {label}",
            color=color,
            timestamp=now,
        )
        embed.add_field(
            name="👤  Target User",
            value=f"{user_mention}\n`{user_display}`",
            inline=True,
        )
        embed.add_field(
            name="⚖️  Action",
            value=f"`{label}`",
            inline=True,
        )
        embed.add_field(
            name="🛡️  Moderator",
            value=mod_mention,
            inline=True,
        )
        embed.add_field(
            name="📋  Reason",
            value=data["reason"],
            inline=False,
        )
        embed.add_field(
            name="🔗  Source",
            value=f"[Jump to original log]({source_message.jump_url})",
            inline=False,
        )
        embed.set_footer(
            text=f"Logged from #{source_message.channel.name}  •  {now.strftime('%d %b %Y, %H:%M UTC')}",
            icon_url=self.bot.user.display_avatar.url,
        )

        await log_channel.send(embed=embed)

        # Record action if moderator ID is resolved
        # (Try to find moderator in guild by display name as fallback)
        guild = log_channel.guild
        if guild and mod_display != "Unknown":
            found_member = discord.utils.find(
                lambda m: m.display_name == mod_display or str(m) == mod_display,
                guild.members,
            )
            if found_member:
                self._record_action(found_member.id, action)

    # ------------------------------------------------------------------
    # Role change listener — promotion/demotion tracking
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles == after.roles:
            return

        role_map = self._staff_role_map()
        staff_role_ids = set(role_map.values())

        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}

        added = after_ids - before_ids
        removed = before_ids - after_ids

        uid = str(after.id)

        # Track when new staff roles were assigned
        for rank, rid in role_map.items():
            if rid in added:
                self._role_history.setdefault(uid, {})[rank] = (
                    datetime.now(timezone.utc).isoformat()
                )
        _save(ROLE_HISTORY_FILE, self._role_history)

        # Detect promotion (gained a higher staff role)
        added_staff = added & staff_role_ids
        removed_staff = removed & staff_role_ids
        if not added_staff:
            return

        # Reverse map: role_id → rank name
        id_to_rank = {v: k for k, v in role_map.items()}

        for rid in added_staff:
            new_rank = id_to_rank.get(rid)
            if not new_rank:
                continue
            # Determine old rank
            old_rank = None
            for oid in removed_staff:
                candidate = id_to_rank.get(oid)
                if candidate and RANK_HIERARCHY.index(candidate) < RANK_HIERARCHY.index(new_rank):
                    old_rank = candidate
                    break

            await self._log_rank_change(after, old_rank, new_rank)

    async def _log_rank_change(
        self,
        member: discord.Member,
        old_rank: Optional[str],
        new_rank: str,
    ) -> None:
        channel = self._get_channel("PROMOTION_LOG_CHANNEL")
        if not channel:
            return

        is_promotion = (
            old_rank is None
            or RANK_HIERARCHY.index(new_rank) > RANK_HIERARCHY.index(old_rank)
        )
        title = "📈  Promotion Logged" if is_promotion else "📉  Role Update Logged"
        color = 0x2ECC71 if is_promotion else 0xE74C3C
        new_emoji = RANK_EMOJIS.get(new_rank, "")

        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
        embed.set_author(
            name=str(member),
            icon_url=member.display_avatar.url,
        )
        if old_rank:
            embed.add_field(
                name="Previous Rank",
                value=f"{RANK_EMOJIS.get(old_rank, '')} {old_rank}",
                inline=True,
            )
        embed.add_field(
            name="New Rank",
            value=f"{new_emoji} {new_rank}",
            inline=True,
        )
        embed.add_field(
            name="Member",
            value=member.mention,
            inline=True,
        )
        embed.set_footer(
            text=f"User ID: {member.id}",
            icon_url=self.bot.user.display_avatar.url,
        )
        await channel.send(embed=embed)

    # ------------------------------------------------------------------
    # Weekly Mod Activity Report
    # ------------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def weekly_report_task(self) -> None:
        """Check every 5 minutes; fire the report on Monday at the configured hour."""
        now = datetime.now(timezone.utc)
        if now.weekday() != 0:  # 0 = Monday
            return
        target_hour = int(self._cfg("WEEKLY_REPORT_HOUR", "9") or 9)
        if now.hour != target_hour:
            return
        # Only fire once per Monday (track by date string)
        today_str = now.strftime("%Y-%m-%d")
        if self._last_weekly_report_date == today_str:
            return
        self._last_weekly_report_date = today_str
        await self._post_weekly_activity_report()

    @weekly_report_task.before_loop
    async def before_weekly_report(self) -> None:
        await self.bot.wait_until_ready()

    async def _post_weekly_activity_report(self) -> None:
        channel = self._get_channel("MOD_ACTIVITY_LOG_CHANNEL")
        if not channel:
            return

        week = self._week_key()
        week_data = self._mod_actions.get(week, {})

        staff_ids = self._cfg_list("STAFF_IDS")
        if not staff_ids:
            # Fall back to anyone who acted this week
            staff_ids = [int(uid) for uid in week_data.keys() if uid.isdigit()]

        now = datetime.now(timezone.utc)
        # Calculate Monday–Sunday range
        monday = now - timedelta(days=now.weekday())
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        sunday = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)

        header_embed = discord.Embed(
            title="📊  Weekly Moderation Activity Report",
            description=(
                f"**Period:** {discord_timestamp(monday, 'D')} — {discord_timestamp(sunday, 'D')}\n"
                f"*This report is automatically generated every Monday.*"
            ),
            color=0x5865F2,
            timestamp=now,
        )
        header_embed.set_footer(
            text=f"Week {now.strftime('%W')} · {now.year}",
            icon_url=self.bot.user.display_avatar.url,
        )
        await channel.send(embed=header_embed)

        if not staff_ids:
            await channel.send(
                embed=discord.Embed(
                    description="No moderation activity recorded this week.",
                    color=0x95A5A6,
                )
            )
            return

        for uid in staff_ids:
            member = channel.guild.get_member(uid)
            if member is None:
                try:
                    member = await channel.guild.fetch_member(uid)
                except discord.NotFound:
                    continue

            actions = week_data.get(str(uid), {})
            total = sum(actions.values())

            rank = self._get_member_rank(member)
            rank_display = f"{RANK_EMOJIS.get(rank, '')} {rank}" if rank else "Staff"

            embed = discord.Embed(
                title=f"Staff Activity — {member.display_name}",
                color=0x5865F2,
                timestamp=now,
            )
            embed.set_author(
                name=f"{member.display_name}  ·  {rank_display}",
                icon_url=member.display_avatar.url,
            )
            embed.add_field(name="Staff Member", value=member.mention, inline=True)
            embed.add_field(name="Rank", value=rank_display, inline=True)
            embed.add_field(name="Total Actions", value=str(total), inline=True)

            # Action breakdown
            breakdown_lines = []
            for act in ["warn", "mute", "unmute", "kick", "ban", "unban",
                         "softban", "deafen", "undeafen", "voicemute", "voiceunmute", "note"]:
                count = actions.get(act, 0)
                if count:
                    breakdown_lines.append(
                        f"{ACTION_ICONS[act]} **{ACTION_LABELS[act]}:** {count}×"
                    )
            if not breakdown_lines:
                breakdown_lines = ["*No actions recorded this week.*"]

            embed.add_field(
                name="📋  Actions This Week",
                value="\n".join(breakdown_lines),
                inline=False,
            )
            embed.set_footer(
                text=f"User ID: {uid}  ·  Auto-generated report",
                icon_url=self.bot.user.display_avatar.url,
            )
            await channel.send(embed=embed)
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Check inactivity expiry
    # ------------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def check_inactivity_expiry(self) -> None:
        now = datetime.now(timezone.utc)
        expired = []
        for req_id, req in list(self._inactivity.items()):
            if req.get("status") != "accepted":
                continue
            end_str = req.get("ends_at")
            if not end_str:
                continue
            try:
                ends_at = datetime.fromisoformat(end_str)
            except ValueError:
                continue
            if now >= ends_at and not req.get("notified"):
                expired.append(req_id)

        for req_id in expired:
            req = self._inactivity[req_id]
            uid = req.get("user_id")
            if uid:
                try:
                    user = await self.bot.fetch_user(int(uid))
                    embed = discord.Embed(
                        title="🔔  Leave of Absence Ended",
                        description=(
                            "Your approved Leave of Absence has now ended. "
                            "Welcome back — we hope you're feeling better! "
                            "Please resume your duties as soon as possible."
                        ),
                        color=0x2ECC71,
                        timestamp=now,
                    )
                    embed.add_field(
                        name="Duration",
                        value=req.get("duration_str", "N/A"),
                        inline=True,
                    )
                    embed.add_field(
                        name="Reason",
                        value=req.get("reason", "N/A"),
                        inline=True,
                    )
                    embed.set_footer(text="Staff Manager · Automated Notification")
                    await user.send(embed=embed)
                except Exception:
                    pass
            self._inactivity[req_id]["notified"] = True
        if expired:
            _save(INACTIVITY_FILE, self._inactivity)

    @check_inactivity_expiry.before_loop
    async def before_check_inactivity(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # LOA decision handler
    # ------------------------------------------------------------------

    async def _handle_loa_decision(
        self,
        interaction: discord.Interaction,
        request_id: str,
        *,
        accepted: bool,
    ) -> None:
        # Permission check
        allowed_role_ids = self._cfg_list("STAFF_MANAGEMENT_ROLE_IDS")
        member = interaction.user
        if allowed_role_ids:
            member_role_ids = {r.id for r in getattr(member, "roles", [])}
            if not member_role_ids.intersection(set(allowed_role_ids)):
                await interaction.response.send_message(
                    "❌ You don't have permission to handle LOA requests.", ephemeral=True
                )
                return

        req = self._inactivity.get(request_id)
        if not req:
            await interaction.response.send_message(
                "❌ This request could not be found.", ephemeral=True
            )
            return
        if req.get("status") not in (None, "pending"):
            await interaction.response.send_message(
                f"ℹ️ This request has already been **{req['status']}**.", ephemeral=True
            )
            return

        now = datetime.now(timezone.utc)
        req["status"] = "accepted" if accepted else "declined"
        req["reviewed_by"] = str(interaction.user)
        req["reviewed_at"] = now.isoformat()

        if accepted:
            td = parse_duration(req.get("raw_duration", ""))
            ends_at = now + td if td else now + timedelta(days=14)
            req["ends_at"] = ends_at.isoformat()

        _save(INACTIVITY_FILE, self._inactivity)

        # Update the original embed
        decision_color = 0x2ECC71 if accepted else 0xE74C3C
        decision_label = "✅ Accepted" if accepted else "❌ Declined"

        original_embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if original_embed:
            new_embed = original_embed.copy()
            new_embed.color = decision_color
            new_embed.add_field(
                name="Decision",
                value=(
                    f"{decision_label} by {interaction.user.mention}\n"
                    f"{discord_timestamp(now, 'F')}"
                ),
                inline=False,
            )
            if accepted and req.get("ends_at"):
                ends_at = datetime.fromisoformat(req["ends_at"])
                new_embed.add_field(
                    name="LOA Ends",
                    value=discord_timestamp(ends_at, "F"),
                    inline=False,
                )
            await interaction.message.edit(embed=new_embed, view=None)

        await interaction.response.send_message(
            f"{'✅' if accepted else '❌'} LOA request **{decision_label}** by {interaction.user.mention}.",
            ephemeral=True,
        )

        # DM the requester
        uid = req.get("user_id")
        if uid:
            try:
                user = await self.bot.fetch_user(int(uid))
                dm_embed = discord.Embed(
                    title=(
                        "✅  Leave of Absence Approved"
                        if accepted
                        else "❌  Leave of Absence Declined"
                    ),
                    color=decision_color,
                    timestamp=now,
                )
                dm_embed.add_field(
                    name="Reviewed by", value=str(interaction.user), inline=True
                )
                dm_embed.add_field(
                    name="Duration", value=req.get("duration_str", "N/A"), inline=True
                )
                dm_embed.add_field(
                    name="Reason", value=req.get("reason", "N/A"), inline=False
                )
                if accepted and req.get("ends_at"):
                    ends_at_dt = datetime.fromisoformat(req["ends_at"])
                    dm_embed.add_field(
                        name="Your LOA ends",
                        value=discord_timestamp(ends_at_dt, "F"),
                        inline=False,
                    )
                if not accepted:
                    dm_embed.description = (
                        "Unfortunately, your LOA request was not approved at this time. "
                        "Please reach out to Staff Management if you have questions."
                    )
                dm_embed.set_footer(text="Staff Manager · Automated Notification")
                await user.send(embed=dm_embed)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Inactivity request command
    # ------------------------------------------------------------------

    @commands.command(name="inactivityreq", aliases=["loa", "loareq"])
    async def inactivity_request(self, ctx: commands.Context, duration: str, *, reason: str) -> None:
        """
        Submit a Leave of Absence request.
        Usage: !inactivityreq <duration> <reason>
        Examples: !inactivityreq 5d I'm sick
                  !inactivityreq 2w Family emergency
        """
        # Must be a staff member
        member_rank = self._get_member_rank(ctx.author)
        if not member_rank:
            await ctx.message.add_reaction("❌")
            await ctx.send(
                embed=discord.Embed(
                    description="❌ You must be a staff member to submit an LOA request.",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        td = parse_duration(duration)
        if td is None:
            await ctx.message.add_reaction("❌")
            await ctx.send(
                embed=discord.Embed(
                    description=(
                        "❌ Invalid duration format.\n"
                        "Use: `5d` (days), `2h` (hours), `30m` (minutes), `1w` (week), or combine: `1d12h`"
                    ),
                    color=0xE74C3C,
                ),
                delete_after=15,
            )
            return

        max_duration = timedelta(days=14)
        if td > max_duration:
            await ctx.message.add_reaction("❌")
            await ctx.send(
                embed=discord.Embed(
                    description="❌ The maximum Leave of Absence duration is **14 days**.",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        channel = self._get_channel("INACTIVITY_CHANNEL")
        if not channel:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Inactivity request channel is not configured.",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        now = datetime.now(timezone.utc)
        duration_str = format_duration(td)
        request_id = f"{ctx.author.id}_{int(now.timestamp())}"

        embed = discord.Embed(
            title="📋  Leave of Absence Request",
            color=0xF1C40F,
            timestamp=now,
        )
        embed.set_author(
            name=f"{ctx.author.display_name}  ·  {member_rank}",
            icon_url=ctx.author.display_avatar.url,
        )
        embed.add_field(
            name="🛡️  Staff Member",
            value=f"{ctx.author.mention}\n`{ctx.author}`",
            inline=True,
        )
        embed.add_field(
            name="🏅  Current Rank",
            value=f"{RANK_EMOJIS.get(member_rank, '')} {member_rank}",
            inline=True,
        )
        embed.add_field(
            name="⏱️  Requested Duration",
            value=f"**{duration_str}**",
            inline=True,
        )
        embed.add_field(
            name="📄  Reason",
            value=reason,
            inline=False,
        )
        embed.add_field(
            name="🕐  Submitted",
            value=discord_timestamp(now, "F"),
            inline=True,
        )
        embed.add_field(
            name="📌  Status",
            value="⏳ Pending Review",
            inline=True,
        )
        embed.set_footer(
            text=f"User ID: {ctx.author.id}  ·  Request ID: {request_id}",
            icon_url=self.bot.user.display_avatar.url,
        )

        view = InactivityView(self, request_id)
        msg = await channel.send(embed=embed, view=view)

        # Persist
        self._inactivity[request_id] = {
            "user_id": str(ctx.author.id),
            "raw_duration": duration,
            "duration_str": duration_str,
            "reason": reason,
            "status": "pending",
            "message_id": str(msg.id),
            "channel_id": str(channel.id),
        }
        _save(INACTIVITY_FILE, self._inactivity)

        await ctx.message.add_reaction("✅")
        try:
            confirm_embed = discord.Embed(
                title="✅  LOA Request Submitted",
                description=(
                    f"Your Leave of Absence request has been submitted to {channel.mention}.\n"
                    "You will receive a DM once it has been reviewed."
                ),
                color=0x2ECC71,
                timestamp=now,
            )
            confirm_embed.add_field(name="Duration", value=duration_str, inline=True)
            confirm_embed.add_field(name="Reason", value=reason, inline=True)
            confirm_embed.set_footer(text="Staff Manager · Automated Notification")
            await ctx.author.send(embed=confirm_embed)
        except discord.Forbidden:
            pass

    # ------------------------------------------------------------------
    # Promotion request command
    # ------------------------------------------------------------------

    @commands.command(name="promotionreq", aliases=["promote", "promoapp"])
    async def promotion_request(self, ctx: commands.Context, *, reason: str) -> None:
        """
        Submit a promotion request.
        Usage: !promotionreq <reason>
        Example: !promotionreq I have met all requirements and been active for 3 weeks.
        """
        member_rank = self._get_member_rank(ctx.author)
        if not member_rank:
            await ctx.message.add_reaction("❌")
            await ctx.send(
                embed=discord.Embed(
                    description="❌ You must be a staff member to submit a promotion request.",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        # Staff Management and above cannot request promotion
        ineligible = ["Staff Management", "Head of Staff", "Admin", "Head Admin"]
        if member_rank in ineligible:
            await ctx.message.add_reaction("❌")
            await ctx.send(
                embed=discord.Embed(
                    description=(
                        f"❌ Members with the rank **{member_rank}** are not eligible "
                        f"to submit a promotion request."
                    ),
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        desired_rank = self._next_rank(member_rank)
        if not desired_rank:
            await ctx.message.add_reaction("❌")
            await ctx.send(
                embed=discord.Embed(
                    description="❌ You are already at the highest promotable rank.",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        channel = self._get_channel("PROMOTION_REQUEST_CHANNEL")
        if not channel:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Promotion request channel is not configured.",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        # Gather achievement data
        totals = self._user_action_totals(ctx.author.id)
        time_in_rank = self._time_in_rank(ctx.author.id, member_rank)

        achievements_lines = []
        for act in ["warn", "mute", "unmute", "kick", "ban", "unban",
                     "softban", "deafen", "undeafen", "voicemute", "voiceunmute"]:
            count = totals.get(act, 0)
            if count:
                achievements_lines.append(
                    f"{ACTION_ICONS[act]} {ACTION_LABELS[act]}: **{count}**"
                )
        if not achievements_lines:
            achievements_lines = ["*No tracked mod actions yet.*"]

        now = datetime.now(timezone.utc)
        request_id = f"{ctx.author.id}_{int(now.timestamp())}"

        time_in_rank_str = format_duration(time_in_rank) if time_in_rank else "Unknown (role history not tracked)"

        # Ping high-rank reviewers
        high_rank_role_ids = self._cfg_list("HIGH_RANK_ROLE_IDS")
        ping_text = " ".join(f"<@&{rid}>" for rid in high_rank_role_ids) if high_rank_role_ids else ""

        embed = discord.Embed(
            title="🌟  Promotion Request",
            color=0xF1C40F,
            timestamp=now,
        )
        embed.set_author(
            name=f"{ctx.author.display_name}  ·  {member_rank}",
            icon_url=ctx.author.display_avatar.url,
        )
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        embed.add_field(
            name="👤  Staff Name",
            value=f"{ctx.author.mention}\n`{ctx.author}`",
            inline=True,
        )
        embed.add_field(
            name="🏅  Current Rank",
            value=f"{RANK_EMOJIS.get(member_rank, '')} {member_rank}",
            inline=True,
        )
        embed.add_field(
            name="🎯  Desired Rank",
            value=f"{RANK_EMOJIS.get(desired_rank, '')} {desired_rank}",
            inline=True,
        )
        embed.add_field(
            name="⏳  Time in Current Rank",
            value=time_in_rank_str,
            inline=False,
        )
        embed.add_field(
            name="🏆  Achievements / Evidence",
            value="\n".join(achievements_lines),
            inline=False,
        )
        embed.add_field(
            name="📝  Reason for Promotion",
            value=reason,
            inline=False,
        )
        embed.add_field(
            name="🕐  Timestamp",
            value=discord_timestamp(now, "F"),
            inline=True,
        )
        embed.add_field(
            name="📌  Status",
            value="⏳ Pending Review",
            inline=True,
        )
        embed.set_footer(
            text=f"User ID: {ctx.author.id}  ·  Request ID: {request_id}",
            icon_url=self.bot.user.display_avatar.url,
        )

        view = PromotionView(self, request_id)
        content = ping_text if ping_text else None
        msg = await channel.send(content=content, embed=embed, view=view)

        # Persist
        self._pending_promotions[request_id] = {
            "user_id": str(ctx.author.id),
            "current_rank": member_rank,
            "desired_rank": desired_rank,
            "reason": reason,
            "status": "pending",
            "message_id": str(msg.id),
            "channel_id": str(channel.id),
        }
        _save(PENDING_PROMOTIONS_FILE, self._pending_promotions)

        await ctx.message.add_reaction("✅")
        try:
            confirm_embed = discord.Embed(
                title="✅  Promotion Request Submitted",
                description=(
                    f"Your promotion request has been submitted to {channel.mention}.\n"
                    "You will receive a DM once it has been reviewed by Staff Management."
                ),
                color=0x2ECC71,
                timestamp=now,
            )
            confirm_embed.add_field(name="Current Rank", value=member_rank, inline=True)
            confirm_embed.add_field(name="Desired Rank", value=desired_rank, inline=True)
            confirm_embed.set_footer(text="Staff Manager · Automated Notification")
            await ctx.author.send(embed=confirm_embed)
        except discord.Forbidden:
            pass

    # ------------------------------------------------------------------
    # Promotion decision handler
    # ------------------------------------------------------------------

    async def _handle_promotion_decision(
        self,
        interaction: discord.Interaction,
        request_id: str,
        *,
        accepted: bool,
    ) -> None:
        high_rank_role_ids = self._cfg_list("HIGH_RANK_ROLE_IDS")
        member = interaction.user
        if high_rank_role_ids:
            member_role_ids = {r.id for r in getattr(member, "roles", [])}
            if not member_role_ids.intersection(set(high_rank_role_ids)):
                await interaction.response.send_message(
                    "❌ You don't have permission to handle promotion requests.", ephemeral=True
                )
                return

        req = self._pending_promotions.get(request_id)
        if not req:
            await interaction.response.send_message(
                "❌ This request could not be found.", ephemeral=True
            )
            return
        if req.get("status") not in (None, "pending"):
            await interaction.response.send_message(
                f"ℹ️ This request has already been **{req['status']}**.", ephemeral=True
            )
            return

        now = datetime.now(timezone.utc)
        req["status"] = "accepted" if accepted else "declined"
        req["reviewed_by"] = str(interaction.user)
        req["reviewed_at"] = now.isoformat()
        _save(PENDING_PROMOTIONS_FILE, self._pending_promotions)

        decision_color = 0x2ECC71 if accepted else 0xE74C3C
        decision_label = "✅ Accepted" if accepted else "❌ Declined"

        # Update original embed
        original_embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if original_embed:
            new_embed = original_embed.copy()
            new_embed.color = decision_color
            new_embed.add_field(
                name="Decision",
                value=(
                    f"{decision_label} by {interaction.user.mention}\n"
                    f"{discord_timestamp(now, 'F')}"
                ),
                inline=False,
            )
            await interaction.message.edit(embed=new_embed, view=None)

        await interaction.response.send_message(
            f"{'✅' if accepted else '❌'} Promotion request **{decision_label}** by {interaction.user.mention}.",
            ephemeral=True,
        )

        uid = req.get("user_id")
        desired_rank = req.get("desired_rank", "")
        current_rank = req.get("current_rank", "")

        # DM the requester
        if uid:
            try:
                user = await self.bot.fetch_user(int(uid))
                dm_embed = discord.Embed(
                    title=(
                        "🎉  Promotion Approved!"
                        if accepted
                        else "📋  Promotion Request Declined"
                    ),
                    color=decision_color,
                    timestamp=now,
                )
                if accepted:
                    dm_embed.description = (
                        f"Congratulations! Your promotion request has been approved. "
                        f"You are now promoted to **{RANK_EMOJIS.get(desired_rank, '')} {desired_rank}**. "
                        f"Keep up the excellent work!"
                    )
                else:
                    dm_embed.description = (
                        f"Your promotion request from **{current_rank}** to **{desired_rank}** "
                        f"was not approved at this time. Please continue working hard and try again later. "
                        f"Feel free to reach out to Staff Management for feedback."
                    )
                dm_embed.add_field(
                    name="Reviewed by", value=str(interaction.user), inline=True
                )
                dm_embed.add_field(
                    name="Current Rank", value=current_rank, inline=True
                )
                if accepted:
                    dm_embed.add_field(
                        name="New Rank",
                        value=f"{RANK_EMOJIS.get(desired_rank, '')} {desired_rank}",
                        inline=True,
                    )
                dm_embed.set_footer(text="Staff Manager · Automated Notification")
                await user.send(embed=dm_embed)
            except Exception:
                pass

        # Log promotion if accepted
        if accepted:
            log_channel = self._get_channel("PROMOTION_LOG_CHANNEL")
            if log_channel:
                guild = interaction.guild
                target_member = guild.get_member(int(uid)) if guild and uid else None

                promo_embed = discord.Embed(
                    title="🎊  Staff Promotion",
                    color=0x2ECC71,
                    timestamp=now,
                )
                promo_embed.add_field(
                    name="Staff Member",
                    value=f"<@{uid}>" if uid else "Unknown",
                    inline=True,
                )
                promo_embed.add_field(
                    name="Previous Rank",
                    value=f"{RANK_EMOJIS.get(current_rank, '')} {current_rank}",
                    inline=True,
                )
                promo_embed.add_field(
                    name="New Rank",
                    value=f"{RANK_EMOJIS.get(desired_rank, '')} {desired_rank}",
                    inline=True,
                )
                promo_embed.add_field(
                    name="Approved by",
                    value=interaction.user.mention,
                    inline=True,
                )
                promo_embed.add_field(
                    name="Date",
                    value=discord_timestamp(now, "F"),
                    inline=True,
                )
                if target_member:
                    promo_embed.set_author(
                        name=str(target_member),
                        icon_url=target_member.display_avatar.url,
                    )
                promo_embed.set_footer(
                    text=f"User ID: {uid}",
                    icon_url=self.bot.user.display_avatar.url,
                )
                await log_channel.send(embed=promo_embed)

    # ------------------------------------------------------------------
    # Manual mod action command (for actions not from Dyno)
    # ------------------------------------------------------------------

    @commands.command(name="modlog")
    @commands.has_permissions(kick_members=True)
    async def manual_mod_log(
        self,
        ctx: commands.Context,
        action: str,
        user: discord.User,
        *,
        reason: str = "No reason provided.",
    ) -> None:
        """
        Manually log a mod action.
        Usage: !modlog <action> <user> [reason]
        Actions: warn, mute, unmute, kick, ban, unban, softban
        """
        action = action.lower()
        if action not in ACTION_COLORS:
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ Unknown action `{action}`. Valid: {', '.join(ACTION_COLORS.keys())}",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        channel = self._get_channel("MOD_ACTION_LOG_CHANNEL")
        if not channel:
            await ctx.send(
                embed=discord.Embed(description="❌ Mod action log channel not configured.", color=0xE74C3C),
                delete_after=10,
            )
            return

        data = {
            "action": action,
            "user_id": user.id,
            "user_tag": str(user),
            "moderator": str(ctx.author),
            "reason": reason,
        }
        await self._post_mod_action_log(channel, data, ctx.message)
        self._record_action(ctx.author.id, action)
        await ctx.message.add_reaction("✅")

    # ------------------------------------------------------------------
    # Staff stats command
    # ------------------------------------------------------------------

    @commands.command(name="staffstats")
    async def staff_stats(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
    ) -> None:
        """
        Show all-time mod action stats for a staff member.
        Usage: !staffstats [@member]
        """
        target = member or ctx.author
        totals = self._user_action_totals(target.id)
        rank = self._get_member_rank(target)

        embed = discord.Embed(
            title=f"📊  Staff Statistics — {target.display_name}",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(target), icon_url=target.display_avatar.url)
        embed.add_field(
            name="Rank",
            value=f"{RANK_EMOJIS.get(rank, '')} {rank}" if rank else "Not a staff member",
            inline=True,
        )
        embed.add_field(
            name="Total Actions",
            value=str(sum(totals.values())),
            inline=True,
        )

        lines = []
        for act in ["warn", "mute", "unmute", "kick", "ban", "unban",
                     "softban", "deafen", "undeafen", "voicemute", "voiceunmute", "note"]:
            count = totals.get(act, 0)
            if count:
                lines.append(f"{ACTION_ICONS[act]} **{ACTION_LABELS[act]}:** {count}")
        embed.add_field(
            name="All-Time Breakdown",
            value="\n".join(lines) if lines else "*No actions tracked yet.*",
            inline=False,
        )

        time_in = self._time_in_rank(target.id, rank) if rank else None
        if time_in:
            embed.add_field(
                name=f"Time as {rank}",
                value=format_duration(time_in),
                inline=False,
            )

        embed.set_footer(
            text=f"User ID: {target.id}",
            icon_url=self.bot.user.display_avatar.url,
        )
        await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffManagerCog(bot))
