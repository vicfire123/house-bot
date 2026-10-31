"""Soundboard module: play the server's own Discord soundboard sounds.

Spec (locked 2026-10-31, Victor's calls):
  - Use the CURRENT Discord soundboard only - the guild sounds already
    uploaded in Server Settings > Soundboard, plus Discord's defaults.
    No local mp3 files, no separate starter pack, no ffmpeg.
  - !sfx <name> plays it, !sfx list (or bare !sfx) lists what exists.
  - The requester must be in a voice channel.
  - Cooldown: 10 seconds per user.
  - Sounds play through Discord's native VoiceChannel.send_sound
    (discord.py 2.5+; the bot runs 2.7.1), which is a voice effect,
    NOT the music cog's audio stream - music keeps playing over it.

Adding a sound later = upload it in Discord (Server Settings >
Soundboard, MP3 up to 512kb / 5 seconds). This cog re-fetches the
inventory on every call, so new sounds just show up - no redeploy.
"""

import logging

import discord
from discord.ext import commands

log = logging.getLogger("housebot.soundboard")


class Soundboard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _inventory(self, guild: discord.Guild):
        """(guild sounds, default sounds) - fresh fetch, cache fallback."""
        guild_sounds = []
        default_sounds = []
        try:
            guild_sounds = await guild.fetch_soundboard_sounds()
        except Exception:
            log.exception("guild soundboard fetch failed - using cache")
            guild_sounds = list(guild.soundboard_sounds)
        try:
            default_sounds = await self.bot.fetch_soundboard_default_sounds()
        except Exception:
            log.exception("default soundboard fetch failed")
        return guild_sounds, default_sounds

    async def _list_text(self, guild: discord.Guild) -> str:
        guild_sounds, default_sounds = await self._inventory(guild)
        lines = []
        if guild_sounds:
            lines.append("**Server sounds:**")
            for s in guild_sounds:
                emoji = s.emoji_name or "🔊"
                lines.append(f"{emoji} {s.name}")
        if default_sounds:
            lines.append("**Discord defaults:**")
            for s in default_sounds:
                emoji = getattr(s, "emoji_name", None) or "🔊"
                lines.append(f"{emoji} {s.name}")
        if not lines:
            return ("No soundboard sounds found. Add some in "
                    "Server Settings > Soundboard, then run `!sfx list` again.")
        lines.append("\nPlay one with `!sfx <name>`")
        return "\n".join(lines)

    @commands.command(name="sfx")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def sfx(self, ctx: commands.Context, *, name: str = ""):
        """!sfx <sound name> - play a soundboard sound. !sfx list - see them."""
        name = name.strip()
        if not name or name.lower() == "list":
            # Listing should not burn the play cooldown.
            ctx.command.reset_cooldown(ctx)
            await ctx.send(await self._list_text(ctx.guild))
            return

        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("Join a voice channel first, then !sfx.")
            return
        channel = ctx.author.voice.channel

        guild_sounds, default_sounds = await self._inventory(ctx.guild)
        all_sounds = list(guild_sounds) + list(default_sounds)
        wanted = name.lower()
        sound = next((s for s in all_sounds if s.name.lower() == wanted), None)
        if sound is None:
            partial = [s for s in all_sounds if wanted in s.name.lower()]
            if len(partial) == 1:
                sound = partial[0]
        if sound is None:
            names = ", ".join(s.name for s in all_sounds[:20]) or "none"
            await ctx.send(f"No soundboard sound called **{name}**. "
                           f"Available: {names} - full list with `!sfx list`.")
            return

        # The bot itself has to sit in that voice channel to send a
        # soundboard sound (Discord requires the sender to be connected,
        # not muted/suppressed). Music shares this same voice client,
        # so a soundboard hit never restarts or steals the music stream.
        vc = ctx.guild.voice_client
        try:
            if vc is None:
                vc = await channel.connect(reconnect=True)
            elif vc.channel != channel:
                if vc.is_playing():
                    await ctx.send(f"I'm playing music in {vc.channel.name} - "
                                   "soundboard follows the music channel.")
                    return
                await vc.move_to(channel)
        except Exception as e:
            log.exception("soundboard voice connect failed")
            await ctx.send(f"I couldn't join {channel.name}: {e}")
            return

        try:
            await channel.send_sound(sound)
        except discord.Forbidden:
            await ctx.send("I'm missing Speak or Use Soundboard in that channel - "
                           "check the VicBot role's voice permissions.")
        except Exception as e:
            log.exception("send_sound failed")
            await ctx.send(f"Couldn't play **{sound.name}**: {e}")

    @sfx.error
    async def sfx_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Slow down - `!sfx` again in {error.retry_after:.0f}s.",
                           delete_after=5)
        else:
            log.error("sfx failed: %s", error)
            await ctx.send(f"That hit an error: {error}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Soundboard(bot))
