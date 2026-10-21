"""Watch module: potofgreed service health, alerted into Discord.

Checks every 2 minutes:
  - Minecraft (reuses the minecraft module's status call)
  - HTTP services: dashboard and Jellyfin by default
    (override with WATCH_URLS in .env as Name=URL pairs)
  - Minecraft backup freshness: newest file in the mounted backups
    folder must be under 26 hours old (skips itself if not mounted)

Alerting rule - signal, not noise: nothing posts while healthy.
A check has to fail TWICE in a row before a DOWN alert fires, and
exactly one BACK UP message fires when it recovers. Alerts go to
the audit/log channel. !status runs every check live, for anyone.
"""

import logging
import os
import time

import aiohttp
from discord.ext import commands, tasks

from cogs.minecraft import fetch_status

log = logging.getLogger("housebot.watch")

CHECK_EVERY_SECONDS = 120
FAILURES_BEFORE_ALERT = 2
BACKUP_MAX_AGE_HOURS = 26


class Check:
    def __init__(self, name):
        self.name = name
        self.state = "unknown"  # unknown | up | down
        self.failures = 0
        self.detail = ""
        self.down_since = None


class Watch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        from bot import MC_ADDRESS, WATCH_BACKUP_DIR, WATCH_URLS_RAW, audit

        self._audit = audit
        self.mc_address = MC_ADDRESS
        self.backup_dir = WATCH_BACKUP_DIR

        host = MC_ADDRESS.rsplit(":", 1)[0] if ":" in MC_ADDRESS else MC_ADDRESS
        if WATCH_URLS_RAW:
            self.http_checks = []
            for item in WATCH_URLS_RAW.split(","):
                if "=" in item:
                    name, url = item.split("=", 1)
                    self.http_checks.append((name.strip(), url.strip()))
        else:
            self.http_checks = [
                ("Dashboard", f"http://{host}:8124/"),
                ("Jellyfin", f"http://{host}:8096/health"),
            ]

        self.mc = Check("Minecraft")
        self.http = {name: Check(name) for name, _ in self.http_checks}
        self.backup = Check("MC backups")
        self.watch_loop.start()
        log.info(
            "Watch checks: Minecraft, %s, backups at %s",
            ", ".join(self.http),
            self.backup_dir,
        )

    def cog_unload(self):
        self.watch_loop.cancel()

    # ---------------- probes: each returns (ok, detail); ok=None = skip --------

    async def _probe_mc(self):
        try:
            _, online, maximum, _, _ = await fetch_status(self.mc_address)
            return True, f"{online}/{maximum} players"
        except Exception as exc:
            return False, f"unreachable ({type(exc).__name__})"

    async def _probe_http(self, session, url):
        try:
            async with session.get(url) as resp:
                if resp.status < 500:
                    return True, f"HTTP {resp.status}"
                return False, f"HTTP {resp.status}"
        except Exception as exc:
            return False, f"unreachable ({type(exc).__name__})"

    def _probe_backup(self):
        try:
            paths = [
                os.path.join(self.backup_dir, name)
                for name in os.listdir(self.backup_dir)
            ]
        except Exception:
            return None, "backups folder not mounted - check skipped"
        if not paths:
            return False, "backup folder is EMPTY"
        newest = max(os.path.getmtime(p) for p in paths)
        age_h = (time.time() - newest) / 3600
        if age_h <= BACKUP_MAX_AGE_HOURS:
            return True, f"newest backup is {age_h:.1f}h old"
        return False, f"newest backup is {age_h:.1f}h old (limit {BACKUP_MAX_AGE_HOURS}h)"

    # ---------------- state machine: alert on transitions only ----------------

    async def _apply(self, check: Check, result):
        ok, detail = result
        if ok is None:
            return
        check.detail = detail
        if ok:
            check.failures = 0
            if check.state == "down":
                check.state = "up"
                mins = ""
                if check.down_since:
                    mins = f" (was down ~{(time.time() - check.down_since) / 60:.0f} min)"
                await self._audit(None, f"🟢 **{check.name} is back up** on potofgreed{mins}.")
            else:
                check.state = "up"
        else:
            check.failures += 1
            if check.state != "down" and check.failures >= FAILURES_BEFORE_ALERT:
                check.state = "down"
                check.down_since = time.time()
                await self._audit(
                    None, f"🔴 **{check.name} is DOWN** on potofgreed - {detail}."
                )

    @tasks.loop(seconds=CHECK_EVERY_SECONDS)
    async def watch_loop(self):
        await self._apply(self.mc, await self._probe_mc())
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for name, url in self.http_checks:
                await self._apply(self.http[name], await self._probe_http(session, url))
        await self._apply(self.backup, self._probe_backup())

    @watch_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ---------------- command ----------------

    @commands.command(name="status")
    async def status(self, ctx: commands.Context):
        """!status - live health of everything the watch module covers."""
        rows = [("Minecraft", *await self._probe_mc())]
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for name, url in self.http_checks:
                rows.append((name, *await self._probe_http(session, url)))
        ok, detail = self._probe_backup()
        if ok is not None:
            rows.append(("MC backups", ok, detail))
        lines = ["**potofgreed status**"]
        for name, good, detail in rows:
            lines.append(f"{'🟢' if good else '🔴'} {name} - {detail}")
        await ctx.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(Watch(bot))
