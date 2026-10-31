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
        # Music player panel (spec locked 2026-10-31): one live panel
        # message at a time, replaced fresh on every new track.
        self.panel_message: discord.Message | None = None
        self.panel_view: "PlayerPanelView | None" = None


class PlayerPanelView(discord.ui.View):
    """Buttons on the Now Playing panel.

    Access rule (Victor's call, 2026-10-31): anyone IN the voice
    channel with the bot can press any button, stop included. Anyone
    outside it is ignored with a short ephemeral note. Views live only
    for their track - a fresh panel (and view) is posted per track, so
    nothing here is registered as a persistent view.
    """

    def __init__(self, music: "Music", guild_id: int):
        super().__init__(timeout=None)
        self.music = music
        self.guild_id = guild_id

        self.pause_button = discord.ui.Button(
            label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary)
        self.pause_button.callback = self._toggle_pause
        self.add_item(self.pause_button)

        skip_button = discord.ui.Button(
            label="Skip", emoji="⏭️", style=discord.ButtonStyle.primary)
        skip_button.callback = self._skip
        self.add_item(skip_button)

        stop_button = discord.ui.Button(
            label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger)
        stop_button.callback = self._stop
        self.add_item(stop_button)

        queue_button = discord.ui.Button(
            label="Queue", emoji="📋", style=discord.ButtonStyle.secondary)
        queue_button.callback = self._queue
        self.add_item(queue_button)

    def set_paused(self, paused: bool) -> None:
        self.pause_button.label = "Resume" if paused else "Pause"
        self.pause_button.emoji = "▶️" if paused else "⏸️"

    async def _toggle_pause(self, interaction: discord.Interaction):
        if not await self.music._panel_check(interaction):
            return
        guild = interaction.guild
        vc = guild.voice_client if guild else None
        if vc is None:
            await interaction.response.send_message(
                "Nothing is connected.", ephemeral=True)
            return
        p = self.music.player(guild.id)
        if vc.is_playing():
            vc.pause()
            self.set_paused(True)
            await interaction.response.edit_message(
                embed=self.music._panel_embed(p, paused=True), view=self)
        elif vc.is_paused():
            vc.resume()
            self.set_paused(False)
            await interaction.response.edit_message(
                embed=self.music._panel_embed(p, paused=False), view=self)
        else:
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True)

    async def _skip(self, interaction: discord.Interaction):
        if not await self.music._panel_check(interaction):
            return
        vc = interaction.guild.voice_client if interaction.guild else None
        await interaction.response.defer()
        if vc and (vc.is_playing() or vc.is_paused()):
            # vc.stop() ends the track; the queue runner posts the
            # next track's fresh panel and deletes this one.
            vc.stop()

    async def _stop(self, interaction: discord.Interaction):
        if not await self.music._panel_check(interaction):
            return
        await interaction.response.defer()
        if interaction.guild is not None:
            await self.music._stop_player(interaction.guild)

    async def _queue(self, interaction: discord.Interaction):
        if not await self.music._panel_check(interaction):
            return
        p = self.music.player(interaction.guild.id)
        text = self.music._queue_text(p)
        await interaction.response.send_message(text[:1900], ephemeral=True)


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

    # ---------- music player panel ----------

    def _queue_text(self, p: GuildPlayer) -> str:
        lines = []
        if p.current:
            lines.append(f"Now: **{p.current.title}**")
        pending = list(p.queue._queue)  # display only
        for i, t in enumerate(pending, 1):
            lines.append(f"{i}. {t.title}")
            if i >= 15:
                lines.append(f"...and {len(pending) - 15} more")
                break
        return "\n".join(lines) if lines else "Queue is empty."

    def _panel_embed(self, p: GuildPlayer, paused: bool = False) -> discord.Embed:
        track = p.current
        embed = discord.Embed(
            title=track.title if track else "Nothing playing",
            url=track.webpage or None if track else None,
            color=discord.Color.blurple(),
        )
        if track:
            embed.set_author(name="Now playing")
            embed.add_field(name="Requested by", value=track.requester, inline=True)
            embed.add_field(name="Status", value="Paused" if paused else "Playing",
                            inline=True)
            pending = list(p.queue._queue)  # display only
            if pending:
                embed.add_field(name="Up next", value=pending[0].title[:1024], inline=False)
            embed.set_footer(text=f"{len(pending)} in queue - "
                                  "buttons work for anyone in the voice channel")
        return embed

    async def _panel_check(self, interaction: discord.Interaction) -> bool:
        """Panel gate: the presser must be in the bot's voice channel."""
        member = interaction.user
        vc = interaction.guild.voice_client if interaction.guild else None
        ok = (
            isinstance(member, discord.Member)
            and member.voice is not None
            and member.voice.channel is not None
            and vc is not None
            and member.voice.channel == vc.channel
        )
        if not ok:
            try:
                await interaction.response.send_message(
                    "Hop in the voice channel with the music first - "
                    "panel buttons only work in there.", ephemeral=True)
            except Exception:
                pass
        return ok

    async def _delete_panel(self, p: GuildPlayer) -> None:
        message, view = p.panel_message, p.panel_view
        p.panel_message, p.panel_view = None, None
        if view is not None:
            view.stop()
        if message is not None:
            try:
                await message.delete()
            except Exception:
                pass  # already deleted by hand, or perms - both fine

    async def _post_panel(self, p: GuildPlayer) -> None:
        """Fresh panel per track: old panel deleted, new one posted."""
        await self._delete_panel(p)
        if p.text_channel is None or p.current is None:
            return
        view = PlayerPanelView(self, p.text_channel.guild.id)
        try:
            message = await p.text_channel.send(embed=self._panel_embed(p), view=view)
        except Exception:
            log.exception("panel post failed")
            return
        p.panel_message, p.panel_view = message, view

    async def _refresh_panel(self, p: GuildPlayer, paused: bool) -> None:
        if p.panel_message is None:
            return
        if p.panel_view is not None:
            p.panel_view.set_paused(paused)
        try:
            await p.panel_message.edit(embed=self._panel_embed(p, paused=paused),
                                        view=p.panel_view)
        except Exception:
            pass  # panel deleted by hand - the next track replaces it

    async def _stop_player(self, guild: discord.Guild) -> None:
        """Shared stop path for the !stop command and the panel button."""
        p = self.player(guild.id)
        while not p.queue.empty():
            p.queue.get_nowait()
        await self._delete_panel(p)
        vc = guild.voice_client
        if vc:
            vc.stop()

    async def _run_queue(self, guild: discord.Guild):
        p = self.player(guild.id)
        try:
            while True:
                vc = guild.voice_client
                if vc is None or not vc.is_connected():
                    await self._delete_panel(p)
                    await self._announce(p, "I lost the voice connection - queue stopped. "
                                            "Run !play again when you're in a channel.")
                    return
                try:
                    track = await asyncio.wait_for(p.queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    # Idle 5 minutes with an empty queue - leave the channel.
                    await self._delete_panel(p)
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
                done = asyncio.Event()
                try:
                    vc.play(source, after=lambda _e: self.bot.loop.call_soon_threadsafe(done.set))
                except Exception as e:
                    log.exception("voice play failed")
                    await self._announce(p, f"Couldn't start **{track.title}**: {e}")
                    p.current = None
                    continue
                # Fresh Now Playing panel for this track (deletes the old one).
                await self._post_panel(p)
                await done.wait()
                p.current = None
        except Exception:
            log.exception("queue runner died")
            await self._announce(p, "The music player hit an error and stopped - "
                                    "the details are in the bot log. Run !play to restart it.")
        finally:
            await self._delete_panel(p)
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
            await self._refresh_panel(self.player(ctx.guild.id), paused=True)
            await ctx.send("Paused.")

    @commands.command(name="resume")
    async def resume(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await self._refresh_panel(self.player(ctx.guild.id), paused=False)
            await ctx.send("Resumed.")

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context):
        """Stop playback and clear the queue (stays in the channel)."""
        await self._stop_player(ctx.guild)
        await ctx.send("Stopped, queue cleared.")

    @commands.command(name="leave")
    async def leave(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        if vc:
            await self._delete_panel(self.player(ctx.guild.id))
            await vc.disconnect()
            await ctx.send("Left the voice channel.")

    @commands.command(name="queue")
    async def show_queue(self, ctx: commands.Context):
        p = self.player(ctx.guild.id)
        await ctx.send(self._queue_text(p))

    @commands.command(name="np")
    async def now_playing(self, ctx: commands.Context):
        p = self.player(ctx.guild.id)
        if p.current:
            await ctx.send(f"Now playing: **{p.current.title}**\n{p.current.webpage}")
        else:
            await ctx.send("Nothing is playing.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
