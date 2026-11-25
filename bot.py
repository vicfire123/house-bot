"""
House Bot - one private bot for the friend server.

Modules:
  - music:     YouTube/SoundCloud queue player (yt-dlp + ffmpeg)
  - admin:     natural-language moderation ("kick X", "give admin to Y")
               with a hard rule: destructive actions NEVER fire without
               the requester tapping Confirm first (approval-card model).
               Only whitelisted user IDs can use it at all.
  - minecraft: !mc status from the potofgreed Paper server + presence.
  - watch:     potofgreed service health checks every 2 minutes -
               DOWN / back-up alerts in the log channel, plus !status.
  - starboard: 1 ⭐ on any human message reposts it to #starboard.
  - updates:   patch / version tracker for 17 feeds (games, NVIDIA
               drivers, devices, iOS, free games), posting to #game-updates every 3 hours,
               with per-feed health alerts in the audit channel.
  - media:     !request a show / season / episode (Sonarr) or a movie
               (Radarr). Whitelisted user IDs only.

(cogs/soundboard.py still exists in the repo but is NOT loaded -
Victor killed !sfx on 2026-10-31 after seeing the native Discord
soundboard live: the bot layer added nothing.)

Config lives in .env (see .env.example). The token never goes in code.
"""

import os
import logging

import discord
from discord.ext import commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("housebot")

TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
PREFIX = os.environ.get("COMMAND_PREFIX", "!").strip() or "!"


def _id_set(raw: str) -> set[int]:
    out = set()
    for part in raw.replace(",", " ").split():
        try:
            out.add(int(part))
        except ValueError:
            pass
    return out


# Only these Discord user IDs may use the admin module. Everyone else is
# ignored silently. Set in .env: ADMIN_USER_IDS=123,456
ADMIN_USER_IDS = _id_set(os.environ.get("ADMIN_USER_IDS", ""))
# Optional channel for the audit log (every proposal + outcome lands here).
AUDIT_CHANNEL_ID_RAW = os.environ.get("AUDIT_CHANNEL_ID", "").strip()
AUDIT_CHANNEL_ID = int(AUDIT_CHANNEL_ID_RAW) if AUDIT_CHANNEL_ID_RAW.isdigit() else None
# Minecraft server address for the status module.
MC_ADDRESS = os.environ.get("MC_ADDRESS", "localhost:25565").strip()
# Watch module: optional "Name=URL,Name=URL" HTTP check overrides.
# Default checks are built from the MC_ADDRESS host (dashboard, Jellyfin).
WATCH_URLS_RAW = os.environ.get("WATCH_URLS", "").strip()
# Minecraft backups folder INSIDE the container (docker-compose mounts
# the host folder read-only). Missing folder = freshness check skips.
WATCH_BACKUP_DIR = os.environ.get("WATCH_BACKUP_DIR", "/backups").strip()
# Restore-test results log INSIDE the container (docker-compose mounts
# the host file read-only). The watch module announces each new result.
WATCH_RESTORE_RESULTS = os.environ.get("WATCH_RESTORE_RESULTS", "/restore-results.log").strip()

intents = discord.Intents.default()
intents.message_content = True  # privileged - enable in Dev Portal
intents.members = True          # privileged - enable in Dev Portal
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


async def audit(guild: discord.Guild | None, text: str) -> None:
    """Write one line to the audit channel (if configured) and the log.

    Alerts must never vanish silently: a missing channel id or a
    failed send is a warning in the log, so a dead audit channel is
    visible instead of swallowing the alarm it was meant to carry."""
    log.info("AUDIT: %s", text)
    if AUDIT_CHANNEL_ID is None:
        log.warning("AUDIT channel not configured - alert only in log")
        return
    channel = bot.get_channel(AUDIT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(AUDIT_CHANNEL_ID)
        except Exception:
            log.warning("AUDIT channel %s unreachable - alert only in log",
                        AUDIT_CHANNEL_ID)
            return
    try:
        await channel.send(text[:1900])
    except Exception:
        log.warning("AUDIT send failed - alert only in log", exc_info=True)


@bot.event
async def on_ready():
    log.info("Logged in as %s (id %s)", bot.user, bot.user.id if bot.user else "?")
    log.info("discord.py version: %s", discord.__version__)
    log.info("Admin whitelist: %s", sorted(ADMIN_USER_IDS) or "EMPTY - admin module disabled")
    if AUDIT_CHANNEL_ID is None:
        log.warning("AUDIT_CHANNEL_ID is not set - watch/tracker alerts "
                    "will only appear in the container log")


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    # A bare "!play" used to fail silently in the log. Answer in chat.
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing something - usage: `{PREFIX}{ctx.command.name} "
                       f"{ctx.command.signature}`" if ctx.command else "Missing an argument.")
    elif isinstance(error, commands.CommandNotFound):
        pass  # not every !word is a command; stay quiet
    elif isinstance(error, commands.CommandOnCooldown):
        pass  # per-command error handlers report their own cooldowns
    else:
        log.error("command %s failed: %s", ctx.command, error)
        await ctx.send(f"That command hit an error: {error}")


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    await ctx.send(
        "**House Bot**\n"
        f"`{PREFIX}play <song or URL>` add to the music queue - a panel with "
        "pause / skip / stop / queue buttons posts with each track\n"
        f"`{PREFIX}skip` / `{PREFIX}stop` / `{PREFIX}queue` / `{PREFIX}np` / `{PREFIX}leave`\n"
        f"`{PREFIX}panel` re-drop the music player panel at the bottom of the channel\n"
        f"`{PREFIX}updates` last known patch / version for every tracked game, NVIDIA drivers, device and iOS, plus free game drops from Epic, Steam and GOG (posts land in #game-updates)\n"
        "Star a message with ⭐ and it lands in #starboard\n"
        f"`{PREFIX}mc` Minecraft server status\n"
        f"`{PREFIX}status` potofgreed service health (dashboard, Jellyfin, backups)\n"
        "Admins: mention me + plain English, e.g. `@HouseBot kick Marcus`, "
        "`@HouseBot create a private channel called planning for me and @Marcus` - "
        "anything destructive asks you to confirm first.\n"
        f"Admins: `{PREFIX}request <title>` gets a show or movie - add `season 2` "
        "or `S03E04` for part of a show."
    )


async def _load() -> None:
    # One bad cog used to kill startup outright, and restart:
    # unless-stopped crash-looped the whole bot with nothing outside
    # reporting it. Load each cog on its own so the rest still come
    # up; the failure is loud in the log instead of fatal.
    for ext in ("cogs.music", "cogs.admin", "cogs.minecraft", "cogs.watch",
                "cogs.starboard", "cogs.updates", "cogs.media"):
        try:
            await bot.load_extension(ext)
        except Exception:
            log.exception("failed to load %s - continuing without it", ext)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is empty - copy .env.example to .env and fill it in.")
    import asyncio

    async def _main() -> None:
        # Load extensions and start the bot on ONE event loop. The old
        # shape (asyncio.run(_load()) followed by bot.run()) ran cog
        # setup on a throwaway loop that closed immediately, silently
        # killing anything a cog started while loading - that is what
        # strangled the updates tracker on 2026-10-31 (it logged
        # "loaded", then never polled, and its HTTP session was bound
        # to the dead loop).
        async with bot:
            await _load()
            await bot.start(TOKEN)

    asyncio.run(_main())
