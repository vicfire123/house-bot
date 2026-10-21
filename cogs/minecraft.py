"""Minecraft module: status for the potofgreed Paper server.

Talks to the server directly (status ping + query - query is enabled on
potofgreed, which is what returns the real player list). Also keeps the
bot's Discord presence updated: "3/50 on Minecraft" etc.
"""

import logging

import discord
from discord.ext import commands, tasks
from mcstatus import JavaServer

log = logging.getLogger("housebot.minecraft")


async def fetch_status(address: str):
    """Returns (online, players_online, players_max, names, version)."""
    server = JavaServer.lookup(address)
    status = await server.async_status()
    names = []
    try:
        query = await server.async_query()
        names = list(query.players.names)
    except Exception:
        if status.players.sample:
            names = [p.name for p in status.players.sample]
    return (
        True,
        status.players.online,
        status.players.max,
        names,
        str(status.version.name) if status.version else "?",
    )


class Minecraft(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        from bot import MC_ADDRESS  # local import: cogs load after core
        self.address = MC_ADDRESS
        self.presence_loop.start()

    def cog_unload(self):
        self.presence_loop.cancel()

    @tasks.loop(seconds=60)
    async def presence_loop(self):
        try:
            _, online, maximum, _, _ = await fetch_status(self.address)
            text = f"{online}/{maximum} on Minecraft"
        except Exception:
            text = "Minecraft offline"
        try:
            await self.bot.change_presence(
                activity=discord.Game(name=text), status=discord.Status.online)
        except Exception:
            pass

    @presence_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    @commands.command(name="mc")
    async def mc(self, ctx: commands.Context):
        """!mc - is the server up, and who is on it."""
        try:
            _, online, maximum, names, version = await fetch_status(self.address)
        except Exception:
            await ctx.send("Minecraft server is **offline** (or unreachable from potofgreed).")
            return
        lines = [f"**Minecraft is up** - {online}/{maximum} players ({version})"]
        if names:
            lines.append("Online: " + ", ".join(sorted(names)))
        elif online:
            lines.append("Players are on, but the server isn't sharing names right now.")
        else:
            lines.append("Nobody on right now.")
        lines.append("Ask Victor for the address + Tailscale invite - it's a private server.")
        await ctx.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(Minecraft(bot))
