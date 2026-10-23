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
    """Write one line to the audit channel (if configured) and the log."""
    log.info("AUDIT: %s", text)
    if AUDIT_CHANNEL_ID is None:
        return
    channel = bot.get_channel(AUDIT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(AUDIT_CHANNEL_ID)
        except Exception:
            return
    try:
        await channel.send(text[:1900])
    except Exception:
        pass


@bot.event
async def on_ready():
    log.info("Logged in as %s (id %s)", bot.user, bot.user.id if bot.user else "?")
    log.info("discord.py version: %s", discord.__version__)
    log.info("Admin whitelist: %s", sorted(ADMIN_USER_IDS) or "EMPTY - admin module disabled")


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    # A bare "!play" used to fail silently in the log. Answer in chat.
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing something - usage: `{PREFIX}{ctx.command.name} "
                       f"{ctx.command.signature}`" if ctx.command else "Missing an argument.")
    elif isinstance(error, commands.CommandNotFound):
        pass  # not every !word is a command; stay quiet
    else:
        log.error("command %s failed: %s", ctx.command, error)
        await ctx.send(f"That command hit an error: {error}")


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    await ctx.send(
        "**House Bot**\n"
        f"`{PREFIX}play <song or URL>` add to the music queue\n"
        f"`{PREFIX}skip` / `{PREFIX}stop` / `{PREFIX}queue` / `{PREFIX}np` / `{PREFIX}leave`\n"
        f"`{PREFIX}mc` Minecraft server status\n"
        f"`{PREFIX}status` potofgreed service health (dashboard, Jellyfin, backups)\n"
        "Admins: mention me + plain English, e.g. `@HouseBot kick Marcus`, "
        "`@HouseBot create a private channel called planning for me and @Marcus` - "
        "anything destructive asks you to confirm first."
    )


async def _load() -> None:
    for ext in ("cogs.music", "cogs.admin", "cogs.minecraft", "cogs.watch"):
        await bot.load_extension(ext)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is empty - copy .env.example to .env and fill it in.")
    import asyncio

    asyncio.run(_load())
    bot.run(TOKEN, log_handler=None)
