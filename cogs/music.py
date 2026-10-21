"""Music module: per-server queue player on yt-dlp + ffmpeg.

Private-bot economics: no YouTube legal target on its back (that kills
the big public bots), but YouTube still changes its player every few
weeks - when playback breaks, update yt-dlp first:
    docker compose exec house-bot pip install -U yt-dlp
    docker compose restart house-bot

Design rule for this module (learned the hard way): NEVER die silently.
Every failure - extraction, voice connect, ffmpeg, opus - is reported
in the text channel and the log, with the track skipped, not the whole
player killed.

Spotify: no bot can stream Spotify audio (DRM). Pasting a Spotify link
reads the track names and queues the YouTube matches - which is what
every "Spotify bot" actually does. Single tracks work with zero setup
(Spotify's public oEmbed). Playlists need free Spotify API credentials
in .env (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET) - see README.
"""

import asyncio
import json
import logging
import os
import re
import urllib.parse
import urllib.request

import discord
import yt_dlp
from discord.ext import commands

log = logging.getLogger("housebot.music")

YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = "-vn"

SPOTIFY_ID = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
PLAYLIST_CAP = 100


class Track:
    def __init__(self, title: str, requester: str,
                 stream_url: str = "", webpage: str = "", lazy_query: str = ""):
        self.title = title
        self.requester = requester
        self.stream_url = stream_url
        self.webpage = webpage
        # Playlist entries arrive as names only; the stream is resolved
        # right before playback so adding 80 songs stays instant.
        self.lazy_query = lazy_query

    def source(self) -> discord.AudioSource:
        return discord.FFmpegPCMAudio(
            self.stream_url, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTS
        )


def _track_from_info(info: dict, requester: str) -> Track:
    return Track(
        title=info.get("title", "Unknown"),
        requester=requester,
        stream_url=info["url"],
        webpage=info.get("webpage_url", ""),
    )


def _extract_url(url: str) -> dict:
    """Full extraction of one direct video URL."""
    with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
        if info is None:
            raise RuntimeError("nothing found there")
        if "entries" in info:
            info = info["entries"][0]
        return info


def _search_candidates(query: str, limit: int = 5) -> list[str]:
    """Top search-result URLs, flat (cheap) - no full extraction yet."""
    opts = dict(YTDL_OPTS)
    opts["extract_flat"] = True
    opts.pop("default_search", None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        entries = (info or {}).get("entries") or []
        return [e.get("url") for e in entries if e.get("url")]


async def resolve(query: str, requester: str) -> Track:
    """Turn a search term or URL into a playable Track.

    Searches try up to 5 results in order: YouTube's top hit is
    regularly a dead or region-locked upload ("This video is not
    available"), and giving up on hit #1 was half the failure rate.
    """
    loop = asyncio.get_running_loop()
    if query.startswith(("http://", "https://")):
        info = await loop.run_in_executor(None, _extract_url, query)
        return _track_from_info(info, requester)

    candidates = await loop.run_in_executor(None, _search_candidates, query)
    if not candidates:
        raise RuntimeError("nothing found for that search")
    last_error: Exception | None = None
    for url in candidates:
        try:
            info = await loop.run_in_executor(None, _extract_url, url)
            return _track_from_info(info, requester)
        except Exception as e:  # unavailable / age-gated / region-locked - try next
            last_error = e
    raise RuntimeError(f"the top {len(candidates)} results were all unplayable "
                       f"(last error: {last_error})")


# ---------------- Spotify ----------------

def _spotify_oembed(url: str) -> tuple[str, str]:
    """(title, author) for a Spotify track link - no credentials needed."""
    req = urllib.request.Request(
        "https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe=""),
        headers={"User-Agent": "HouseBot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("title", ""), data.get("author_name", "")


def _spotify_playlist_names(url: str) -> list[str]:
    """Track names ('Artist - Title') from a playlist/album link, via API."""
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials

    sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=SPOTIFY_ID, client_secret=SPOTIFY_SECRET))
    names = []
    if "playlist" in url:
        results = sp.playlist_items(url, limit=100,
                                    fields="items.track(name,artists(name)),next")
        while results and len(names) < PLAYLIST_CAP:
            for item in results.get("items", []):
                track = item.get("track") or {}
                artists = ", ".join(a["name"] for a in track.get("artists", []))
                if track.get("name"):
                    names.append(f"{artists} - {track['name']}")
            results = sp.next(results) if results.get("next") else None
    elif "album" in url:
        album = sp.album(url)
        artists = ", ".join(a["name"] for a in album.get("artists", []))
        for track in album.get("tracks", {}).get("items", [])[:PLAYLIST_CAP]:
            names.append(f"{artists} - {track['name']}")
    return names


async def expand_query(query: str, requester: str) -> list[Track]:
    """One user query -> one or more queue entries (Spotify playlists fan out)."""
    if "open.spotify.com" not in query:
        return [await resolve(query, requester)]

    loop = asyncio.get_running_loop()
    if "/track/" in query:
        title, author = await loop.run_in_executor(None, _spotify_oembed, query)
        search = f"{author} {title}".strip() or query
        return [await resolve(search, requester)]

    if "/playlist/" in query or "/album/" in query:
        if not (SPOTIFY_ID and SPOTIFY_SECRET):
            raise RuntimeError(
                "Spotify playlists need free API credentials in the bot's .env "
                "(SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET - 2 min setup, see README). "
                "Single Spotify tracks work without them.")
        names = await loop.run_in_executor(None, _spotify_playlist_names, query)
        if not names:
            raise RuntimeError("that Spotify link came back empty")
        return [Track(title=n, requester=requester, lazy_query=n) for n in names]

    raise RuntimeError("that Spotify link type isn't supported - use a track, album or playlist link")


# ---------------- player ----------------

class GuildPlayer:
    def __init__(self):
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.current: Track | None = None
        self.task: asyncio.Task | None = None
        self.text_channel: discord.TextChannel | None = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        if not discord.opus.is_loaded():
            for lib in ("libopus.so.0", "libopus.so", "opus"):
                try:
                    discord.opus.load_opus(lib)
                    break
                except Exception:
                    continue
        log.info("opus loaded: %s", discord.opus.is_loaded())

    def player(self, guild_id: int) -> GuildPlayer:
        return self.players.setdefault(guild_id, GuildPlayer())

    async def _announce(self, p: GuildPlayer, text: str):
        if p.text_channel:
            try:
                await p.text_channel.send(text[:500])
            except Exception:
                pass

    async def _run_queue(self, guild: discord.Guild):
        p = self.player(guild.id)
        try:
            while True:
                vc = guild.voice_client
                if vc is None or not vc.is_connected():
                    await self._announce(p, "I lost the voice connection - queue stopped. "
                                            "Run !play again when you're in a channel.")
                    return
                try:
                    track = await asyncio.wait_for(p.queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    # Idle 5 minutes with an empty queue - leave the channel.
                    await vc.disconnect()
                    return
                # Lazy entries (from Spotify playlists) resolve right now.
                if not track.stream_url and track.lazy_query:
                    try:
                        real = await resolve(track.lazy_query, track.requester)
                        track.stream_url, track.title, track.webpage = \
                            real.stream_url, real.title, real.webpage
                    except Exception as e:
                        await self._announce(p, f"Skipped **{track.title}** - couldn't load it: {e}")
                        continue
                p.current = track
                try:
                    source = track.source()
                except Exception as e:
                    log.exception("ffmpeg source failed")
                    await self._announce(p, f"Skipped **{track.title}** - audio engine error: {e}")
                    p.current = None
                    continue
                await self._announce(p, f"Now playing: **{track.title}** (added by {track.requester})")
                done = asyncio.Event()
                try:
                    vc.play(source, after=lambda _e: self.bot.loop.call_soon_threadsafe(done.set))
                except Exception as e:
                    log.exception("voice play failed")
                    await self._announce(p, f"Couldn't start **{track.title}**: {e}")
                    p.current = None
                    continue
                await done.wait()
                p.current = None
        except Exception:
            log.exception("queue runner died")
            await self._announce(p, "The music player hit an error and stopped - "
                                    "the details are in the bot log. Run !play to restart it.")
        finally:
            p.task = None

    @commands.command(name="play")
    async def play(self, ctx: commands.Context, *, query: str):
        """!play <song, URL, or Spotify link> - add to the queue."""
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("Join a voice channel first, then !play.")
            return
        channel = ctx.author.voice.channel
        vc = ctx.guild.voice_client
        try:
            if vc is None:
                vc = await channel.connect(reconnect=True)
            elif vc.channel != channel:
                await vc.move_to(channel)
        except Exception as e:
            log.exception("voice connect failed")
            await ctx.send(f"I couldn't join {channel.name}: {e}\n"
                           "Usual causes: I'm missing Connect/Speak in that channel, "
                           "or the audio codec failed to load (the bot log will say).")
            return

        async with ctx.typing():
            try:
                tracks = await expand_query(query, ctx.author.display_name)
            except Exception as e:
                await ctx.send(f"Couldn't load that: {e}")
                return
        p = self.player(ctx.guild.id)
        p.text_channel = ctx.channel
        for t in tracks:
            await p.queue.put(t)
        if len(tracks) == 1:
            await ctx.send(f"Queued: **{tracks[0].title}**")
        else:
            await ctx.send(f"Queued **{len(tracks)} tracks** from that link.")
        if p.task is None or p.task.done():
            p.task = asyncio.create_task(self._run_queue(ctx.guild))

    @commands.command(name="skip")
    async def skip(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await ctx.send("Skipped.")
        else:
            await ctx.send("Nothing is playing.")

    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await ctx.send("Paused.")

    @commands.command(name="resume")
    async def resume(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await ctx.send("Resumed.")

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context):
        """Stop playback and clear the queue (stays in the channel)."""
        p = self.player(ctx.guild.id)
        while not p.queue.empty():
            p.queue.get_nowait()
        vc = ctx.guild.voice_client
        if vc:
            vc.stop()
        await ctx.send("Stopped, queue cleared.")

    @commands.command(name="leave")
    async def leave(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        if vc:
            await vc.disconnect()
            await ctx.send("Left the voice channel.")

    @commands.command(name="queue")
    async def show_queue(self, ctx: commands.Context):
        p = self.player(ctx.guild.id)
        lines = []
        if p.current:
            lines.append(f"Now: **{p.current.title}**")
        pending = list(p.queue._queue)  # display only
        for i, t in enumerate(pending, 1):
            lines.append(f"{i}. {t.title}")
            if i >= 15:
                lines.append(f"...and {len(pending) - 15} more")
                break
        await ctx.send("\n".join(lines) if lines else "Queue is empty.")

    @commands.command(name="np")
    async def now_playing(self, ctx: commands.Context):
        p = self.player(ctx.guild.id)
        if p.current:
            await ctx.send(f"Now playing: **{p.current.title}**\n{p.current.webpage}")
        else:
            await ctx.send("Nothing is playing.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
