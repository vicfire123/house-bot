"""Media module: !request a TV show or movie, down to one episode.

  !request Stranger Things           whole show (all seasons monitored + searched)
  !request Stranger Things season 1  only season 1 monitored + searched
  !request Stranger Things S03E04    only that episode searched
  !request Dune                      movie via Radarr (add + search)
  !request Dune (2021)               a year in brackets narrows the match

Routing: a season/episode selector means TV (Sonarr). A bare title is
looked up in both Sonarr and Radarr; if more than one title matches
closely the bot asks which one instead of guessing.

Who: only user IDs in ADMIN_USER_IDS, silent to everyone else - the
same whitelist the admin module uses.

Defaults: new titles are added with the root folder / quality profile
/ tags of Seerr's default (non-4K) Sonarr and Radarr servers, read
live from Seerr's API, so a bot request lands exactly where a Seerr
request does. Without Seerr, explicit *_ROOT_FOLDER and
*_QUALITY_PROFILE_ID values in .env are used. If neither is set the
bot refuses to add - it never guesses a folder or profile, and it
never changes a Sonarr/Radarr setting.

Keys live in .env (SONARR_API_KEY / RADARR_API_KEY / SEERR_API_KEY),
are sent only in the X-Api-Key header and never appear in replies.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger("housebot.media")

HTTP_TIMEOUT = 30    # seconds - *arr lookups proxy to TVDB/TMDB
CHOICE_TIMEOUT = 60  # seconds the "which one?" buttons stay live
MAX_CHOICES = 4      # per kind (TV / movie) on a "which one?" reply


class ArrError(Exception):
    """A failure with a short, chat-safe message."""


# ---------------- parsing ----------------

@dataclass
class MediaRequest:
    title: str
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None


# Selectors are anchored at the END of the text and need a number, so
# titles like "Season of the Witch" pass through untouched.
_SELECTORS = (
    re.compile(r"\s+s(\d{1,2})\s*e(\d{1,3})$", re.IGNORECASE),
    re.compile(r"\s+season\s+(\d{1,2})\s+episode\s+(\d{1,3})$", re.IGNORECASE),
    re.compile(r"\s+(?:season\s+|s)(\d{1,2})$", re.IGNORECASE),
)
_YEAR = re.compile(r"\s*\(((?:19|20)\d{2})\)$")


def parse_request(text: str) -> MediaRequest:
    text = " ".join(text.split())
    req = MediaRequest(title=text)
    for pattern in _SELECTORS:
        m = pattern.search(text)
        if m:
            req.season = int(m.group(1))
            if m.lastindex == 2:
                req.episode = int(m.group(2))
            text = text[:m.start()]
            break
    m = _YEAR.search(text)
    if m and text[:m.start()].strip():
        req.year = int(m.group(1))
        text = text[:m.start()]
    req.title = text.strip()
    return req


# ---------------- matching ----------------

def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower().replace("&", "and"))


def close_matches(results: list, title: str, year: Optional[int]) -> list:
    """Lookup results that match the asked title closely.

    Exact title matches win (several = remakes, the caller asks).
    With no exact match the top few lookup hits come back so the
    caller asks rather than guesses."""
    if year is not None:
        results = [r for r in results if r.get("year") == year]
    exact = [r for r in results if _norm(r.get("title", "")) == _norm(title)]
    return (exact or results)[:MAX_CHOICES]


def is_exact(item: dict, title: str) -> bool:
    return _norm(item.get("title", "")) == _norm(title)


def label(kind: str, item: dict) -> str:
    return f"{item.get('title', '?')} ({item.get('year') or '?'})" \
           f"{' - TV' if kind == 'tv' else ' - movie'}"


# ---------------- defaults (Seerr's, never invented) ----------------

def seerr_default_server(servers: list) -> Optional[dict]:
    """The server Seerr sends ordinary (non-4K) requests to."""
    for s in servers or []:
        if s.get("isDefault") and not s.get("is4k"):
            return s
    return None


def tv_defaults(server: dict, anime: bool) -> dict:
    """Same choice Seerr makes: the anime profile/folder when the show
    is anime and both are configured, else the standard ones."""
    use_anime = bool(anime and server.get("activeAnimeProfileId")
                     and server.get("activeAnimeDirectory"))
    pre = "activeAnime" if use_anime else "active"
    return {
        "qualityProfileId": server.get(f"{pre}ProfileId"),
        "rootFolderPath": server.get(f"{pre}Directory"),
        "languageProfileId": server.get(f"{pre}LanguageProfileId"),
        "tags": list(server.get("animeTags" if use_anime else "tags") or []),
        "seasonFolder": server.get("enableSeasonFolders", True),
        "seriesType": server.get("animeSeriesType" if anime else "seriesType") or "standard",
        "monitorNewItems": server.get("monitorNewItems") or "all",
    }


def movie_defaults(server: dict) -> dict:
    return {
        "qualityProfileId": server.get("activeProfileId"),
        "rootFolderPath": server.get("activeDirectory"),
        "minimumAvailability": server.get("minimumAvailability") or "released",
        "tags": list(server.get("tags") or []),
    }


def env_defaults(prefix: str) -> Optional[dict]:
    """Explicit .env fallback for a setup without Seerr."""
    root = os.environ.get(f"{prefix}_ROOT_FOLDER", "").strip()
    profile = os.environ.get(f"{prefix}_QUALITY_PROFILE_ID", "").strip()
    if not root or not profile.isdigit():
        return None
    return {"qualityProfileId": int(profile), "rootFolderPath": root,
            "languageProfileId": None, "tags": [], "seasonFolder": True,
            "seriesType": "standard", "monitorNewItems": "all",
            "minimumAvailability": "released"}


NO_DEFAULTS = ("Can't add it: no root folder / quality profile to use. "
               "Set SEERR_API_KEY in .env (or the explicit *_ROOT_FOLDER "
               "and *_QUALITY_PROFILE_ID values) and restart me.")


# ---------------- *arr client ----------------

class Arr:
    """Tiny JSON client for Sonarr / Radarr (v3 API) and Seerr (v1)."""

    def __init__(self, name: str, url: str, key: str, api: str = "/api/v3"):
        self.name = name
        self.base = url.rstrip("/") + api
        self.key = key

    async def call(self, method: str, path: str, params: Optional[dict] = None,
                   json: Optional[dict] = None):
        try:
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                        method, self.base + path, params=params, json=json,
                        headers={"X-Api-Key": self.key}) as resp:
                    if resp.status in (401, 403):
                        raise ArrError(f"{self.name} rejected the API key in .env.")
                    if resp.status >= 400:
                        body = " ".join((await resp.text()).split())[:150]
                        raise ArrError(f"{self.name} refused that "
                                       f"(HTTP {resp.status}): {body}")
                    return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.warning("%s call failed: %s %s", self.name, method, path, exc_info=True)
            raise ArrError(f"{self.name} is unreachable from potofgreed.")


# ---------------- TV (Sonarr) ----------------

def _aired(ep: dict) -> bool:
    raw = ep.get("airDateUtc")
    if not raw:
        return False
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    except ValueError:
        return False


async def _search(arr: Arr, name: str, **body) -> None:
    await arr.call("POST", "/command", json={"name": name, **body})


async def _monitor_episodes(sonarr: Arr, episodes: list) -> None:
    ids = [e["id"] for e in episodes if not e.get("monitored")]
    if ids:
        await sonarr.call("PUT", "/episode/monitor",
                          json={"episodeIds": ids, "monitored": True})


async def _find_episode(sonarr: Arr, series_id: int, season: int, episode: int,
                        tries: int, delay: float):
    """(episode or None, season_loaded). A just-added series fills its
    episode list in the background, hence the bounded retry."""
    for attempt in range(tries):
        eps = await sonarr.call("GET", "/episode",
                                params={"seriesId": series_id, "seasonNumber": season})
        for ep in eps:
            if ep.get("episodeNumber") == episode:
                return ep, True
        if eps:
            return None, True
        if attempt < tries - 1:
            await asyncio.sleep(delay)
    return None, False


async def _search_episode(sonarr: Arr, series_id: int, req: MediaRequest,
                          name: str, tries: int, delay: float, added: bool) -> str:
    code = f"S{req.season:02d}E{req.episode:02d}"
    prefix = f"Added **{name}** with nothing else monitored. " if added else ""
    ep, loaded = await _find_episode(sonarr, series_id, req.season, req.episode, tries, delay)
    if ep is None:
        if loaded:
            return f"{prefix}**{name}** has no {code} - nothing searched."
        return (f"{prefix}Sonarr is still loading its episode list - run "
                f"the same request again in a minute to search {code}.")
    if ep.get("hasFile"):
        return f"Already have **{name}** {code} - nothing to do."
    await _monitor_episodes(sonarr, [ep])
    await _search(sonarr, "EpisodeSearch", episodeIds=[ep["id"]])
    return f"{prefix}Searching **{name}** {code} - only that episode."


async def request_tv(sonarr: Arr, series: dict, req: MediaRequest,
                     defaults: Optional[dict], tries: int = 10,
                     delay: float = 3.0) -> str:
    """Do one TV request against Sonarr; returns the reply line."""
    name = f"{series.get('title', '?')} ({series.get('year') or '?'})"
    numbers = [s["seasonNumber"] for s in series.get("seasons", [])]
    if req.season is not None and req.season not in numbers:
        return f"**{name}** has no season {req.season}."

    tvdb = series["tvdbId"]
    found = await sonarr.call("GET", "/series", params={"tvdbId": tvdb})
    existing = next((s for s in found or [] if s.get("tvdbId") == tvdb), None)

    # ---- not in Sonarr yet: add it ----
    if existing is None:
        if not defaults or not defaults.get("rootFolderPath") \
                or not defaults.get("qualityProfileId"):
            return NO_DEFAULTS
        whole, one_episode = req.season is None, req.episode is not None
        payload = {
            "tvdbId": tvdb,
            "title": series.get("title"),
            "qualityProfileId": defaults["qualityProfileId"],
            "rootFolderPath": defaults["rootFolderPath"],
            "seasonFolder": defaults["seasonFolder"],
            "seriesType": defaults["seriesType"],
            "tags": defaults["tags"],
            "monitored": True,
            # One-episode request: later seasons must not auto-monitor.
            "monitorNewItems": "none" if one_episode else defaults["monitorNewItems"],
            "seasons": [{"seasonNumber": n,
                         "monitored": not one_episode and n > 0
                         and (whole or n == req.season)} for n in numbers],
            # Sonarr searches only monitored episodes on add, so this
            # covers "all seasons" and "just season N" alike.
            "addOptions": {"ignoreEpisodesWithFiles": True,
                           "searchForMissingEpisodes": not one_episode},
        }
        if one_episode:
            payload["addOptions"]["monitor"] = "none"
        if defaults.get("languageProfileId"):
            payload["languageProfileId"] = defaults["languageProfileId"]
        created = await sonarr.call("POST", "/series", json=payload)
        if not (created or {}).get("id"):
            return f"Sonarr did not accept **{name}**."
        if one_episode:
            return await _search_episode(sonarr, created["id"], req, name,
                                         tries, delay, added=True)
        if whole:
            return f"Added **{name}** - all seasons monitored, searching the series."
        return (f"Added **{name}** - only season {req.season} monitored, "
                f"searching season {req.season}.")

    # ---- already in Sonarr ----
    sid = existing["id"]
    if req.episode is not None:
        return await _search_episode(sonarr, sid, req, name, 1, 0, added=False)

    params = {"seriesId": sid}
    if req.season is not None:
        params["seasonNumber"] = req.season
    episodes = [e for e in await sonarr.call("GET", "/episode", params=params)
                if e.get("seasonNumber", 0) > 0 or req.season == 0]
    aired = [e for e in episodes if _aired(e)]
    what = f"season {req.season}" if req.season is not None else "every season"
    if aired and all(e.get("hasFile") for e in aired):
        return (f"Already have **{name}** {what} - all {len(aired)} aired "
                f"episodes are downloaded. Nothing to do.")

    # Monitoring is only ever ADDED: other seasons keep their state.
    wanted = [s for s in existing.get("seasons", [])
              if (s["seasonNumber"] == req.season if req.season is not None
                  else s["seasonNumber"] > 0)]
    if not existing.get("monitored") or any(not s.get("monitored") for s in wanted):
        existing["monitored"] = True
        for s in wanted:
            s["monitored"] = True
        await sonarr.call("PUT", f"/series/{sid}", json=existing)
    await _monitor_episodes(sonarr, episodes)
    if req.season is not None:
        await _search(sonarr, "SeasonSearch", seriesId=sid, seasonNumber=req.season)
        return (f"**{name}** is already in Sonarr - season {req.season} "
                f"monitored, searching season {req.season}.")
    await _search(sonarr, "SeriesSearch", seriesId=sid)
    return f"**{name}** is already in Sonarr - all seasons monitored, searching the series."


# ---------------- movies (Radarr) ----------------

async def request_movie(radarr: Arr, movie: dict, defaults: Optional[dict]) -> str:
    """Do one movie request against Radarr; returns the reply line."""
    name = f"{movie.get('title', '?')} ({movie.get('year') or '?'})"
    tmdb = movie["tmdbId"]
    found = await radarr.call("GET", "/movie", params={"tmdbId": tmdb})
    existing = next((m for m in found or [] if m.get("tmdbId") == tmdb), None)

    if existing is not None:
        if existing.get("hasFile"):
            return f"Already have **{name}** - nothing to do."
        if not existing.get("monitored"):
            existing["monitored"] = True
            await radarr.call("PUT", f"/movie/{existing['id']}", json=existing)
        await _search(radarr, "MoviesSearch", movieIds=[existing["id"]])
        return f"**{name}** is already in Radarr - monitored, searching."

    if not defaults or not defaults.get("rootFolderPath") \
            or not defaults.get("qualityProfileId"):
        return NO_DEFAULTS
    created = await radarr.call("POST", "/movie", json={
        "tmdbId": tmdb,
        "title": movie.get("title"),
        "year": movie.get("year"),
        "titleSlug": str(tmdb),
        "qualityProfileId": defaults["qualityProfileId"],
        "rootFolderPath": defaults["rootFolderPath"],
        "minimumAvailability": defaults["minimumAvailability"],
        "tags": defaults["tags"],
        "monitored": True,
        "addOptions": {"searchForMovie": True},
    })
    if not (created or {}).get("id"):
        return f"Radarr did not accept **{name}**."
    return f"Added **{name}** to Radarr - monitored, searching."


# ---------------- "which one?" buttons ----------------

class ChoiceView(discord.ui.View):
    """One button per close match. Only the requester can press, once."""

    def __init__(self, requester_id: int, choices: list, on_pick):
        super().__init__(timeout=CHOICE_TIMEOUT)
        self.requester_id = requester_id
        self.on_pick = on_pick
        self.decided = False
        for kind, item in choices:
            button = discord.ui.Button(label=label(kind, item)[:80],
                                       style=discord.ButtonStyle.primary)
            button.callback = self._picker(kind, item)
            self.add_item(button)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._picker(None, None)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who asked can pick.", ephemeral=True)
            return False
        return True

    def _picker(self, kind, item):
        async def callback(interaction: discord.Interaction):
            if self.decided:
                return
            self.decided = True
            self.stop()
            if item is None:
                await interaction.response.edit_message(
                    content="Cancelled - nothing requested.", view=None)
                return
            await interaction.response.edit_message(
                content=f"Working on {label(kind, item)}...", view=None)
            result = await self.on_pick(kind, item)
            await interaction.edit_original_response(content=result)
        return callback


# ---------------- the cog ----------------

class Media(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        from bot import ADMIN_USER_IDS, PREFIX, audit  # cogs load after core
        self.admin_ids = ADMIN_USER_IDS
        self.prefix = PREFIX
        self.audit = audit
        env = lambda k, d="": os.environ.get(k, "").strip() or d  # noqa: E731
        self.urls = {"Sonarr": env("SONARR_URL", "http://192.168.1.92:8989"),
                     "Radarr": env("RADARR_URL", "http://192.168.1.92:7878")}
        self.keys = {"Sonarr": env("SONARR_API_KEY"), "Radarr": env("RADARR_API_KEY")}
        seerr_key = env("SEERR_API_KEY")
        self.seerr = Arr("Seerr", env("SEERR_URL", "http://192.168.1.92:5055"),
                         seerr_key, api="/api/v1") if seerr_key else None

    async def _service(self, name: str):
        """(client or None, Seerr's default server settings or None).

        Seerr's server entry carries both the defaults and the API key
        Seerr itself uses, so SEERR_API_KEY alone is enough; an explicit
        SONARR_API_KEY / RADARR_API_KEY in .env wins."""
        server = None
        if self.seerr is not None:
            server = seerr_default_server(
                await self.seerr.call("GET", f"/settings/{name.lower()}"))
        key = self.keys[name] or (server or {}).get("apiKey", "")
        return (Arr(name, self.urls[name], key) if key else None), server

    async def _fulfil(self, kind: str, item: dict, req: MediaRequest,
                      sonarr, s_server, radarr, r_server) -> str:
        try:
            if kind == "tv":
                anime = "anime" in [g.lower() for g in item.get("genres") or []]
                defaults = tv_defaults(s_server, anime) if s_server else env_defaults("SONARR")
                return await request_tv(sonarr, item, req, defaults)
            defaults = movie_defaults(r_server) if r_server else env_defaults("RADARR")
            return await request_movie(radarr, item, defaults)
        except ArrError as e:
            return str(e)
        except Exception:
            log.exception("media request failed")
            return "That request hit an unexpected error - check my log."

    @commands.command(name="request")
    async def request(self, ctx: commands.Context, *, query: str = ""):
        """!request <title> [season N | SxxEyy] - Sonarr / Radarr request."""
        if ctx.guild is None or ctx.author.id not in self.admin_ids:
            return  # not whitelisted: total silence, same as the admin module
        req = parse_request(query)
        if not req.title:
            await ctx.reply(f"Usage: `{self.prefix}request <title>` - add "
                            "`season 2` or `S03E04` for part of a show.")
            return

        tv_only = req.season is not None
        try:
            async with ctx.typing():
                sonarr, s_server = await self._service("Sonarr")
                radarr, r_server = (None, None) if tv_only else await self._service("Radarr")
                if sonarr is None and radarr is None:
                    await ctx.reply("No Sonarr/Radarr API key configured - set "
                                    "SEERR_API_KEY (or SONARR_API_KEY / "
                                    "RADARR_API_KEY) in .env and restart me.")
                    return
                choices = []
                if sonarr is not None:
                    hits = await sonarr.call("GET", "/series/lookup", params={"term": req.title})
                    choices += [("tv", h) for h in close_matches(hits, req.title, req.year)]
                if radarr is not None:
                    hits = await radarr.call("GET", "/movie/lookup", params={"term": req.title})
                    choices += [("movie", h) for h in close_matches(hits, req.title, req.year)]
        except ArrError as e:
            await ctx.reply(str(e))
            return

        async def run(kind: str, item: dict) -> str:
            result = await self._fulfil(kind, item, req, sonarr, s_server, radarr, r_server)
            await self.audit(ctx.guild, f"REQUEST by {ctx.author}: `{query}` -> {result}")
            return result

        if not choices:
            await ctx.reply(f"Couldn't find anything called **{req.title}**"
                            f"{' as a TV show' if tv_only else ''}.")
            return
        # Go straight ahead only on a single, exact title match.
        exact = [c for c in choices if is_exact(c[1], req.title)]
        if len(exact) == 1:
            async with ctx.typing():
                await ctx.reply(await run(*exact[0]))
            return
        await ctx.reply("Which one did you mean?",
                        view=ChoiceView(ctx.author.id, exact or choices, run))


async def setup(bot: commands.Bot):
    await bot.add_cog(Media(bot))
