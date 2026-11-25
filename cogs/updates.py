"""Game / driver / OS update tracker - VicBot cog (added 2026-10-31).

Victor's ask (2026-10-31, live deploy session): watch patch notes and
version drops for the games and platforms the server cares about and
post them to #game-updates. Feeds (17 - feeds 11-13 added later on
2026-10-31 at Victor's ask: free game drops "everywhere, not just
Epic"; those post to #free-games IF that channel exists, else
#game-updates. Feed 14, COD Warzone, added 2026-11-07 at Victor's
ask. Feeds 15-16 and the iOS filter added 2026-11-10 at Victor's
ask: device updates for HIS hardware only. BLEACH Mirrors High
added 2026-11-10 at Victor's ask, the same day the NVIDIA feed was
REMOVED at his ask: NVIDIA's lookup service only ever answered
workstation / data-center drivers, never a GeForce Game Ready
driver, so that feed never fired once. NVIDIA RESTORED 2026-11-24
with a fixed filter - see the NVIDIA entry below. GTA VI switched from
the /VI page watcher to the Rockstar Support launch-updates article
the same day):

  Steam official news (ISteamNews, no key needed):
    - NBA 2K27 (app 4356430)       patch-note posts only
    - Forza Horizon 6 (app 2483190) update posts, no weekly playlist spam
    - Warframe (app 230410)        update + announcement posts
    - Phantom Blade Zero (app 4115450) patch / update posts
  Web pages:
    - COD MW4      callofduty.com patch-notes index -> current MW4 page
    - COD Warzone  callofduty.com patch-notes index -> current Warzone
                   page (the index lists each series newest first, so
                   season changes are picked up on their own; both COD
                   feeds fall back to their last known page if the
                   index ever stops listing links)
    - GTA 6 (PS5)  Rockstar Support "GTA VI Launch Updates" article
                   (switched 2026-11-24; the /VI page watcher posted
                   its one "out now" item on Nov 19 and was never
                   wired to patch notes, so 01.002.002 on Nov 23 went
                   unreported). One item per dated entry
                   "[Month D, YYYY] - <section>", bullets as notes.
                   Fetched with curl_cffi impersonating Chrome: Akamai
                   bot mitigation stalls plain aiohttp / curl forever
                   on this host (verified Nov 24 from the sandbox and
                   the bot container). No version number in the post -
                   Rockstar does not publish one. Fallback if Akamai
                   ever beats the impersonation (NOT built): Leonida
                   Waypoint's fan patch log,
                   https://leonidawaypoint.com/patch-notes - plain
                   fetch works but it can lag up to a day.
  APIs:
    - NVIDIA GeForce drivers (AjaxDriverService DriverManualLookup,
      RTX 50 Series / RTX 5070 / Windows 11: psID=131 pfID=1070
      osID=135). Posts ONLY a WHQL, non-beta release whose name has
      "GeForce" or "RTX Driver Release" (NVIDIA renamed the family to
      "NVIDIA RTX Driver Release 620" - the old GeForce-only filter
      would have stayed silent forever) and NOT "Data Center",
      "Quadro" or "RTX PRO". Hotfixes stay silent. The date is the
      service's ReleaseDateTime as-is (it can differ from NVIDIA's
      driver page - known quirk, not fixed on purpose)
    - ONE PIECE Treasure Cruise (iTunes lookup id 943690848)
    - Pokemon Masters EX (iTunes lookup id 1442061397)
    - BLEACH Mirrors High (iTunes lookup id 6758332966)
    - iOS releases (Apple Developer releases RSS, releases only -
      no betas, no release candidates; FILTERED to the current iOS
      generation only - Victor's iPhone 18 Pro Max cannot install
      the legacy maintenance branches (iOS 18.x and older) that the
      RSS also carries for old devices)
    - Galaxy S23 Ultra firmware (SamMobile archive for SM-S918U /
      TMB - Samsung's own FOTA version.xml endpoint now 403s from
      everywhere, verified from the sandbox AND from potofgreed;
      SamMobile's archive is server-rendered and lists every build
      with its release date, so the feed takes the newest dated
      TMB build)
    - Xiaomi Pad 8 HyperOS (miuidownloader yupei page; only his
      tablet's own firmware branch - the MIXM global builds,
      WPZMIXM on Android 16, XPZMIXM on Android 17 - newest wins)

Mechanics:
  - Polls every 3 hours. Seen-state persists as JSON on the bot-data
    volume (/data/updates-state.json). The FIRST successful fetch per
    feed only seeds the state - old news never posts.
  - Failure isolation: one broken feed never blocks the others, and a
    failed fetch never advances that feed's state (so a missed patch
    still posts on the next good run).
  - Health: every feed records its last good check in
    /data/updates-health.json. A feed with no good check for 24h gets
    ONE alert in the audit channel, and !updates marks its label stale
    ("no good check since ...") instead of showing it as healthy.
  - Free-game feeds track a SET of seen ids (Epic/Steam return
    unordered sets - one "newest" id both missed drops and reposted
    them). Only items actually posted join the set.
  - At most 3 posts per feed per run, oldest first, and overflow is
    never dropped - it posts on the next poll. State advances only to
    the newest item actually posted, so a mid-batch failure reposts
    just the failed item.
  - If a post target was deleted, the cached channel is cleared so
    the next poll re-resolves (or recreates) it.
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

# Empty env value means "not set" (an empty UPDATES_DATA_FILE= line in
# .env used to make this "" and silently break every save - the file
# path is derived the same way starboard.py does it).
_raw_data_file = os.environ.get("UPDATES_DATA_FILE", "").strip()
DATA_FILE = _raw_data_file or (
    "/data/updates-state.json" if os.path.isdir("/data")
    else os.path.join(os.path.dirname(__file__), "updates-state.json")
)
# Per-feed health (last good check, last failure, alert sent) lives in
# a second file next to the state file so the state shape never changes.
HEALTH_FILE = os.path.join(os.path.dirname(DATA_FILE),
                           "updates-health.json")
# A feed with no good check for this long gets ONE alert in the audit
# channel, then stays quiet until it recovers.
FEED_ALERT_AFTER_HOURS = 24
# Free-game feeds track a set of seen IDs (not one newest ID) so an
# unordered source can never make a drop get missed or reposted.
MAX_SEEN_IDS = 50

COD_INDEX_URL = "https://www.callofduty.com/patchnotes/"
# Fallbacks if the index ever stops listing links: the last known
# living pages for each COD series.
COD_URL = ("https://www.callofduty.com/patchnotes/2026/10/"
           "call-of-duty-modern-warfare-4-launch-patch-notes")
WARZONE_URL = ("https://www.callofduty.com/patchnotes/2026/11/"
               "call-of-duty-mw4-warzone-season-1-patch-notes")
GTA_SUPPORT_URL = ("https://support.rockstargames.com/articles/"
                   "sN5pEdFYZdlvijKXd6xpJ/"
                   "grand-theft-auto-vi-launch-updates-ps5-xbox-series-x-or-s")
# NVIDIA AjaxDriverService: GeForce RTX 50 Series (psID=131), GeForce
# RTX 5070 (pfID=1070), Windows 11 (osID=135) - walked live from
# NVIDIA's lookupValueSearch.aspx on 2026-11-24. The old params
# (psID=98 pfID=623 osID=57) are dead - do not reuse them.
NVIDIA_URL = (
    "https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/"
    "services/AjaxDriverService.php?func=DriverManualLookup"
    "&psID=131&pfID=1070&osID=135&languageCode=1033&beta=0&isWHQL=0"
    "&dltype=-1&dch=1&upCRD=0&qnf=0&sort1=0&numberOfResults=1")
NVIDIA_NAME_OK = ("GeForce", "RTX Driver Release")
NVIDIA_NAME_BAD = ("Data Center", "Quadro", "RTX PRO")
# Embed field values cap at 1024 chars.
NOTES_MAX = 1000
APPLE_RSS = "https://developer.apple.com/news/releases/rss/releases.rss"
EPIC_URL = ("https://store-site-backend-static.ak.epicgames.com/"
            "freeGamesPromotions?locale=en-US&country=US"
            "&allowCountries=US")
STEAM_FREE_URL = ("https://store.steampowered.com/search/results/"
                  "?query&start=0&count=50&dynamic_data=&sort_by=_ASC"
                  "&force_infinite=1&maxprice=free&specials=1"
                  "&infinite=1")
GOG_URL = "https://www.gog.com/en/"
SAMSUNG_URL = ("https://www.sammobile.com/samsung/galaxy-s23-ultra/"
               "firmware/SM-S918U/TMB/")
XIAOMI_URL = "https://miuidownloader.com/hyperos/yupei"

MONTHS = ("January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December")
DATE_RE = re.compile(r"(?:" + "|".join(MONTHS) + r") \d{1,2}, 20\d\d")


def _item(item_id: str, label: str, url: str, extra: str = "") -> dict:
    return {"id": str(item_id), "label": label, "url": url, "extra": extra}


_GTA_HEADER_RE = re.compile(
    r"^\[?\s*(" + DATE_RE.pattern + r")\s*\]?\s*[-\u2013\u2014:]\s*(.+)$")
_BLOCK_TAGS_RE = re.compile(
    r"</?(?:p|div|br|h[1-6]|ul|ol|section|article|tr|table)\b[^>]*>", re.I)


def _html_lines(html: str) -> list[str]:
    """HTML -> visible text lines. <li> becomes a "- " bullet line,
    block tags become line breaks, scripts and styles are dropped."""
    html = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", html,
                  flags=re.S | re.I)
    html = re.sub(r"<li\b[^>]*>", "\n- ", html, flags=re.I)
    html = _BLOCK_TAGS_RE.sub("\n", html)
    html = re.sub(r"<[^>]+>", "", html)
    lines = []
    for raw in unescape(html).replace("\xa0", " ").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line and line != "-":
            lines.append(line)
    return lines


def _trim_notes(bullets: list[str]) -> str:
    out = ""
    for b in bullets:
        line = b if b.startswith("- ") else f"- {b}"
        nxt = f"{out}\n{line}" if out else line
        if len(nxt) > NOTES_MAX:
            return (out + "\n...") if out else line[:NOTES_MAX - 3] + "..."
        out = nxt
    return out


def parse_gta_support(html: str) -> list[dict]:
    """Rockstar Support "GTA VI Launch Updates" article -> one item per
    dated entry, newest first. Entries are headed
    "[Month D, YYYY] - <section>"; several sections on the same date
    merge into one item. [] when no dated header is found."""
    entries: dict[str, list[str]] = {}
    current = None
    for line in _html_lines(html):
        m = _GTA_HEADER_RE.match(line)
        if m:
            current = m.group(1)
            entries.setdefault(current, [])
            continue
        if current is not None and line.startswith("- "):
            entries[current].append(line)
    ordered = sorted(entries,
                     key=lambda d: datetime.strptime(d, "%B %d, %Y"),
                     reverse=True)
    return [_item(f"gta-patch-{d}", f"Patch notes updated - {d}",
                  GTA_SUPPORT_URL) | {"notes": _trim_notes(entries[d])}
            for d in ordered]


def parse_nvidia(data) -> list[dict] | None:
    """AjaxDriverService JSON -> [item] for a WHQL GeForce-family
    release, [] (silent) for anything else, None for a broken payload."""
    try:
        info = data["IDS"][0]["downloadInfo"]
    except Exception:
        log.warning("NVIDIA service returned an unexpected shape")
        return None
    name = urllib.parse.unquote(str(info.get("Name", "")))
    if str(info.get("IsWHQL")) != "1" or str(info.get("IsBeta")) != "0":
        return []  # hotfix / beta: stay silent
    if not any(k in name for k in NVIDIA_NAME_OK) \
            or any(k in name for k in NVIDIA_NAME_BAD):
        log.info("NVIDIA check: service answered %r - not a GeForce "
                 "family driver, staying silent", name)
        return []
    version = str(info.get("Version", "")).strip()
    if not version:
        return []
    details = urllib.parse.unquote(str(info.get("DetailsURL", ""))) \
        or "https://www.nvidia.com/en-us/drivers/"
    return [_item(f"nvidia-{version}",
                  f"GeForce Game Ready Driver {version}", details,
                  extra=str(info.get("ReleaseDateTime", "")).strip())]


class Updates(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self._channel: discord.TextChannel | None = None
        self._lock = asyncio.Lock()
        self.state: dict = self._load()
        self.health: dict = self._load_health()

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
                # Prune orphan keys (e.g. "nvidia" - feed removed Nov
                # 10): no feed reads them, and leaving them makes the
                # file lie about what is tracked. A feed's ":seen" set
                # (free-game feeds) is a real key and survives.
                known = {f["key"] for f in FEEDS}
                known |= {k + ":seen" for k in known}
                orphans = [k for k in data if k not in known]
                if orphans:
                    for k in orphans:
                        del data[k]
                    log.info("updates state: pruned orphan keys %s", orphans)
                    tmp = DATA_FILE + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as out:
                        json.dump(data, out, indent=1, sort_keys=True)
                    os.replace(tmp, DATA_FILE)
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

    async def _cod_current_url(self, marker: str, exclude: str = ""):
        """First patch-notes link for a COD series on the official
        index. The index lists each series newest first, so the first
        match is the series' current living page - a new season or
        Reloaded page takes over automatically when it publishes."""
        index = await self._get_text(COD_INDEX_URL)
        if index is None:
            return None
        hrefs = re.findall(r'href="([^"]*patchnotes[^"]*)"', index)
        for href in dict.fromkeys(hrefs):  # page order, de-duplicated
            low = href.lower()
            if marker in low and "patch-notes" in low \
                    and (not exclude or exclude not in low):
                if href.startswith("/"):
                    href = "https://www.callofduty.com" + href
                return href
        return None

    async def _cod_page_latest(self, url: str):
        html = await self._get_text(url)
        if html is None:
            return None
        dates = DATE_RE.findall(html)
        if not dates:
            log.warning("COD page fetched but no dated sections: %s", url)
            return None
        latest = max(dates,
                     key=lambda d: datetime.strptime(d, "%B %d, %Y"))
        return url, latest

    async def _feed_cod(self):
        url = await self._cod_current_url("modern-warfare-4", "warzone")
        found = await self._cod_page_latest(url or COD_URL)
        if found is None:
            return None
        url, latest = found
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        return [_item(f"cod-{slug}-{latest}",
                      f"Patch notes updated - {latest}", url)]

    async def _feed_warzone(self):
        url = await self._cod_current_url("warzone")
        found = await self._cod_page_latest(url or WARZONE_URL)
        if found is None:
            return None
        url, latest = found
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        return [_item(f"wz-{slug}-{latest}",
                      f"Patch notes updated - {latest}", url)]

    async def _get_text_browser(self, url: str) -> str | None:
        """Fetch through curl_cffi with Chrome TLS impersonation - only
        for hosts behind Akamai bot mitigation (Rockstar Support), where
        plain aiohttp completes the TLS handshake and then never gets
        an answer. curl_cffi is synchronous, so it runs in a worker
        thread and never blocks the event loop."""
        def _fetch() -> str | None:
            from curl_cffi import requests as cffi_requests
            resp = cffi_requests.get(url, impersonate="chrome", timeout=30)
            if resp.status_code != 200:
                log.warning("fetch %s -> HTTP %s (curl_cffi)", url,
                            resp.status_code)
                return None
            return resp.text
        try:
            return await asyncio.to_thread(_fetch)
        except Exception:
            log.exception("fetch failed (curl_cffi): %s", url)
            return None

    async def _feed_gta6(self):
        """GTA VI title updates from Rockstar Support (2026-11-24).

        None (not []) when the fetch fails or the article has no dated
        entries, so the feed-health alert fires instead of the feed
        looking healthy - never guess from the page shape."""
        html = await self._get_text_browser(GTA_SUPPORT_URL)
        if html is None:
            return None
        items = parse_gta_support(html)
        if not items:
            log.warning("GTA Support article fetched but no dated "
                        "entries found - page may have changed shape")
            return None
        return items

    async def _feed_nvidia(self):
        data = await self._get_json(NVIDIA_URL)
        if not data:
            return None
        return parse_nvidia(data)

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

    async def _feed_bleach(self):
        return await self._itunes_app(6758332966)

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
            m = re.match(r"iOS (\d+)", title)
            if not m:
                continue
            if ("beta" in title.lower()
                    or "Release Candidate" in title
                    or re.search(r"\bRC\b", title)):
                continue
            link = (node.findtext("link") or "").strip()
            items.append((int(m.group(1)),
                          _item(node.findtext("guid") or title, title,
                                link)))
        if not items:
            return []
        # Only the current iOS generation can install on Victor's
        # iPhone 18 Pro Max. The RSS also carries maintenance releases
        # for old devices (iOS 18.x and older) - those are noise here.
        # Track the newest major present, floored at the major of the
        # last item this feed accepted (so the line can only ever move
        # forward, never back down to a legacy branch), and return
        # just that line's newest release.
        majors = [major for major, _ in items]
        stored = self.state.get("ios", {}).get("label", "")
        sm = re.match(r"iOS (\d+)", stored)
        if sm:
            majors.append(int(sm.group(1)))
        target = max(majors)
        current = [it for major, it in items if major == target]
        if not current:
            return []  # current line scrolled out of the RSS window
        return current[:1]

    async def _feed_samsung(self):
        html = await self._get_text(SAMSUNG_URL)
        if html is None:
            return None
        best = None  # (release date, build)
        # <tr[^>]*> not a bare <tr>: the parser must survive SamMobile
        # adding attributes to its table rows.
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            m = re.search(r"/SM-S918U/TMB/download/(S918U[A-Z0-9]+)/",
                          row)
            if not m:
                continue
            d = re.search(r"(20\d\d-\d\d-\d\d)", row)
            if not d:
                continue
            if best is None or d.group(1) > best[0]:
                best = (d.group(1), m.group(1))
        if best is None:
            log.warning("Samsung archive fetched but no dated TMB "
                        "builds found")
            return None
        when, build = best
        return [_item(f"sam-{build}", f"Firmware {build}", SAMSUNG_URL,
                      extra=when)]

    async def _feed_xiaomi_pad8(self):
        html = await self._get_text(XIAOMI_URL)
        if html is None:
            return None
        best = None  # (version tuple, full version string)
        for m in re.finditer(r"OS(\d+(?:\.\d+)+)\.([A-Z]PZMIXM)\b",
                             html):
            ver = tuple(int(x) for x in m.group(1).split("."))
            if best is None or ver > best[0]:
                best = (ver, f"OS{m.group(1)}.{m.group(2)}")
        if best is None:
            log.warning("Xiaomi page fetched but no MIXM global build "
                        "found for yupei")
            return None
        return [_item(f"pad8-{best[1]}", best[1], XIAOMI_URL)]

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
                    CHANNEL_NAME, topic="Game and device update posts "
                                        "from VicBot.")
                log.info("created #%s in %s", CHANNEL_NAME, guild.name)
                self._channel = channel
                return channel
            except Exception:
                log.exception("could not create #%s in %s",
                              CHANNEL_NAME, guild.name)
        log.error("no #%s channel available in any guild", CHANNEL_NAME)
        return None

    # ---------- feed health: dead feeds must not look healthy ----------

    def _load_health(self) -> dict:
        try:
            with open(HEALTH_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("feed health load failed - starting empty")
        return {}

    def _save_health(self) -> None:
        try:
            folder = os.path.dirname(HEALTH_FILE)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = HEALTH_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.health, fh, indent=1, sort_keys=True)
            os.replace(tmp, HEALTH_FILE)
        except Exception:
            log.exception("feed health save failed")

    def _health_entry(self, key: str) -> dict:
        return self.health.setdefault(
            key, {"last_ok": None, "last_fail": None, "first_fail": None,
                  "alerted": False})

    def _record_ok(self, key: str) -> None:
        entry = self._health_entry(key)
        entry["last_ok"] = datetime.now(timezone.utc).isoformat()
        entry["last_fail"] = None
        entry["first_fail"] = None
        if entry["alerted"]:
            entry["alerted"] = False
            log.info("feed %s recovered - alert flag cleared", key)
        self._save_health()

    async def _record_fail(self, feed: dict) -> None:
        key = feed["key"]
        entry = self._health_entry(key)
        now = datetime.now(timezone.utc)
        entry["last_fail"] = now.isoformat()
        if entry.get("first_fail") is None:
            # Start of this failure streak - the alert clock measures
            # ~24h of CONTINUOUS failure, so a feed that never had a
            # good check does not alert on its very first bad poll.
            entry["first_fail"] = now.isoformat()
        self._save_health()
        if entry["alerted"]:
            return
        first_fail = self._parse_iso(entry.get("first_fail"))
        if first_fail is None:
            return
        if (now - first_fail).total_seconds() \
                < FEED_ALERT_AFTER_HOURS * 3600:
            return
        entry["alerted"] = True
        self._save_health()
        last_ok = self._parse_iso(entry.get("last_ok"))
        since = last_ok.strftime("%b %d, %Y") if last_ok else "never"
        try:
            from bot import audit
            await audit(None,
                        f"⚠️ **Tracker feed {feed['name']} has had no good "
                        f"check in ~{FEED_ALERT_AFTER_HOURS}h** (last good: "
                        f"{since}). Its source may be down or changed - "
                        f"!updates still shows the last known label.")
        except Exception:
            log.exception("feed health alert failed for %s", key)

    def _stale_note(self, key: str) -> str:
        """What !updates appends when a feed's label is older than it
        looks: quiet while healthy, explicit once it goes stale."""
        entry = self.health.get(key) or {}
        last_ok = self._parse_iso(entry.get("last_ok"))
        if last_ok is None:
            return (" (no good check yet - failing)"
                    if entry.get("last_fail") else "")
        age_h = (datetime.now(timezone.utc) - last_ok).total_seconds() / 3600
        if age_h >= FEED_ALERT_AFTER_HOURS:
            return f" (no good check since {last_ok.strftime('%b %d')})"
        return ""

    def _post_target(self, feed: dict, channel):
        pref = feed.get("channel")
        if pref and channel is not None:
            found = discord.utils.get(channel.guild.text_channels,
                                      name=pref)
            if found is not None:
                return found
        return channel

    async def _run_set_feed(self, feed: dict, items, channel) -> None:
        """Free-game feeds: the source returns an unordered set, so one
        stored 'newest' id can both miss a drop and repost one. Track
        the whole set of seen ids instead; only items actually posted
        join the set, so a backlog is never dropped, just deferred."""
        key = feed["key"]
        seen_ids = set(self.state.get(key + ":seen") or [])
        fresh = [it for it in items if it["id"] not in seen_ids]
        if not fresh:
            keep = [it["id"] for it in items]
            keep += [i for i in seen_ids if i not in keep]
            self.state[key + ":seen"] = keep[:MAX_SEEN_IDS]
            self._save()
            return
        target = self._post_target(feed, channel)
        posted = None
        posted_ids = []
        for it in reversed(fresh[-MAX_POSTS_PER_FEED:]):
            await self._post(feed, it, target)
            posted = it
            posted_ids.append(it["id"])
        keep = posted_ids + [i for i in (it["id"] for it in items)
                             if i in seen_ids]
        keep += [i for i in seen_ids if i not in keep]
        self.state[key + ":seen"] = keep[:MAX_SEEN_IDS]
        if posted is not None:
            self.state[key] = {"id": posted["id"],
                               "label": posted["label"]}
        self._save()

    async def _run_feed(self, feed: dict, channel) -> None:
        key = feed["key"]
        try:
            items = await getattr(self, feed["fetch"])()
        except Exception:
            log.exception("feed %s crashed", key)
            await self._record_fail(feed)
            return
        if items is None:
            # Fetch failed; state untouched, retry next run.
            await self._record_fail(feed)
            return
        self._record_ok(key)
        seen = self.state.get(key)
        if seen is None:
            # First successful contact seeds silently so old news never
            # posts. If nothing relevant exists yet (a filtered feed
            # before its first patch), seed EMPTY instead - otherwise
            # the first real patch would be swallowed as the seed.
            newest = items[0] if items else {"id": "", "label": "(none yet)"}
            self.state[key] = {"id": newest["id"], "label": newest["label"]}
            if feed.get("set"):
                self.state[key + ":seen"] = [it["id"] for it in items]
            self._save()
            log.info("feed %s seeded at %s", key, newest["label"])
            return
        if not items:
            return  # fetched fine, nothing relevant right now
        if feed.get("set"):
            await self._run_set_feed(feed, items, channel)
            return
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
        # Oldest fresh items first, capped per run: overflow is never
        # dropped, it just posts on the next poll.
        fresh = fresh[-MAX_POSTS_PER_FEED:]
        target = self._post_target(feed, channel)
        posted = None
        for it in reversed(fresh):  # oldest first
            await self._post(feed, it, target)
            posted = it
        if posted is not None:
            # Advance only to the newest item actually posted, so a
            # mid-batch failure reposts just the failed item, not the
            # ones that already went out.
            self.state[key] = {"id": posted["id"],
                               "label": posted["label"]}
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
        except (discord.NotFound, discord.Forbidden):
            # The target channel was deleted (or the bot lost access).
            # Drop the cached channel so the next poll re-resolves it
            # (or recreates it) instead of failing forever.
            log.warning("post target gone for feed %s - clearing channel "
                        "cache", feed["key"])
            self._channel = None
            raise
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
                             f"{seen['label']}{self._stale_note(feed['key'])}")
            else:
                lines.append(f"{feed['emoji']} **{feed['name']}** - "
                             "no data yet (seeds on the first good check)")
        await ctx.send("\n".join(lines))


FEEDS = [
    {"key": "nba2k27", "name": "NBA 2K27 (PS5)", "emoji": "🏀",
     "color": 0xE03A3A, "fetch": "_feed_nba2k27"},
    {"key": "cod-mw4", "name": "COD: Modern Warfare 4", "emoji": "🔫",
     "color": 0x8A9A5B, "fetch": "_feed_cod"},
    {"key": "cod-warzone", "name": "COD: Warzone", "emoji": "🪂",
     "color": 0x4C7C9B, "fetch": "_feed_warzone"},
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
    {"key": "bleach-mirrors-high", "name": "BLEACH Mirrors High",
     "emoji": "🗡️", "color": 0xE67E22, "fetch": "_feed_bleach"},
    {"key": "ios", "name": "iOS Releases", "emoji": "🍎",
     "color": 0xA2AAAD, "fetch": "_feed_ios"},
    {"key": "samsung-s23u", "name": "Galaxy S23 Ultra Firmware",
     "emoji": "📱", "color": 0x1428A0, "fetch": "_feed_samsung"},
    {"key": "xiaomi-pad8", "name": "Xiaomi Pad 8 HyperOS",
     "emoji": "📲", "color": 0xFF6900, "fetch": "_feed_xiaomi_pad8"},
    {"key": "epic-free", "name": "Epic Free Games", "emoji": "🎁",
     "color": 0x444444, "fetch": "_feed_epic_free", "channel": "free-games",
     "set": True},
    {"key": "steam-free", "name": "Steam Freebies", "emoji": "🎁",
     "color": 0x1B78C4, "fetch": "_feed_steam_free",
     "channel": "free-games", "set": True},
    {"key": "gog-free", "name": "GOG Giveaways", "emoji": "🎁",
     "color": 0x86328A, "fetch": "_feed_gog_free", "channel": "free-games",
     "set": True},
]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Updates(bot))
