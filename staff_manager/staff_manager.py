"""
Staff Manager — Modmail Plugin
===============================
Tracks Dyno mod actions, posts weekly activity reports, handles inactivity
requests, promotion requests with Accept/Decline buttons, and auto-logs
role changes.

Environment variables (set with ?config set KEY VALUE in Modmail):
  MOD_ACTION_LOG_CHANNEL        Channel ID for mod action logs
  MOD_ACTIVITY_LOG_CHANNEL      Channel ID for weekly activity report
  INACTIVITY_CHANNEL            Channel ID for inactivity requests
  PROMOTION_REQUEST_CHANNEL     Channel ID for promotion requests
  PROMOTION_LOG_CHANNEL         Channel ID for promotion logs
  DYNO_ID                       Dyno user ID (default: 155149108183695360)
  STAFF_IDS                     Comma-separated staff user IDs
  STAFF_MANAGEMENT_ROLE_IDS     Role IDs that can approve LOA requests
  HIGH_RANK_ROLE_IDS            Role IDs that can approve promotions
  TRIAL_MODERATOR_ROLE_ID
  MODERATOR_ROLE_ID
  SENIOR_MODERATOR_ROLE_ID
  STAFF_MANAGEMENT_ROLE_ID
  HEAD_OF_STAFF_ROLE_ID
  ADMIN_ROLE_ID
  HEAD_ADMIN_ROLE_ID
  WEEKLY_REPORT_HOUR            UTC hour for Monday report (default: 9)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import discord
from discord import ui
from discord.ext import commands, tasks

# ---------------------------------------------------------------------------
# Constants & file paths
# ---------------------------------------------------------------------------

DYNO_DEFAULT_ID = 155149108183695360

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MOD_ACTIONS_FILE    = os.path.join(DATA_DIR, "mod_actions.json")
INACTIVITY_FILE     = os.path.join(DATA_DIR, "inactivity.json")
ROLE_HISTORY_FILE   = os.path.join(DATA_DIR, "role_history.json")
PROMOTIONS_FILE     = os.path.join(DATA_DIR, "pending_promotions.json")

os.makedirs(DATA_DIR, exist_ok=True)

# Rank hierarchy — lowest to highest
RANK_HIERARCHY: List[str] = [
    "Trial Moderator",
    "Moderator",
    "Senior Moderator",
    "Staff Management",
    "Head of Staff",
    "Admin",
    "Head Admin",
]

RANK_EMOJIS: Dict[str, str] = {
    "Trial Moderator":  "🛡️",
    "Moderator":        "⚔️",
    "Senior Moderator": "🔱",
    "Staff Management": "👑",
    "Head of Staff":    "🌟",
    "Admin":            "💫",
    "Head Admin":       "🏆",
}

ACTION_COLORS: Dict[str, int] = {
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

ACTION_ICONS: Dict[str, str] = {
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

# Keywords found in Dyno embed titles mapped to a normalised action key
DYNO_TITLE_MAP: Dict[str, str] = {
    "warned":        "warn",
    "warn":          "warn",
    "muted":         "mute",
    "mute":          "mute",
    "unmuted":       "unmute",
    "unmute":        "unmute",
    "kicked":        "kick",
    "kick":          "kick",
    "banned":        "ban",
    "ban":           "ban",
    "unbanned":      "unban",
    "unban":         "unban",
    "softbanned":    "softban",
    "softban":       "softban",
    "deafened":      "deafen",
    "deafen":        "deafen",
    "undeafened":    "undeafen",
    "undeafen":      "undeafen",
    "voice muted":   "voicemute",
    "voice unmuted": "voiceunmute",
    "noted":         "note",
    "note":          "note",
}

ACTION_LABELS: Dict[str, str] = {
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

ALL_ACTIONS: List[str] = list(ACTION_LABELS.keys())

# ---------------------------------------------------------------------------
# JSON persistence helpers
# ---------------------------------------------------------------------------

def _load(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

DURATION_RE = re.compile(
    r"^(?:(\d+)\s*w(?:eeks?)?)?"
    r"(?:(\d+)\s*d(?:ays?)?)?"
    r"(?:(\d+)\s*h(?:ours?)?)?"
    r"(?:(\d+)\s*m(?:inutes?|ins?)?)?"
    r"(?:(\d+)\s*s(?:econds?|ecs?)?)?$",
    re.IGNORECASE,
)


def parse_duration(text: str) -> Optional[timedelta]:
    """Parse '5d', '2h30m', '1w3d' etc. into a timedelta. Returns None on failure."""
    text = text.strip()
    m = DURATION_RE.fullmatch(text)
    if not m or not any(m.groups()):
        return None
    weeks, days, hours, minutes, seconds = (int(v or 0) for v in m.groups())
    td = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return td if td.total_seconds() > 0 else None


def format_duration(td: timedelta) -> str:
    """Return a human-readable string like '2 weeks, 3 days, 4 hours'."""
    total = int(td.total_seconds())
    weeks, r    = divmod(total, 604800)
    days, r     = divmod(r, 86400)
    hours, r    = divmod(r, 3600)
    minutes, s  = divmod(r, 60)
    parts = []
    if weeks:   parts.append(f"{weeks} week{'s' if weeks   != 1 else ''}")
    if days:    parts.append(f"{days} day{'s' if days     != 1 else ''}")
    if hours:   parts.append(f"{hours} hour{'s' if hours   != 1 else ''}")
    if minutes: parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if s and not parts: parts.append(f"{s} second{'s' if s != 1 else ''}")
    return ", ".join(parts) if parts else "0 seconds"


def ts(dt: datetime, style: str = "F") -> str:
    """Return a Discord timestamp string e.g. <t:1234567890:F>."""
    return f"<t:{int(dt.timestamp())}:{style}>"


# ---------------------------------------------------------------------------
# Config — reads from config.json in the same folder as this file
# ---------------------------------------------------------------------------

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def _load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# ---------------------------------------------------------------------------
# Dyno embed parser
# ---------------------------------------------------------------------------

def _parse_dyno_embed(embed: discord.Embed) -> Optional[dict]:
    """Return a dict with action/user_id/user_tag/moderator/reason, or None."""
    title = (embed.title or getattr(embed.author, "name", "") or "").lower()

    action: Optional[str] = None
    for keyword, act in DYNO_TITLE_MAP.items():
        if keyword in title:
            action = act
            break
    if action is None:
        return None

    result: Dict[str, object] = {
        "action":    action,
        "user_id":   None,
        "user_tag":  None,
        "moderator": None,
        "reason":    "No reason provided.",
    }

    def field_value(name: str) -> str:
        nl = name.lower()
        for f in embed.fields:
            if nl in f.name.lower():
                return f.value or ""
        return ""

    desc = embed.description or ""

    # --- User ---
    user_raw = field_value("user") or field_value("member") or field_value("target")
    user_raw = re.sub(r"[<@!>]", "", user_raw).strip()
    if user_raw:
        id_m = re.search(r"\((\d{17,20})\)", user_raw)
        if id_m:
            result["user_id"] = int(id_m.group(1))
        elif re.fullmatch(r"\d{17,20}", user_raw):
            result["user_id"] = int(user_raw)
        tag_part = re.sub(r"\s*\(\d+\)\s*$", "", user_raw).strip()
        if tag_part:
            result["user_tag"] = tag_part

    if not result["user_id"]:
        for pat in [r"User:\s*(?:<@!?)?(\d{17,20})>?",
                    r"\*\*User:\*\*\s*(?:<@!?)?(\d{17,20})>?"]:
            m2 = re.search(pat, desc, re.IGNORECASE)
            if m2:
                result["user_id"] = int(m2.group(1))
                break

    # --- Moderator ---
    mod_raw = (field_value("responsible moderator")
               or field_value("moderator")
               or field_value("mod")
               or field_value("staff"))
    mod_raw = re.sub(r"[<@!>]", "", mod_raw).strip()
    if mod_raw:
        result["moderator"] = mod_raw
    else:
        m3 = re.search(r"(?:Responsible Moderator|Moderator):\s*(.+)", desc, re.IGNORECASE)
        if m3:
            result["moderator"] = re.sub(r"[<@!>]", "", m3.group(1)).strip()

    # --- Reason ---
    reason_raw = field_value("reason")
    if reason_raw:
        result["reason"] = reason_raw.strip() or "No reason provided."

    return result


# ---------------------------------------------------------------------------
# Button Views — using add_item for Python 3.8+ compatibility
# ---------------------------------------------------------------------------

class InactivityView(ui.View):
    """Accept / Decline buttons for a Leave of Absence request."""

    def __init__(self, cog: StaffManagerCog, request_id: str) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id

        accept = ui.Button(
            label="✅  Accept",
            style=discord.ButtonStyle.success,
            custom_id=f"loa_accept_{request_id}",
        )
        accept.callback = self._accept
        self.add_item(accept)

        decline = ui.Button(
            label="❌  Decline",
            style=discord.ButtonStyle.danger,
            custom_id=f"loa_decline_{request_id}",
        )
        decline.callback = self._decline
        self.add_item(decline)

    async def _accept(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_loa_decision(interaction, self.request_id, accepted=True)

    async def _decline(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_loa_decision(interaction, self.request_id, accepted=False)


class PromotionView(ui.View):
    """Accept / Decline buttons for a promotion request."""

    def __init__(self, cog: StaffManagerCog, request_id: str) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id

        accept = ui.Button(
            label="✅  Accept Promotion",
            style=discord.ButtonStyle.success,
            custom_id=f"promo_accept_{request_id}",
        )
        accept.callback = self._accept
        self.add_item(accept)

        decline = ui.Button(
            label="❌  Decline Promotion",
            style=discord.ButtonStyle.danger,
            custom_id=f"promo_decline_{request_id}",
        )
        decline.callback = self._decline
        self.add_item(decline)

    async def _accept(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_promotion_decision(interaction, self.request_id, accepted=True)

    async def _decline(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_promotion_decision(interaction, self.request_id, accepted=False)


# ---------------------------------------------------------------------------
# Main Cog
# ---------------------------------------------------------------------------

class StaffManagerCog(commands.Cog, name="Staff Manager"):
    """Comprehensive staff management and mod logging for Modmail servers."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._mod_actions: dict         = _load(MOD_ACTIONS_FILE)
        self._inactivity: dict          = _load(INACTIVITY_FILE)
        self._role_history: dict        = _load(ROLE_HISTORY_FILE)
        self._pending_promotions: dict  = _load(PROMOTIONS_FILE)
        self._last_report_date: str     = ""

    async def cog_load(self) -> None:
        self.weekly_report_task.start()
        self.check_loa_expiry.start()

    async def cog_unload(self) -> None:
        self.weekly_report_task.cancel()
        self.check_loa_expiry.cancel()

    # ------------------------------------------------------------------ #
    # Config helpers                                                       #
    # ------------------------------------------------------------------ #

    def _cfg(self, key: str, default: str = "") -> str:
        cfg = _load_config()
        v = cfg.get(key)
        # Handle list values stored as JSON arrays
        if isinstance(v, list):
            return ",".join(str(x) for x in v)
        if v is not None and str(v) != "0":
            return str(v)
        # Fall back to env var
        return os.environ.get(key, default)

    def _cfg_int(self, key: str, default: int = 0) -> int:
        cfg = _load_config()
        v = cfg.get(key)
        if isinstance(v, int) and v != 0:
            return v
        env = os.environ.get(key, "")
        return int(env) if env.isdigit() else default

    def _cfg_list(self, key: str) -> List[int]:
        cfg = _load_config()
        v = cfg.get(key)
        # Native JSON array
        if isinstance(v, list):
            return [int(x) for x in v if str(x).isdigit() or isinstance(x, int)]
        # Comma-separated string fallback
        s = os.environ.get(key, "")
        return [int(x.strip()) for x in s.split(",") if x.strip().isdigit()]

    @property
    def _dyno_id(self) -> int:
        return self._cfg_int("DYNO_ID", DYNO_DEFAULT_ID)

    def _channel(self, key: str) -> Optional[discord.TextChannel]:
        cid = self._cfg_int(key)
        if not cid:
            return None
        return self.bot.get_channel(cid)  # type: ignore[return-value]

    def _staff_role_map(self) -> Dict[str, int]:
        keys = {
            "Trial Moderator":  "TRIAL_MODERATOR_ROLE_ID",
            "Moderator":        "MODERATOR_ROLE_ID",
            "Senior Moderator": "SENIOR_MODERATOR_ROLE_ID",
            "Staff Management": "STAFF_MANAGEMENT_ROLE_ID",
            "Head of Staff":    "HEAD_OF_STAFF_ROLE_ID",
            "Admin":            "ADMIN_ROLE_ID",
            "Head Admin":       "HEAD_ADMIN_ROLE_ID",
        }
        return {rank: self._cfg_int(env) for rank, env in keys.items() if self._cfg_int(env)}

    def _member_rank(self, member: discord.Member) -> Optional[str]:
        role_map = self._staff_role_map()
        member_role_ids = {r.id for r in member.roles}
        for rank in reversed(RANK_HIERARCHY):
            rid = role_map.get(rank)
            if rid and rid in member_role_ids:
                return rank
        return None

    def _next_rank(self, current: str) -> Optional[str]:
        try:
            idx = RANK_HIERARCHY.index(current)
            return RANK_HIERARCHY[idx + 1] if idx + 1 < len(RANK_HIERARCHY) else None
        except ValueError:
            return None

    def _time_in_rank(self, user_id: int, rank: str) -> Optional[timedelta]:
        record = self._role_history.get(str(user_id), {}).get(rank)
        if not record:
            return None
        try:
            since = datetime.fromisoformat(record)
            return datetime.now(timezone.utc) - since
        except ValueError:
            return None

    def _lifetime_totals(self, user_id: int) -> Dict[str, int]:
        totals: Dict[str, int] = {}
        for week_data in self._mod_actions.values():
            for act, count in week_data.get(str(user_id), {}).items():
                totals[act] = totals.get(act, 0) + count
        return totals

    def _week_key(self, dt: Optional[datetime] = None) -> str:
        return (dt or datetime.now(timezone.utc)).strftime("%Y-W%W")

    def _record_action(self, moderator_id: int, action: str) -> None:
        week = self._week_key()
        uid  = str(moderator_id)
        self._mod_actions.setdefault(week, {}).setdefault(uid, {})
        self._mod_actions[week][uid][action] = (
            self._mod_actions[week][uid].get(action, 0) + 1
        )
        _save(MOD_ACTIONS_FILE, self._mod_actions)

    # ------------------------------------------------------------------ #
    # Dyno listener                                                        #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.id != self._dyno_id:
            return
        if not message.embeds:
            return
        log_ch = self._channel("MOD_ACTION_LOG_CHANNEL")
        if not log_ch:
            return
        for embed in message.embeds:
            data = _parse_dyno_embed(embed)
            if data:
                await self._post_mod_action(log_ch, data, message)

    async def _post_mod_action(
        self,
        log_ch: discord.TextChannel,
        data: dict,
        src: discord.Message,
    ) -> None:
        action      = str(data["action"])
        color       = ACTION_COLORS.get(action, 0x2F3136)
        icon        = ACTION_ICONS.get(action, "🔹")
        label       = ACTION_LABELS.get(action, action.capitalize())
        user_id     = data.get("user_id")
        user_tag    = data.get("user_tag") or (f"ID: {user_id}" if user_id else "Unknown")
        mod_display = str(data.get("moderator") or "Unknown")
        reason      = str(data.get("reason") or "No reason provided.")
        now         = datetime.now(timezone.utc)

        user_mention = f"<@{user_id}>" if user_id else "Unknown"

        embed = discord.Embed(
            title=f"{icon}  Moderation Action — {label}",
            color=color,
            timestamp=now,
        )
        embed.add_field(name="👤  Target User",   value=f"{user_mention}\n`{user_tag}`", inline=True)
        embed.add_field(name="⚖️  Action",         value=f"`{label}`",                    inline=True)
        embed.add_field(name="🛡️  Moderator",      value=f"**{mod_display}**",             inline=True)
        embed.add_field(name="📋  Reason",         value=reason,                           inline=False)
        embed.add_field(name="🔗  Source",         value=f"[Jump to original log]({src.jump_url})", inline=False)
        embed.set_footer(
            text=f"Logged from #{src.channel.name}  •  {now.strftime('%d %b %Y, %H:%M UTC')}",
            icon_url=self.bot.user.display_avatar.url,
        )
        await log_ch.send(embed=embed)

        # Try to find the moderator as a guild member and record the action
        guild = log_ch.guild
        if guild and mod_display != "Unknown":
            found = discord.utils.find(
                lambda m: m.display_name == mod_display or str(m) == mod_display,
                guild.members,
            )
            if found:
                self._record_action(found.id, action)

    # ------------------------------------------------------------------ #
    # Role-change listener (promotion / demotion auto-log)                 #
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles == after.roles:
            return
        role_map = self._staff_role_map()
        if not role_map:
            return

        id_to_rank = {v: k for k, v in role_map.items()}
        staff_ids  = set(role_map.values())

        before_ids = {r.id for r in before.roles}
        after_ids  = {r.id for r in after.roles}
        added      = after_ids - before_ids
        removed    = before_ids - after_ids

        # Track when the role was assigned
        uid = str(after.id)
        for rank, rid in role_map.items():
            if rid in added:
                self._role_history.setdefault(uid, {})[rank] = (
                    datetime.now(timezone.utc).isoformat()
                )
        _save(ROLE_HISTORY_FILE, self._role_history)

        # Log promotions / demotions
        for rid in added & staff_ids:
            new_rank = id_to_rank.get(rid)
            if not new_rank:
                continue
            old_rank: Optional[str] = None
            for oid in removed & staff_ids:
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
        ch = self._channel("PROMOTION_LOG_CHANNEL")
        if not ch:
            return
        is_promo = (
            old_rank is None
            or RANK_HIERARCHY.index(new_rank) > RANK_HIERARCHY.index(old_rank)
        )
        color = 0x2ECC71 if is_promo else 0xE74C3C
        title = "📈  Promotion Logged" if is_promo else "📉  Role Update Logged"
        now   = datetime.now(timezone.utc)

        embed = discord.Embed(title=title, color=color, timestamp=now)
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        if old_rank:
            embed.add_field(name="Previous Rank", value=f"{RANK_EMOJIS.get(old_rank, '')} {old_rank}", inline=True)
        embed.add_field(name="New Rank",     value=f"{RANK_EMOJIS.get(new_rank, '')} {new_rank}", inline=True)
        embed.add_field(name="Member",       value=member.mention, inline=True)
        embed.set_footer(text=f"User ID: {member.id}", icon_url=self.bot.user.display_avatar.url)
        await ch.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Weekly report task                                                   #
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=5)
    async def weekly_report_task(self) -> None:
        now = datetime.now(timezone.utc)
        if now.weekday() != 0:
            return
        target_hour = int(self._cfg("WEEKLY_REPORT_HOUR", "9") or 9)
        if now.hour != target_hour:
            return
        today = now.strftime("%Y-%m-%d")
        if self._last_report_date == today:
            return
        self._last_report_date = today
        await self._post_weekly_report()

    @weekly_report_task.before_loop
    async def _before_weekly(self) -> None:
        await self.bot.wait_until_ready()

    async def _post_weekly_report(self) -> None:
        ch = self._channel("MOD_ACTIVITY_LOG_CHANNEL")
        if not ch:
            return

        now = datetime.now(timezone.utc)

        # Report fires on Monday — we want LAST week's data (Mon–Sun just ended)
        last_week_dt = now - timedelta(days=7)
        week         = self._week_key(last_week_dt)
        week_data    = self._mod_actions.get(week, {})
        staff_ids    = self._cfg_list("STAFF_IDS") or [int(k) for k in week_data if k.isdigit()]

        # Date range for the report header (last Mon → last Sun)
        last_monday = (last_week_dt - timedelta(days=last_week_dt.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        last_sunday = last_monday + timedelta(days=6, hours=23, minutes=59, seconds=59)

        header = discord.Embed(
            title="📊  Weekly Moderation Activity Report",
            description=(
                f"**Period:** {ts(last_monday, 'D')} — {ts(last_sunday, 'D')}\n"
                f"*Automatically generated every Monday — covering the previous week's actions.*"
            ),
            color=0x5865F2,
            timestamp=now,
        )
        header.set_footer(
            text=f"Week {last_week_dt.strftime('%W')} · {last_week_dt.year}",
            icon_url=self.bot.user.display_avatar.url,
        )
        await ch.send(embed=header)

        if not staff_ids:
            await ch.send(embed=discord.Embed(
                description="No moderation activity recorded last week.",
                color=0x95A5A6,
            ))
            return

        for uid in staff_ids:
            try:
                member = ch.guild.get_member(uid) or await ch.guild.fetch_member(uid)
            except discord.NotFound:
                continue

            actions      = week_data.get(str(uid), {})
            total        = sum(actions.values())
            rank         = self._member_rank(member)
            rank_display = f"{RANK_EMOJIS.get(rank, '')} {rank}" if rank else "Staff"

            # Build per-action breakdown — show all tracked action types with their counts
            breakdown_lines = []
            for a in ALL_ACTIONS:
                count = actions.get(a, 0)
                breakdown_lines.append(
                    f"{ACTION_ICONS[a]} **{ACTION_LABELS[a]}:** {count}"
                )

            embed = discord.Embed(
                title=f"Staff Activity — {member.display_name}",
                color=0x5865F2 if total > 0 else 0x95A5A6,
                timestamp=now,
            )
            embed.set_author(
                name=f"{member.display_name}  ·  {rank_display}",
                icon_url=member.display_avatar.url,
            )
            embed.add_field(name="👤  Staff Member",  value=member.mention,   inline=True)
            embed.add_field(name="🏅  Rank",          value=rank_display,     inline=True)
            embed.add_field(name="📈  Total Actions", value=str(total),       inline=True)
            embed.add_field(
                name="📋  Actions This Week",
                value="\n".join(breakdown_lines),
                inline=False,
            )
            embed.set_footer(
                text=f"User ID: {uid}  ·  {last_monday.strftime('%d %b')} – {last_sunday.strftime('%d %b %Y')}",
                icon_url=self.bot.user.display_avatar.url,
            )
            await ch.send(embed=embed)
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------ #
    # LOA expiry checker                                                   #
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=5)
    async def check_loa_expiry(self) -> None:
        now = datetime.now(timezone.utc)
        changed = False
        for req_id, req in self._inactivity.items():
            if req.get("status") != "accepted" or req.get("notified"):
                continue
            ends_str = req.get("ends_at")
            if not ends_str:
                continue
            try:
                ends_at = datetime.fromisoformat(ends_str)
            except ValueError:
                continue
            if now < ends_at:
                continue

            uid = req.get("user_id")
            if uid:
                try:
                    user = await self.bot.fetch_user(int(uid))
                    dm = discord.Embed(
                        title="🔔  Leave of Absence Ended",
                        description=(
                            "Your approved Leave of Absence has now ended. Welcome back! "
                            "Please resume your staff duties as soon as possible."
                        ),
                        color=0x2ECC71,
                        timestamp=now,
                    )
                    dm.add_field(name="Duration", value=req.get("duration_str", "N/A"), inline=True)
                    dm.add_field(name="Reason",   value=req.get("reason", "N/A"),       inline=True)
                    dm.set_footer(text="Staff Manager · Automated Notification")
                    await user.send(embed=dm)
                except Exception:
                    pass
            req["notified"] = True
            changed = True

        if changed:
            _save(INACTIVITY_FILE, self._inactivity)

    @check_loa_expiry.before_loop
    async def _before_loa(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # LOA decision handler (called by InactivityView buttons)              #
    # ------------------------------------------------------------------ #

    async def _handle_loa_decision(
        self,
        interaction: discord.Interaction,
        request_id: str,
        *,
        accepted: bool,
    ) -> None:
        # Permission check
        allowed = self._cfg_list("STAFF_MANAGEMENT_ROLE_IDS")
        if allowed:
            member_rids = {r.id for r in getattr(interaction.user, "roles", [])}
            if not member_rids.intersection(set(allowed)):
                await interaction.response.send_message(
                    "❌ You don't have permission to handle LOA requests.", ephemeral=True
                )
                return

        req = self._inactivity.get(request_id)
        if not req:
            await interaction.response.send_message("❌ Request not found.", ephemeral=True)
            return
        if req.get("status") not in (None, "pending"):
            await interaction.response.send_message(
                f"ℹ️ This request was already **{req['status']}**.", ephemeral=True
            )
            return

        now = datetime.now(timezone.utc)
        req["status"]      = "accepted" if accepted else "declined"
        req["reviewed_by"] = str(interaction.user)
        req["reviewed_at"] = now.isoformat()

        if accepted:
            td = parse_duration(req.get("raw_duration", ""))
            ends_at = now + (td if td else timedelta(days=14))
            req["ends_at"] = ends_at.isoformat()

        _save(INACTIVITY_FILE, self._inactivity)

        # Rebuild the original embed with decision appended
        label = "✅ Accepted" if accepted else "❌ Declined"
        color = 0x2ECC71 if accepted else 0xE74C3C

        if interaction.message and interaction.message.embeds:
            old = interaction.message.embeds[0]
            new_embed = discord.Embed(
                title=old.title,
                description=old.description,
                color=color,
                timestamp=old.timestamp,
            )
            for f in old.fields:
                new_embed.add_field(name=f.name, value=f.value, inline=f.inline)
            new_embed.add_field(
                name="📋  Decision",
                value=f"{label} by {interaction.user.mention}\n{ts(now, 'F')}",
                inline=False,
            )
            if accepted and req.get("ends_at"):
                new_embed.add_field(
                    name="⏰  LOA Ends",
                    value=ts(datetime.fromisoformat(req["ends_at"]), "F"),
                    inline=False,
                )
            if old.footer.text:
                new_embed.set_footer(text=old.footer.text, icon_url=old.footer.icon_url)
            if old.author.name:
                new_embed.set_author(name=old.author.name, icon_url=old.author.icon_url)
            await interaction.message.edit(embed=new_embed, view=None)

        await interaction.response.send_message(
            f"{label} by {interaction.user.mention}.", ephemeral=True
        )

        # DM the requester
        uid = req.get("user_id")
        if uid:
            try:
                user = await self.bot.fetch_user(int(uid))
                dm = discord.Embed(
                    title="✅  LOA Approved" if accepted else "❌  LOA Declined",
                    color=color,
                    timestamp=now,
                )
                if not accepted:
                    dm.description = (
                        "Your Leave of Absence request was not approved at this time. "
                        "Please contact Staff Management if you have questions."
                    )
                dm.add_field(name="Reviewed by", value=str(interaction.user), inline=True)
                dm.add_field(name="Duration",    value=req.get("duration_str", "N/A"), inline=True)
                dm.add_field(name="Reason",      value=req.get("reason", "N/A"), inline=False)
                if accepted and req.get("ends_at"):
                    dm.add_field(
                        name="Your LOA ends",
                        value=ts(datetime.fromisoformat(req["ends_at"]), "F"),
                        inline=False,
                    )
                dm.set_footer(text="Staff Manager · Automated Notification")
                await user.send(embed=dm)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Promotion decision handler (called by PromotionView buttons)         #
    # ------------------------------------------------------------------ #

    async def _handle_promotion_decision(
        self,
        interaction: discord.Interaction,
        request_id: str,
        *,
        accepted: bool,
    ) -> None:
        allowed = self._cfg_list("HIGH_RANK_ROLE_IDS")
        if allowed:
            member_rids = {r.id for r in getattr(interaction.user, "roles", [])}
            if not member_rids.intersection(set(allowed)):
                await interaction.response.send_message(
                    "❌ You don't have permission to handle promotion requests.", ephemeral=True
                )
                return

        req = self._pending_promotions.get(request_id)
        if not req:
            await interaction.response.send_message("❌ Request not found.", ephemeral=True)
            return
        if req.get("status") not in (None, "pending"):
            await interaction.response.send_message(
                f"ℹ️ This request was already **{req['status']}**.", ephemeral=True
            )
            return

        now = datetime.now(timezone.utc)
        req["status"]      = "accepted" if accepted else "declined"
        req["reviewed_by"] = str(interaction.user)
        req["reviewed_at"] = now.isoformat()
        _save(PROMOTIONS_FILE, self._pending_promotions)

        label = "✅ Accepted" if accepted else "❌ Declined"
        color = 0x2ECC71 if accepted else 0xE74C3C

        # Rebuild the original embed with decision appended
        if interaction.message and interaction.message.embeds:
            old = interaction.message.embeds[0]
            new_embed = discord.Embed(
                title=old.title,
                description=old.description,
                color=color,
                timestamp=old.timestamp,
            )
            for f in old.fields:
                new_embed.add_field(name=f.name, value=f.value, inline=f.inline)
            new_embed.add_field(
                name="📋  Decision",
                value=f"{label} by {interaction.user.mention}\n{ts(now, 'F')}",
                inline=False,
            )
            if old.footer.text:
                new_embed.set_footer(text=old.footer.text, icon_url=old.footer.icon_url)
            if old.author.name:
                new_embed.set_author(name=old.author.name, icon_url=old.author.icon_url)
            if old.thumbnail.url:
                new_embed.set_thumbnail(url=old.thumbnail.url)
            await interaction.message.edit(embed=new_embed, view=None)

        await interaction.response.send_message(
            f"{label} by {interaction.user.mention}.", ephemeral=True
        )

        uid           = req.get("user_id")
        current_rank  = req.get("current_rank", "")
        desired_rank  = req.get("desired_rank", "")

        # DM requester
        if uid:
            try:
                user = await self.bot.fetch_user(int(uid))
                dm = discord.Embed(
                    title="🎉  Promotion Approved!" if accepted else "📋  Promotion Declined",
                    color=color,
                    timestamp=now,
                )
                if accepted:
                    dm.description = (
                        f"Congratulations! Your promotion to "
                        f"**{RANK_EMOJIS.get(desired_rank, '')} {desired_rank}** has been approved. "
                        f"Keep up the excellent work!"
                    )
                else:
                    dm.description = (
                        f"Your promotion request from **{current_rank}** to **{desired_rank}** "
                        f"was not approved at this time. "
                        f"Please continue working hard and feel free to reach out to Staff Management."
                    )
                dm.add_field(name="Reviewed by",   value=str(interaction.user), inline=True)
                dm.add_field(name="Current Rank",  value=current_rank,          inline=True)
                if accepted:
                    dm.add_field(
                        name="New Rank",
                        value=f"{RANK_EMOJIS.get(desired_rank, '')} {desired_rank}",
                        inline=True,
                    )
                dm.set_footer(text="Staff Manager · Automated Notification")
                await user.send(embed=dm)
            except Exception:
                pass

        # Post to promotion-log
        if accepted:
            log_ch = self._channel("PROMOTION_LOG_CHANNEL")
            if log_ch:
                target = log_ch.guild.get_member(int(uid)) if uid else None
                promo  = discord.Embed(title="🎊  Staff Promotion", color=0x2ECC71, timestamp=now)
                if target:
                    promo.set_author(name=str(target), icon_url=target.display_avatar.url)
                promo.add_field(name="Staff Member",  value=f"<@{uid}>" if uid else "Unknown", inline=True)
                promo.add_field(name="Previous Rank", value=f"{RANK_EMOJIS.get(current_rank, '')} {current_rank}", inline=True)
                promo.add_field(name="New Rank",      value=f"{RANK_EMOJIS.get(desired_rank, '')} {desired_rank}", inline=True)
                promo.add_field(name="Approved by",   value=interaction.user.mention, inline=True)
                promo.add_field(name="Date",          value=ts(now, "F"),             inline=True)
                promo.set_footer(text=f"User ID: {uid}", icon_url=self.bot.user.display_avatar.url)
                await log_ch.send(embed=promo)

    # ------------------------------------------------------------------ #
    # Commands                                                             #
    # ------------------------------------------------------------------ #

    @commands.command(name="inactivityreq", aliases=["loa", "loareq"])
    async def inactivity_request(
        self, ctx: commands.Context, duration: str, *, reason: str
    ) -> None:
        """
        Submit a Leave of Absence request.
        Usage: !inactivityreq <duration> <reason>
        Examples:
          !inactivityreq 5d I'm sick
          !inactivityreq 2w Family emergency
          !inactivityreq 1d12h Mental health break
        """
        rank = self._member_rank(ctx.author)  # type: ignore[arg-type]
        if not rank:
            await ctx.send(
                embed=discord.Embed(description="❌ You must be a staff member to use this command.", color=0xE74C3C),
                delete_after=10,
            )
            return

        td = parse_duration(duration)
        if td is None:
            await ctx.send(
                embed=discord.Embed(
                    description=(
                        "❌ Invalid duration. Examples: `5d`, `2h`, `30m`, `1w`, `1d12h`\n"
                        "**w** = weeks · **d** = days · **h** = hours · **m** = minutes"
                    ),
                    color=0xE74C3C,
                ),
                delete_after=15,
            )
            return

        if td > timedelta(days=14):
            await ctx.send(
                embed=discord.Embed(description="❌ Maximum LOA duration is **14 days**.", color=0xE74C3C),
                delete_after=10,
            )
            return

        ch = self._channel("INACTIVITY_CHANNEL")
        if not ch:
            await ctx.send(
                embed=discord.Embed(description="❌ Inactivity channel is not configured.", color=0xE74C3C),
                delete_after=10,
            )
            return

        now          = datetime.now(timezone.utc)
        duration_str = format_duration(td)
        request_id   = f"{ctx.author.id}_{int(now.timestamp())}"

        embed = discord.Embed(title="📋  Leave of Absence Request", color=0xF1C40F, timestamp=now)
        embed.set_author(
            name=f"{ctx.author.display_name}  ·  {rank}",
            icon_url=ctx.author.display_avatar.url,
        )
        embed.add_field(name="🛡️  Staff Member",       value=f"{ctx.author.mention}\n`{ctx.author}`", inline=True)
        embed.add_field(name="🏅  Current Rank",        value=f"{RANK_EMOJIS.get(rank, '')} {rank}",  inline=True)
        embed.add_field(name="⏱️  Requested Duration",  value=f"**{duration_str}**",                  inline=True)
        embed.add_field(name="📄  Reason",              value=reason,                                  inline=False)
        embed.add_field(name="🕐  Submitted",           value=ts(now, "F"),                            inline=True)
        embed.add_field(name="📌  Status",              value="⏳ Pending Review",                     inline=True)
        embed.set_footer(
            text=f"User ID: {ctx.author.id}  ·  Request ID: {request_id}",
            icon_url=self.bot.user.display_avatar.url,
        )

        view = InactivityView(self, request_id)
        msg  = await ch.send(embed=embed, view=view)

        self._inactivity[request_id] = {
            "user_id":      str(ctx.author.id),
            "raw_duration": duration,
            "duration_str": duration_str,
            "reason":       reason,
            "status":       "pending",
            "message_id":   str(msg.id),
            "channel_id":   str(ch.id),
        }
        _save(INACTIVITY_FILE, self._inactivity)

        await ctx.message.add_reaction("✅")
        try:
            conf = discord.Embed(
                title="✅  LOA Request Submitted",
                description=(
                    f"Your request has been submitted to {ch.mention}.\n"
                    "You will receive a DM once it has been reviewed."
                ),
                color=0x2ECC71,
                timestamp=now,
            )
            conf.add_field(name="Duration", value=duration_str, inline=True)
            conf.add_field(name="Reason",   value=reason,       inline=True)
            conf.set_footer(text="Staff Manager · Automated Notification")
            await ctx.author.send(embed=conf)
        except discord.Forbidden:
            pass

    @commands.command(name="promotionreq", aliases=["promote", "promoapp"])
    async def promotion_request(self, ctx: commands.Context, *, reason: str) -> None:
        """
        Submit a promotion request.
        Usage: !promotionreq <reason>
        Example: !promotionreq I have met all requirements and been active consistently.
        """
        rank = self._member_rank(ctx.author)  # type: ignore[arg-type]
        if not rank:
            await ctx.send(
                embed=discord.Embed(description="❌ You must be a staff member to use this command.", color=0xE74C3C),
                delete_after=10,
            )
            return

        ineligible = ["Staff Management", "Head of Staff", "Admin", "Head Admin"]
        if rank in ineligible:
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ **{rank}** members are not eligible to submit promotion requests.",
                    color=0xE74C3C,
                ),
                delete_after=10,
            )
            return

        desired_rank = self._next_rank(rank)
        if not desired_rank:
            await ctx.send(
                embed=discord.Embed(description="❌ You are already at the highest promotable rank.", color=0xE74C3C),
                delete_after=10,
            )
            return

        ch = self._channel("PROMOTION_REQUEST_CHANNEL")
        if not ch:
            await ctx.send(
                embed=discord.Embed(description="❌ Promotion request channel is not configured.", color=0xE74C3C),
                delete_after=10,
            )
            return

        totals        = self._lifetime_totals(ctx.author.id)
        time_in_rank  = self._time_in_rank(ctx.author.id, rank)
        time_str      = format_duration(time_in_rank) if time_in_rank else "Unknown (role not yet tracked)"
        now           = datetime.now(timezone.utc)
        request_id    = f"{ctx.author.id}_{int(now.timestamp())}"

        achievement_lines = [
            f"{ACTION_ICONS[a]} {ACTION_LABELS[a]}: **{totals[a]}**"
            for a in ALL_ACTIONS if totals.get(a, 0)
        ] or ["*No tracked mod actions yet.*"]

        ping_text = " ".join(f"<@&{rid}>" for rid in self._cfg_list("HIGH_RANK_ROLE_IDS"))

        embed = discord.Embed(title="🌟  Promotion Request", color=0xF1C40F, timestamp=now)
        embed.set_author(
            name=f"{ctx.author.display_name}  ·  {rank}",
            icon_url=ctx.author.display_avatar.url,
        )
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        embed.add_field(name="👤  Staff Name",            value=f"{ctx.author.mention}\n`{ctx.author}`",        inline=True)
        embed.add_field(name="🏅  Current Rank",           value=f"{RANK_EMOJIS.get(rank, '')} {rank}",         inline=True)
        embed.add_field(name="🎯  Desired Rank",           value=f"{RANK_EMOJIS.get(desired_rank, '')} {desired_rank}", inline=True)
        embed.add_field(name="⏳  Time in Current Rank",   value=time_str,                                       inline=False)
        embed.add_field(name="🏆  Achievements / Evidence",value="\n".join(achievement_lines),                  inline=False)
        embed.add_field(name="📝  Reason for Promotion",   value=reason,                                         inline=False)
        embed.add_field(name="🕐  Timestamp",              value=ts(now, "F"),                                   inline=True)
        embed.add_field(name="📌  Status",                 value="⏳ Pending Review",                            inline=True)
        embed.set_footer(
            text=f"User ID: {ctx.author.id}  ·  Request ID: {request_id}",
            icon_url=self.bot.user.display_avatar.url,
        )

        view    = PromotionView(self, request_id)
        content = ping_text or None
        msg     = await ch.send(content=content, embed=embed, view=view)

        self._pending_promotions[request_id] = {
            "user_id":      str(ctx.author.id),
            "current_rank": rank,
            "desired_rank": desired_rank,
            "reason":       reason,
            "status":       "pending",
            "message_id":   str(msg.id),
            "channel_id":   str(ch.id),
        }
        _save(PROMOTIONS_FILE, self._pending_promotions)

        await ctx.message.add_reaction("✅")
        try:
            conf = discord.Embed(
                title="✅  Promotion Request Submitted",
                description=(
                    f"Your request has been submitted to {ch.mention}.\n"
                    "You will receive a DM once it has been reviewed."
                ),
                color=0x2ECC71,
                timestamp=now,
            )
            conf.add_field(name="Current Rank", value=rank,         inline=True)
            conf.add_field(name="Desired Rank", value=desired_rank, inline=True)
            conf.set_footer(text="Staff Manager · Automated Notification")
            await ctx.author.send(embed=conf)
        except discord.Forbidden:
            pass

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
        Manually log a mod action (for bots other than Dyno).
        Usage: !modlog <action> <@user> [reason]
        Actions: warn mute unmute kick ban unban softban deafen undeafen voicemute voiceunmute note
        """
        action = action.lower()
        if action not in ACTION_COLORS:
            valid = " · ".join(ACTION_COLORS.keys())
            await ctx.send(
                embed=discord.Embed(description=f"❌ Unknown action. Valid actions:\n`{valid}`", color=0xE74C3C),
                delete_after=10,
            )
            return
        ch = self._channel("MOD_ACTION_LOG_CHANNEL")
        if not ch:
            await ctx.send(
                embed=discord.Embed(description="❌ Mod action log channel not configured.", color=0xE74C3C),
                delete_after=10,
            )
            return
        data = {
            "action":    action,
            "user_id":   user.id,
            "user_tag":  str(user),
            "moderator": str(ctx.author),
            "reason":    reason,
        }
        await self._post_mod_action(ch, data, ctx.message)
        self._record_action(ctx.author.id, action)
        await ctx.message.add_reaction("✅")

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
        target = member or ctx.author  # type: ignore[assignment]
        totals = self._lifetime_totals(target.id)
        rank   = self._member_rank(target)  # type: ignore[arg-type]
        now    = datetime.now(timezone.utc)

        lines = [
            f"{ACTION_ICONS[a]} **{ACTION_LABELS[a]}:** {totals[a]}"
            for a in ALL_ACTIONS if totals.get(a, 0)
        ] or ["*No actions tracked yet.*"]

        embed = discord.Embed(
            title=f"📊  Staff Statistics — {target.display_name}",
            color=0x5865F2,
            timestamp=now,
        )
        embed.set_author(name=str(target), icon_url=target.display_avatar.url)
        embed.add_field(name="Rank",          value=f"{RANK_EMOJIS.get(rank, '')} {rank}" if rank else "Not staff", inline=True)
        embed.add_field(name="Total Actions", value=str(sum(totals.values())),                                        inline=True)
        embed.add_field(name="All-Time Breakdown", value="\n".join(lines), inline=False)

        if rank:
            tir = self._time_in_rank(target.id, rank)
            if tir:
                embed.add_field(name=f"Time as {rank}", value=format_duration(tir), inline=False)

        embed.set_footer(text=f"User ID: {target.id}", icon_url=self.bot.user.display_avatar.url)
        await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffManagerCog(bot))
