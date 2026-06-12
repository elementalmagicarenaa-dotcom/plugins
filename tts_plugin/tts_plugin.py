"""
TTS Voice Plugin for ModMail
=============================
Commands:
  .talk <text>          — Join your VC and say the text (queued, never cuts off)
  .talkjoin <channel>   — Join a specific VC by name or ID
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
DEFAULT_VOICE = "en-US-GuyNeural"
IDLE_TIMEOUT = 300  # seconds before auto-disconnect when idle
REQUIRED_LEVEL = PermissionLevel.ADMINISTRATOR
# ============================================================

VOICES = {
    "guy":     "en-US-GuyNeural",
    "aria":    "en-US-AriaNeural",
    "jenny":   "en-US-JennyNeural",
    "sonia":   "en-GB-SoniaNeural",
    "ryan":    "en-GB-RyanNeural",
    "natasha": "en-AU-NatashaNeural",
    "william": "en-AU-WilliamNeural",
    "clara":   "en-CA-ClaraNeural",
    "liam":    "en-CA-LiamNeural",
}


class TTSPlugin(commands.Cog):
    """Join voice channels and speak text using natural-sounding neural TTS."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._queues: dict[int, asyncio.Queue] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._voices: dict[int, str] = {}

    # ------------------------------------------------------------------ helpers

    def _voice_for(self, guild_id: int) -> str:
        return self._voices.get(guild_id, DEFAULT_VOICE)

    def _vc(self, guild: discord.Guild) -> discord.VoiceClient | None:
        """Return the bot's current VoiceClient for this guild, or None."""
        vc = guild.voice_client
        if vc and isinstance(vc, discord.VoiceClient) and vc.is_connected():
            return vc
        return None

    async def _force_disconnect(self, guild: discord.Guild) -> None:
        """Fully clear all voice state — local client and Discord gateway."""
        existing = guild.voice_client
        if existing:
            try:
                await existing.disconnect(force=True)
            except Exception:
                pass
        try:
            await guild.change_voice_state(channel=None)
        except Exception:
            pass

    async def _connect(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        """Connect to a voice channel after clearing any existing session."""
        guild = channel.guild

        # Check permissions before attempting — missing Connect/Speak is a common
        # cause of silent 4017 disconnects
        me = guild.me
        perms = channel.permissions_for(me)
        missing = []
        if not perms.connect:
            missing.append("Connect")
        if not perms.speak:
            missing.append("Speak")
        if missing:
            raise RuntimeError(
                f"Bot is missing permissions in **{channel.name}**: {', '.join(missing)}. "
                "Grant these in your server's channel settings and try again."
            )

        await self._force_disconnect(guild)
        await asyncio.sleep(1)
        return await channel.connect(reconnect=False)

    async def _generate_audio(self, text: str, voice: str) -> str:
        """Generate TTS audio, save to a temp MP3, return the path."""
        try:
            import edge_tts
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
        """Per-guild queue worker — plays items one at a time, auto-disconnects on idle."""
        queue = self._queues[guild_id]
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        while True:
            try:
                text, voice = await asyncio.wait_for(queue.get(), timeout=IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.info(f"TTS idle timeout in guild {guild_id}, disconnecting.")
                await self._cleanup(guild_id)
                return
            except asyncio.CancelledError:
                return

            vc = self._vc(guild)
            if not vc:
                queue.task_done()
                return

            tmp_path = None
            try:
                tmp_path = await self._generate_audio(text, voice)
                finished = asyncio.Event()

                def _after(err: Exception | None) -> None:
                    if err:
                        logger.error(f"FFmpeg error in guild {guild_id}: {err}")
                    finished.set()

                vc.play(discord.FFmpegPCMAudio(tmp_path), after=_after)
                await finished.wait()

            except RuntimeError as e:
                logger.error(str(e))
            except Exception as e:
                logger.error(f"TTS error in guild {guild_id}: {e}", exc_info=True)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                queue.task_done()

    async def _cleanup(self, guild_id: int) -> None:
        """Disconnect from VC and stop the worker."""
        task = self._workers.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

        self._queues.pop(guild_id, None)

        guild = self.bot.get_guild(guild_id)
        if guild:
            vc = guild.voice_client
            if vc:
                try:
                    if vc.is_playing():
                        vc.stop()
                    await vc.disconnect(force=True)
                except Exception:
                    pass

    def _ensure_worker(self, guild_id: int) -> None:
        if guild_id not in self._queues:
            self._queues[guild_id] = asyncio.Queue()
        existing = self._workers.get(guild_id)
        if not existing or existing.done():
            self._workers[guild_id] = asyncio.create_task(self._worker(guild_id))

    # ------------------------------------------------------------------ commands

    @commands.command(name="talkcheck")
    @checks.has_permissions(REQUIRED_LEVEL)
    async def talkcheck(self, ctx: commands.Context) -> None:
        """Run a full diagnostics check for TTS requirements. Usage: .talkcheck"""
        lines = []

        # 1. PyNaCl
        try:
            import nacl.secret  # noqa: F401
            import nacl
            lines.append(f"✅ PyNaCl {nacl.__version__} installed")
        except ImportError:
            lines.append("❌ PyNaCl NOT installed — run `pip install PyNaCl` on the bot server")

        # 2. edge-tts
        try:
            import edge_tts  # noqa: F401
            lines.append("✅ edge-tts installed")
        except ImportError:
            lines.append("❌ edge-tts NOT installed — run `pip install edge-tts`")

        # 3. ffmpeg
        import shutil
        if shutil.which("ffmpeg"):
            lines.append("✅ ffmpeg found on PATH")
        else:
            lines.append("❌ ffmpeg NOT found — install it (`apt install ffmpeg` on Linux)")

        # 4. discord.py version
        lines.append(f"ℹ️ discord.py {discord.__version__}")

        # 5. Bot voice permissions in this guild (check every VC)
        no_perm_channels = []
        for vc_channel in ctx.guild.voice_channels:
            perms = vc_channel.permissions_for(ctx.guild.me)
            if not perms.connect or not perms.speak:
                no_perm_channels.append(vc_channel.name)
        if no_perm_channels:
            lines.append(f"❌ Missing Connect/Speak in: {', '.join(no_perm_channels)}")
        else:
            lines.append("✅ Bot has Connect + Speak in all voice channels")

        # 6. Quick edge-tts network test (generate 1 word)
        try:
            import edge_tts as _et
            import tempfile, os as _os
            _tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            _tmp.close()
            await _et.Communicate("test", DEFAULT_VOICE).save(_tmp.name)
            size = _os.path.getsize(_tmp.name)
            _os.unlink(_tmp.name)
            if size > 0:
                lines.append("✅ edge-tts network reachable (generated audio successfully)")
            else:
                lines.append("⚠️ edge-tts returned empty audio — check network/firewall")
        except Exception as e:
            lines.append(f"❌ edge-tts network test failed: `{e}`")

        embed = discord.Embed(
            title="🔍 TTS Diagnostics",
            description="\n".join(lines),
            color=discord.Color.green() if all(l.startswith("✅") for l in lines) else discord.Color.orange(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="talkjoin")
    @checks.has_permissions(REQUIRED_LEVEL)
    async def talkjoin(self, ctx: commands.Context, *, channel: discord.VoiceChannel) -> None:
        """
        Join a specific voice channel by name, mention, or ID.

        Usage:
          .talkjoin General
          .talkjoin 1234567890123456
        """
        try:
            await self._connect(channel)
        except Exception as e:
            return await ctx.send(embed=discord.Embed(
                description=f"❌ Could not join **{channel.name}**: `{e}`",
                color=discord.Color.red(),
            ))

        self._ensure_worker(ctx.guild.id)
        await ctx.send(embed=discord.Embed(
            description=f"✅ Joined **{channel.name}**. Use `.talk <text>` to speak.",
            color=discord.Color.green(),
        ))

    @commands.command(name="talk")
    @checks.has_permissions(REQUIRED_LEVEL)
    async def talk(self, ctx: commands.Context, *, text: str) -> None:
        """
        Speak text in the current voice channel.
        Joins your VC automatically if not already connected.
        Messages are queued and play in order.

        Usage: .talk Hello, how is everyone?
        """
        guild_id = ctx.guild.id

        if not self._vc(ctx.guild):
            if not ctx.author.voice or not ctx.author.voice.channel:
                return await ctx.send(embed=discord.Embed(
                    description="❌ You need to be in a voice channel first.",
                    color=discord.Color.red(),
                ))
            try:
                await self._connect(ctx.author.voice.channel)
            except Exception as e:
                return await ctx.send(embed=discord.Embed(
                    description=f"❌ Could not connect to voice: `{e}`",
                    color=discord.Color.red(),
                ))

        self._ensure_worker(guild_id)
        await self._queues[guild_id].put((text, self._voice_for(guild_id)))

        try:
            await ctx.message.add_reaction("🔊")
        except discord.HTTPException:
            pass

    @commands.command(name="talkleave", aliases=["talkstop", "talkdisconnect"])
    @checks.has_permissions(REQUIRED_LEVEL)
    async def talkleave(self, ctx: commands.Context) -> None:
        """Leave the voice channel and clear the queue. Usage: .talkleave"""
        if not self._vc(ctx.guild) and ctx.guild.id not in self._workers:
            return await ctx.send(embed=discord.Embed(
                description="❌ I'm not in a voice channel.",
                color=discord.Color.red(),
            ))

        await self._cleanup(ctx.guild.id)

        try:
            await ctx.message.add_reaction("👋")
        except discord.HTTPException:
            pass

    @commands.command(name="talkvoice")
    @checks.has_permissions(REQUIRED_LEVEL)
    async def talkvoice(self, ctx: commands.Context, *, name: str) -> None:
        """
        Change the TTS voice. Run .talkvoices to see options.

        Usage: .talkvoice aria
        """
        name = name.strip().lower()
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
        """List all available TTS voices. Usage: .talkvoices"""
        current = self._voice_for(ctx.guild.id)
        lines = [
            f"`{short:<10}` — {full}{' ◀ current' if full == current else ''}"
            for short, full in VOICES.items()
        ]
        embed = discord.Embed(
            title="🎙️ Available TTS Voices",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Change with: .talkvoice <name>")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ cleanup on unload

    def cog_unload(self) -> None:
        for guild_id in list(self._workers.keys()):
            asyncio.create_task(self._cleanup(guild_id))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TTSPlugin(bot))
