"""Game / driver / OS update tracker - VicBot cog (added 2026-10-31).

Victor's ask (2026-10-31, live deploy session): watch patch notes and
version drops for the games and platforms the server cares about and
post them to #game-updates. Feeds (13 - the last 3 added later the
same day at Victor's ask: free game drops "everywhere, not just Epic";
those post to #free-games IF that channel exists, else #game-updates -
no new channel is required):

  Steam official news (ISteamNews, no key needed):
    - NBA 2K27 (app 4356430)       patch-note posts only
    - Forza Horizon 6 (app 2483190) update posts, no weekly playlist spam
    - Warframe (app 230410)        update + announcement posts
    - Phantom Blade Zero (app 4115450) patch / update posts
  Web pages:
    - COD MW4      callofduty.com living patch-notes page (dated sections)
    - GTA 6 (PS5)  rockstargames.com/VI page watcher until launch
                   (Nov 19, 2026 per the page itself), then the same
                   watcher catches the flip to available / new info
  APIs:
    - NVIDIA Game Ready drivers (AjaxDriverService; posts ONLY when the
      service returns a GeForce WHQL non-beta driver - anything else is
      treated as no data, never posted)
    - ONE PIECE Treasure Cruise (iTunes lookup id 943690848)
    - Pokemon Masters EX (iTunes lookup id 1442061397)
    - iOS releases (Apple Developer releases RSS, releases only -
      no betas, no release candidates)

Mechanics:
  - Polls every 3 hours. Seen-state persists as JSON on the bot-data
    volume (/data/updates-state.json). The FIRST successful fetch per
    feed only seeds the state - old news never posts.
  - Failure isolation: one broken feed never blocks the others, and a
    failed fetch never advances that feed's state (so a missed patch
    still posts on the next good run).
  - At most 3 posts per feed per run, oldest first, so a backlog after
    downtime cannot flood the channel.
  - !updates lists the last known item per feed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape

import aiohttp
import discord
from discord.ext import commands, tasks

log = logging.getLogger("housebot.updates")

CHANNEL_NAME = "game-updates"
POLL_HOURS = 3
MAX_POSTS_PER_FEED = 3
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

DATA_FILE = os.environ.get(
    "UPDATES_DATA_FILE",
    "/data/updates-state.json" if os.path.isdir("/data")
    else os.path.join(os.path.dirname(__file__), "updates-state.json"),
)

COD_URL = ("https://www.callofduty.com/patchnotes/2026/10/"
           "call-of-duty-modern-warfare-4-launch-patch-notes")
GTA_URL = "https://www.rockstargames.com/VI"
APPLE_RSS = "https://developer.apple.com/news/releases/rss/releases.rss"
NVIDIA_URL = (
    "https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/"
    "services/AjaxDriverService.php?func=DriverManualLookup&psID=98"
    "&pfID=623&osID=57&languageCode=1033&beta=0&isWHQL=0&dltype=-1"
    "&dch=1&upCRD=0&qnf=0&sort1=0&numberOfResults=1"
)
EPIC_URL = ("https://store-site-backend-static.ak.epicgames.com/"
            "freeGamesPromotions?locale=en-US&country=US"
            "&allowCountries=US")
STEAM_FREE_URL = ("https://store.steampowered.com/search/results/"
                  "?query&start=0&count=50&dynamic_data=&sort_by=_ASC"
                  "&force_infinite=1&maxprice=free&specials=1"
                  "&infinite=1")
GOG_URL = "https://www.gog.com/en/"

MONTHS = ("January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December")
DATE_RE = re.compile(r"(?:" + "|".join(MONTHS) + r") \d{1,2}, 20\d\d")


def _item(item_id: str, label: str, url: str, extra: str = "") -> dict:
    return {"id": str(item_id), "label": label, "url": url, "extra": extra}


class Updates(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self._channel: discord.TextChannel | None = None
        self._lock = asyncio.Lock()
        self.state: dict = self._load()

    # ---------- lifecycle ----------

    async def cog_load(self) -> None:
        # NOTE: no HTTP session here. Sessions bind to the running
        # event loop, so the session is created lazily on first use
        # (see _ensure_session) - a session made at load time can end
        # up bound to a loop that is no longer the bot's.
        self.poll_loop.start()
        log.info("updates tracker loaded: %d feeds, state=%s",
                 len(FEEDS), DATA_FILE)

    def cog_unload(self) -> None:
        self.poll_loop.cancel()
        if self.session is not None and not self.session.closed:
            try:
                asyncio.get_event_loop().create_task(self.session.close())
            except Exception:
                pass

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            # trust_env: honor proxy env vars if the host ever sets
            # them; with none set (the normal Docker case) this is a
            # plain direct connection.
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": UA}, timeout=timeout, trust_env=True)
        return self.session

    # ---------- state ----------

    def _load(self) -> dict:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                log.info("updates state loaded: %d feeds seeded", len(data))
                return data
        except FileNotFoundError:
            log.warning("updates state: no file yet at %s - starting empty",
                        DATA_FILE)
        except Exception:
            log.exception("updates state load failed - starting empty")
        return {}

    def _save(self) -> None:
        try:
            folder = os.path.dirname(DATA_FILE)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, indent=1, sort_keys=True)
            os.replace(tmp, DATA_FILE)
        except Exception:
            log.exception("updates state save failed")

    # ---------- http ----------

    async def _get_text(self, url: str) -> str | None:
        try:
            async with self._ensure_session().get(url) as resp:
                if resp.status != 200:
                    log.warning("fetch %s -> HTTP %s", url, resp.status)
                    return None
                return await resp.text(errors="ignore")
        except Exception:
            log.exception("fetch failed: %s", url)
            return None

    async def _get_json(self, url: str):
        text = await self._get_text(url)
        if text is None:
            return None
        try:
            return json.loads(text)
        except Exception:
            log.warning("bad JSON from %s", url)
            return None

    # ---------- feed fetchers (each returns newest-first items) ----------

    async def _steam_news(self, appid: int, keep) -> list[dict] | None:
        url = ("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
               f"?appid={appid}&count=30&maxlength=0&format=json")
        data = await self._get_json(url)
        if not data:
            return None
        items = []
        for raw in data.get("appnews", {}).get("newsitems", []):
            if raw.get("feed_type") != 1:
                continue  # official community announcements only
            title = (raw.get("title") or "").strip()
            if not title or not keep(title):
                continue
            when = datetime.fromtimestamp(raw.get("date", 0),
                                          tz=timezone.utc).strftime("%b %d, %Y")
            items.append(_item(raw.get("gid"), title, raw.get("url", ""),
                               extra=when))
        return items

    async def _feed_nba2k27(self):
        return await self._steam_news(
            4356430, lambda t: "patch" in t.lower())

    async def _feed_fh6(self):
        def keep(t: str) -> bool:
            low = t.lower()
            if "playlist" in low:
                return False
            return any(k in low for k in ("update", "patch", "notes",
                                          "series"))
        return await self._steam_news(2483190, keep)

    async def _feed_warframe(self):
        def keep(t: str) -> bool:
            low = t.lower()
            return not any(k in low for k in ("workshop", "contest",
                                              "sale", "discount",
                                              "tennobaum"))
        return await self._steam_news(230410, keep)

    async def _feed_pbz(self):
        def keep(t: str) -> bool:
            low = t.lower()
            return any(k in low for k in ("patch", "update", "hotfix",
                                          "fix"))
        return await self._steam_news(4115450, keep)

    async def _feed_cod(self):
        html = await self._get_text(COD_URL)
        if html is None:
            return None
        dates = DATE_RE.findall(html)
        if not dates:
            log.warning("COD page fetched but no dated sections found")
            return None

        def key(d: str) -> datetime:
            return datetime.strptime(d, "%B %d, %Y")

        latest = max(dates, key=key)
        return [_item(f"cod-{latest}", f"Patch notes updated - {latest}",
                      COD_URL)]

    async def _feed_gta6(self):
        html = await self._get_text(GTA_URL)
        if html is None:
            return None
        dates = DATE_RE.findall(html)
        preorder = "Pre-Order Now" in html
        release = ""
        idx = html.find("Coming")
        if idx != -1:
            nearby = DATE_RE.search(html[idx:idx + 200])
            if nearby:
                release = nearby.group(0)
        if not release and dates:
            release = dates[0]
        if not release:
            log.warning("GTA page fetched but no release date found")
            return None
        label = f"Official page: Coming {release}"
        if preorder:
            label += " - pre-orders open"
        return [_item(f"gta-{release}-{int(preorder)}", label, GTA_URL)]

    async def _feed_nvidia(self):
        data = await self._get_json(NVIDIA_URL)
        if not data:
            return None
        try:
            info = data["IDS"][0]["downloadInfo"]
        except Exception:
            log.warning("NVIDIA service returned an unexpected shape")
            return None
        name = urllib.parse.unquote(info.get("Name", ""))
        # Only ever post a real GeForce Game Ready WHQL release. The
        # service sometimes answers with workstation (RTX PRO) drivers
        # for odd parameter/IP combos - that is treated as NO DATA.
        if "GeForce" not in name:
            log.info("NVIDIA check: service answered %r - not a GeForce "
                     "driver, staying silent", name)
            return []
        if info.get("IsWHQL") != "1" or info.get("IsBeta") != "0":
            return []
        version = info.get("Version", "").strip()
        if not version:
            return None
        details = urllib.parse.unquote(info.get("DetailsURL", "")) \
            or "https://www.nvidia.com/en-us/drivers/"
        when = info.get("ReleaseDateTime", "")
        return [_item(f"nvidia-{version}",
                      f"GeForce Game Ready Driver {version}", details,
                      extra=when)]

    async def _itunes_app(self, track_id: int):
        url = f"https://itunes.apple.com/lookup?id={track_id}&country=us"
        data = await self._get_json(url)
        if not data or not data.get("results"):
            return None
        app = data["results"][0]
        version = (app.get("version") or "").strip()
        if not version:
            return None
        when = (app.get("currentVersionReleaseDate") or "")[:10]
        notes = (app.get("releaseNotes") or "").strip()
        if len(notes) > 350:
            notes = notes[:347].rstrip() + "..."
        return [_item(f"app-{track_id}-{version}", f"Version {version}",
                      app.get("trackViewUrl", ""), extra=when,
                      ) | {"notes": notes}]

    async def _feed_optc(self):
        return await self._itunes_app(943690848)

    async def _feed_pokemon(self):
        return await self._itunes_app(1442061397)

    async def _feed_ios(self):
        xml = await self._get_text(APPLE_RSS)
        if xml is None:
            return None
        try:
            root = ET.fromstring(xml)
        except Exception:
            log.warning("Apple releases RSS did not parse")
            return None
        items = []
        for node in root.iter("item"):
            title = (node.findtext("title") or "").strip()
            if not re.match(r"iOS \d", title):
                continue
            if "beta" in title.lower() or "Release Candidate" in title:
                continue
            link = (node.findtext("link") or "").strip()
            items.append(_item(node.findtext("guid") or title, title, link))
        return items

    @staticmethod
    def _parse_iso(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    async def _feed_epic_free(self):
        data = await self._get_json(EPIC_URL)
        if not data:
            return None
        try:
            elements = data["data"]["Catalog"]["searchStore"]["elements"]
        except Exception:
            log.warning("Epic promotions payload had an unexpected shape")
            return None
        now = datetime.now(timezone.utc)
        items = []
        for el in elements:
            try:
                groups = (el.get("promotions") or {}).get(
                    "promotionalOffers") or []
                end = None
                for group in groups:
                    for offer in group.get("promotionalOffers") or []:
                        setting = offer.get("discountSetting") or {}
                        if setting.get("discountPercentage") != 0:
                            continue
                        start = self._parse_iso(offer.get("startDate"))
                        stop = self._parse_iso(offer.get("endDate"))
                        if start and stop and start <= now <= stop:
                            end = stop
                if end is None:
                    continue  # not free right now (or only upcoming)
                total = (el.get("price") or {}).get("totalPrice") or {}
                if not total.get("originalPrice"):
                    continue  # always-free F2P title, not a drop
                title = (el.get("title") or "").strip()
                slug = el.get("productSlug") or ""
                url = ("https://www.epicgames.com/store/en-US/product/"
                       + slug) if slug else \
                    "https://www.epicgames.com/store/en-US/free-games"
                items.append(_item(
                    f"epic-{slug or title}", f"FREE on Epic: {title}",
                    url, extra=f"Free until {end.strftime('%b %d')}"))
            except Exception:
                continue
        return items

    async def _feed_steam_free(self):
        data = await self._get_json(STEAM_FREE_URL)
        if not data or "results_html" not in data:
            return None
        items = []
        chunks = re.split(r'<a href="https://store\.steampowered\.com/app/',
                          data["results_html"])
        for chunk in chunks[1:]:
            m = re.match(r"(\d+)/", chunk)
            if not m or "-100%" not in chunk:
                continue
            t = re.search(r'<span class="title">(.*?)</span>', chunk, re.S)
            if not t:
                continue
            title = unescape(re.sub(r"<[^>]+>", "", t.group(1))).strip()
            if not title:
                continue
            appid = m.group(1)
            items.append(_item(
                f"steamfree-{appid}", f"FREE on Steam: {title}",
                f"https://store.steampowered.com/app/{appid}/"))
        return items

    async def _feed_gog_free(self):
        page = await self._get_text(GOG_URL)
        if page is None:
            return None
        m = re.search(
            r'giveaway__overlay-link" href='
            r'"https://www\.gog\.com/en/game/([a-z0-9_]+)"', page)
        if not m:
            return []  # no giveaway running right now
        slug = m.group(1)
        title = slug.replace("_", " ").title()
        return [_item(f"gog-{slug}", f"FREE on GOG: {title}",
                      f"https://www.gog.com/en/game/{slug}")]

    # ---------- engine ----------

    def _target_channel(self) -> discord.TextChannel | None:
        if self._channel is not None:
            return self._channel
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels,
                                        name=CHANNEL_NAME)
            if channel is not None:
                self._channel = channel
                return channel
        return None

    async def _resolve_channel(self) -> discord.TextChannel | None:
        channel = self._target_channel()
        if channel is not None:
            return channel
        for guild in self.bot.guilds:
            try:
                channel = await guild.create_text_channel(
                    CHANNEL_NAME, topic="Game, driver and iOS update posts "
                                        "from VicBot.")
                log.info("created #%s in %s", CHANNEL_NAME, guild.name)
                self._channel = channel
                return channel
            except Exception:
                log.exception("could not create #%s in %s",
                              CHANNEL_NAME, guild.name)
        log.error("no #%s channel available in any guild", CHANNEL_NAME)
        return None

    async def _run_feed(self, feed: dict, channel) -> None:
        key = feed["key"]
        try:
            items = await getattr(self, feed["fetch"])()
        except Exception:
            log.exception("feed %s crashed", key)
            return
        if items is None:
            return  # fetch failed; state untouched, retry next run
        seen = self.state.get(key)
        if seen is None:
            # First successful contact seeds silently so old news never
            # posts. If nothing relevant exists yet (a filtered feed
            # before its first patch), seed EMPTY instead - otherwise
            # the first real patch would be swallowed as the seed.
            newest = items[0] if items else {"id": "", "label": "(none yet)"}
            self.state[key] = {"id": newest["id"], "label": newest["label"]}
            self._save()
            log.info("feed %s seeded at %s", key, newest["label"])
            return
        if not items:
            return  # fetched fine, nothing relevant right now
        newest = items[0]
        if seen["id"] == newest["id"]:
            return
        fresh = []
        for it in items:
            if it["id"] == seen["id"]:
                break
            fresh.append(it)
        else:
            fresh = items  # stored item scrolled out of the window
        fresh = fresh[:MAX_POSTS_PER_FEED]
        target = channel
        pref = feed.get("channel")
        if pref and channel is not None:
            found = discord.utils.get(channel.guild.text_channels,
                                      name=pref)
            if found is not None:
                target = found
        for it in reversed(fresh):  # oldest first
            await self._post(feed, it, target)
        self.state[key] = {"id": newest["id"], "label": newest["label"]}
        self._save()

    async def _post(self, feed: dict, it: dict, channel) -> None:
        embed = discord.Embed(title=f"{feed['emoji']} {feed['name']}",
                              description=f"**{it['label']}**",
                              url=it["url"] or None,
                              color=feed["color"])
        if it.get("extra"):
            embed.add_field(name="Released", value=it["extra"],
                            inline=True)
        if it.get("notes"):
            embed.add_field(name="What's new", value=it["notes"],
                            inline=False)
        if it["url"]:
            embed.add_field(name="Link", value=it["url"], inline=False)
        try:
            await channel.send(embed=embed)
            log.info("posted update: %s - %s", feed["key"], it["label"])
        except Exception:
            log.exception("post failed for feed %s", feed["key"])
            raise

    @tasks.loop(hours=POLL_HOURS)
    async def poll_loop(self) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            log.info("updates poll running")
            channel = await self._resolve_channel()
            if channel is None:
                return
            for feed in FEEDS:
                try:
                    await self._run_feed(feed, channel)
                except Exception:
                    # _run_feed handles fetch/post errors per feed; this
                    # is the last-resort guard so one feed can never
                    # kill the loop for the other nine.
                    log.exception("feed %s failed unexpectedly",
                                  feed["key"])

    @poll_loop.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    # ---------- command ----------

    @commands.command(name="updates")
    async def updates(self, ctx: commands.Context):
        """!updates - last known item per tracked feed."""
        lines = [f"**Tracked updates** (posting to #{CHANNEL_NAME}, "
                 f"checked every {POLL_HOURS}h):"]
        for feed in FEEDS:
            seen = self.state.get(feed["key"])
            if seen:
                lines.append(f"{feed['emoji']} **{feed['name']}** - "
                             f"{seen['label']}")
            else:
                lines.append(f"{feed['emoji']} **{feed['name']}** - "
                             "no data yet (seeds on the first good check)")
        await ctx.send("\n".join(lines))


FEEDS = [
    {"key": "nba2k27", "name": "NBA 2K27 (PS5)", "emoji": "🏀",
     "color": 0xE03A3A, "fetch": "_feed_nba2k27"},
    {"key": "cod-mw4", "name": "COD: Modern Warfare 4", "emoji": "🔫",
     "color": 0x8A9A5B, "fetch": "_feed_cod"},
    {"key": "nvidia", "name": "NVIDIA GeForce Drivers", "emoji": "🟢",
     "color": 0x76B900, "fetch": "_feed_nvidia"},
    {"key": "gta6", "name": "GTA VI (PS5)", "emoji": "🌴",
     "color": 0xF2A03D, "fetch": "_feed_gta6"},
    {"key": "pbz", "name": "Phantom Blade Zero (Steam)", "emoji": "⚔️",
     "color": 0x9B59B6, "fetch": "_feed_pbz"},
    {"key": "fh6", "name": "Forza Horizon 6 (Steam)", "emoji": "🏎️",
     "color": 0xF7630C, "fetch": "_feed_fh6"},
    {"key": "warframe", "name": "Warframe (Steam)", "emoji": "🛡️",
     "color": 0x3D85C6, "fetch": "_feed_warframe"},
    {"key": "optc", "name": "ONE PIECE Treasure Cruise", "emoji": "🏴‍☠️",
     "color": 0xC0392B, "fetch": "_feed_optc"},
    {"key": "pokemon-masters", "name": "Pokemon Masters EX", "emoji": "⚡",
     "color": 0xF1C40F, "fetch": "_feed_pokemon"},
    {"key": "ios", "name": "iOS Releases", "emoji": "🍎",
     "color": 0xA2AAAD, "fetch": "_feed_ios"},
    {"key": "epic-free", "name": "Epic Free Games", "emoji": "🎁",
     "color": 0x444444, "fetch": "_feed_epic_free", "channel": "free-games"},
    {"key": "steam-free", "name": "Steam Freebies", "emoji": "🎁",
     "color": 0x1B78C4, "fetch": "_feed_steam_free",
     "channel": "free-games"},
    {"key": "gog-free", "name": "GOG Giveaways", "emoji": "🎁",
     "color": 0x86328A, "fetch": "_feed_gog_free", "channel": "free-games"},
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Updates(bot))
