"""
TTS Voice Plugin for ModMail
=============================
Commands:
  .talk <text>          — Join your VC and say the text (queued, never cuts off)
  .talkleave            — Leave the voice channel and clear the queue
  .talkvoice <voice>    — Change TTS voice (see VOICES list below)
  .talkvoices           — List available voices

Requirements (install before loading):
  pip install edge-tts PyNaCl
  System: ffmpeg must be installed and on PATH

Place this file in:
  plugins/@local/tts/tts.py
"""

import asyncio
import logging
import os
import tempfile

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel

logger = logging.getLogger("Modmail")

# ============================================================
# CONFIG
# ============================================================
# Default voice — Microsoft neural voices are very natural/human-sounding.
# Run `.talkvoices` in Discord to see all available options.
DEFAULT_VOICE = "en-US-GuyNeural"

# How long (seconds) the bot stays idle in VC before auto-disconnecting.
IDLE_TIMEOUT = 300  # 5 minutes

# Minimum permission level required to use .talk commands.
REQUIRED_LEVEL = PermissionLevel.ADMINISTRATOR
# ============================================================

# Curated list of natural-sounding voices shown by .talkvoices
VOICES = {
    "guy":     "en-US-GuyNeural",       # US male, conversational
    "aria":    "en-US-AriaNeural",      # US female, conversational
    "jenny":   "en-US-JennyNeural",     # US female, friendly
    "sonia":   "en-GB-SoniaNeural",     # British female
    "ryan":    "en-GB-RyanNeural",      # British male
    "natasha": "en-AU-NatashaNeural",   # Australian female
    "william": "en-AU-WilliamNeural",   # Australian male
    "clara":   "en-CA-ClaraNeural",     # Canadian female
    "liam":    "en-CA-LiamNeural",      # Canadian male
}


class TTSPlugin(commands.Cog):
    """Join voice channels and speak text using natural-sounding neural TTS."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Per-guild state
        self._voice_clients: dict[int, discord.VoiceClient] = {}
        self._queues: dict[int, asyncio.Queue] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._voices: dict[int, str] = {}  # guild_id -> voice name

    # ------------------------------------------------------------------ helpers

    def _voice_for(self, guild_id: int) -> str:
        return self._voices.get(guild_id, DEFAULT_VOICE)

    async def _join_or_move(self, ctx: commands.Context) -> discord.VoiceClient | None:
        """Return a connected VoiceClient for the author's current channel."""
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send(embed=discord.Embed(
                description="❌ You need to be in a voice channel first.",
                color=discord.Color.red(),
            ))
            return None

        target = ctx.author.voice.channel
        guild_id = ctx.guild.id
        vc = self._voice_clients.get(guild_id)

        try:
            if vc and vc.is_connected():
                if vc.channel.id != target.id:
                    await vc.move_to(target)
            else:
                vc = await target.connect()
                self._voice_clients[guild_id] = vc
        except Exception as e:
            logger.error(f"VC connect/move error: {e}", exc_info=True)
            await ctx.send(embed=discord.Embed(
                description=f"❌ Could not connect to voice: `{e}`",
                color=discord.Color.red(),
            ))
            return None

        return vc

    async def _generate_audio(self, text: str, voice: str) -> str:
        """
        Generate TTS audio with edge-tts and save to a temp MP3 file.
        Returns the file path (caller is responsible for deleting it).
        """
        try:
            import edge_tts  # imported lazily so the plugin loads even if not installed
        except ImportError:
            raise RuntimeError(
                "edge-tts is not installed. Run `pip install edge-tts` on the bot server."
            )

        communicate = edge_tts.Communicate(text, voice)
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        await communicate.save(tmp.name)
        return tmp.name

    async def _worker(self, guild_id: int) -> None:
        """
        Per-guild queue worker. Plays TTS items one at a time.
        Auto-disconnects after IDLE_TIMEOUT seconds of inactivity.
        """
        queue = self._queues[guild_id]

        while True:
            # Wait for the next item, auto-disconnect on timeout
            try:
                text, voice = await asyncio.wait_for(queue.get(), timeout=IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.info(f"TTS idle timeout in guild {guild_id}, disconnecting.")
                await self._cleanup(guild_id)
                return
            except asyncio.CancelledError:
                return

            vc = self._voice_clients.get(guild_id)
            if not vc or not vc.is_connected():
                queue.task_done()
                continue

            tmp_path = None
            try:
                tmp_path = await self._generate_audio(text, voice)

                finished = asyncio.Event()

                def _after(err: Exception | None) -> None:
                    if err:
                        logger.error(f"FFmpeg playback error in guild {guild_id}: {err}")
                    finished.set()

                vc.play(discord.FFmpegPCMAudio(tmp_path), after=_after)
                await finished.wait()

            except RuntimeError as e:
                # edge-tts not installed — log and give up on this item
                logger.error(str(e))
            except Exception as e:
                logger.error(f"TTS playback error in guild {guild_id}: {e}", exc_info=True)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                queue.task_done()

    async def _cleanup(self, guild_id: int) -> None:
        """Disconnect from VC and cancel the worker for a guild."""
        vc = self._voice_clients.pop(guild_id, None)
        if vc:
            try:
                if vc.is_playing():
                    vc.stop()
                if vc.is_connected():
                    await vc.disconnect()
            except Exception:
                pass

        task = self._workers.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

        self._queues.pop(guild_id, None)

    def _ensure_worker(self, guild_id: int) -> None:
        """Start the queue worker for a guild if it isn't running."""
        if guild_id not in self._queues:
            self._queues[guild_id] = asyncio.Queue()

        existing = self._workers.get(guild_id)
        if not existing or existing.done():
            self._workers[guild_id] = asyncio.create_task(self._worker(guild_id))

    # ------------------------------------------------------------------ commands

    @commands.command(name="talkjoin")
    @checks.has_permissions(REQUIRED_LEVEL)
    async def talkjoin(self, ctx: commands.Context, *, channel: discord.VoiceChannel) -> None:
        """
        Join a specific voice channel by name, mention, or ID.
        After joining, use .talk as normal.

        Usage:
          .talkjoin General
          .talkjoin 1234567890123456
          .talkjoin #General
        """
        guild_id = ctx.guild.id
        vc = self._voice_clients.get(guild_id)

        try:
            if vc and vc.is_connected():
                await vc.move_to(channel)
            else:
                vc = await channel.connect()
                self._voice_clients[guild_id] = vc
        except Exception as e:
            return await ctx.send(embed=discord.Embed(
                description=f"❌ Could not join **{channel.name}**: `{e}`",
                color=discord.Color.red(),
            ))

        self._ensure_worker(guild_id)
        await ctx.send(embed=discord.Embed(
            description=f"✅ Joined **{channel.name}**. Use `.talk <text>` to speak.",
            color=discord.Color.green(),
        ))

    @commands.command(name="talk")
    @checks.has_permissions(REQUIRED_LEVEL)
    async def talk(self, ctx: commands.Context, *, text: str) -> None:
        """
        Speak the given text in the current voice channel.
        If the bot isn't in a VC yet, it joins yours automatically.
        Text is queued — multiple .talk commands play in order.

        Usage: .talk Hello, how is everyone doing today?
        """
        # If already in a VC in this guild, use it directly (don't force-follow the user)
        guild_id = ctx.guild.id
        vc = self._voice_clients.get(guild_id)
        if not vc or not vc.is_connected():
            # Not in any VC — join the author's channel
            vc = await self._join_or_move(ctx)
            if not vc:
                return

        self._ensure_worker(guild_id)

        voice = self._voice_for(guild_id)
        await self._queues[guild_id].put((text, voice))

        # React to confirm the item was queued
        try:
            await ctx.message.add_reaction("🔊")
        except discord.HTTPException:
            pass

    @commands.command(name="talkleave", aliases=["talkstop", "talkdisconnect"])
    @checks.has_permissions(REQUIRED_LEVEL)
    async def talkleave(self, ctx: commands.Context) -> None:
        """
        Stop speaking and leave the voice channel.

        Usage: .talkleave
        """
        guild_id = ctx.guild.id

        if guild_id not in self._voice_clients:
            return await ctx.send(embed=discord.Embed(
                description="❌ I'm not in a voice channel.",
                color=discord.Color.red(),
            ))

        await self._cleanup(guild_id)

        try:
            await ctx.message.add_reaction("👋")
        except discord.HTTPException:
            pass

    @commands.command(name="talkvoice")
    @checks.has_permissions(REQUIRED_LEVEL)
    async def talkvoice(self, ctx: commands.Context, *, name: str) -> None:
        """
        Change the TTS voice for this server.
        Run .talkvoices to see the available options.

        Usage: .talkvoice aria
        """
        name = name.strip().lower()

        # Accept either a shorthand (e.g. "aria") or a full name (e.g. "en-US-AriaNeural")
        if name in VOICES:
            full_name = VOICES[name]
        elif name in VOICES.values():
            full_name = name
        else:
            options = ", ".join(f"`{k}`" for k in VOICES)
            return await ctx.send(embed=discord.Embed(
                description=f"❌ Unknown voice `{name}`.\nAvailable: {options}",
                color=discord.Color.red(),
            ))

        self._voices[ctx.guild.id] = full_name
        await ctx.send(embed=discord.Embed(
            description=f"✅ Voice set to **{full_name}**.",
            color=discord.Color.green(),
        ))

    @commands.command(name="talkvoices")
    @checks.has_permissions(REQUIRED_LEVEL)
    async def talkvoices(self, ctx: commands.Context) -> None:
        """
        List all available TTS voices.

        Usage: .talkvoices
        """
        current = self._voice_for(ctx.guild.id)
        lines = []
        for short, full in VOICES.items():
            marker = " ◀ current" if full == current else ""
            lines.append(f"`{short:<10}` — {full}{marker}")

        embed = discord.Embed(
            title="🎙️ Available TTS Voices",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Change with: .talkvoice <name>")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ cleanup on unload

    def cog_unload(self) -> None:
        """Disconnect from all VCs and cancel all workers when the cog is unloaded."""
        for guild_id in list(self._voice_clients.keys()):
            asyncio.create_task(self._cleanup(guild_id))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TTSPlugin(bot))
