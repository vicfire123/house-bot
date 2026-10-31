"""Starboard module: star a message, it lands in #starboard.

Spec (locked 2026-10-31, Victor's calls):
  - Threshold is ONE ⭐. A single star posts the message.
  - Self-stars count - the author can star their own message in.
  - Bot messages are excluded entirely.
  - Channel is #starboard (looked up by name; created if missing and
    the bot has Manage Channels).
  - Removing the last star deletes the starboard post; extra stars
    just update the count on the existing post, never a duplicate.
  - #vicbot-logs (the audit channel) is never a source.
  - Reactions inside #starboard itself are ignored (no loops).

The message-id -> starboard-post-id map is kept in a JSON file so a
container restart does not duplicate or orphan posts. In Docker the
file lives on the bot-data volume (/data/starboard.json, see
docker-compose.yml); locally it falls back to ./starboard-data.json.
"""

import asyncio
import json
import logging
import os

import discord
from discord.ext import commands

log = logging.getLogger("housebot.starboard")

STAR_EMOJI = "⭐"
STARBOARD_NAME = "starboard"

AUDIT_CHANNEL_ID_RAW = os.environ.get("AUDIT_CHANNEL_ID", "").strip()
AUDIT_CHANNEL_ID = int(AUDIT_CHANNEL_ID_RAW) if AUDIT_CHANNEL_ID_RAW.isdigit() else None


def _data_path() -> str:
    raw = os.environ.get("STARBOARD_DATA_FILE", "").strip()
    if raw:
        return raw
    if os.path.isdir("/data"):
        return "/data/starboard.json"
    return os.path.join(os.getcwd(), "starboard-data.json")


class Starboard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.path = _data_path()
        self.map: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.map = {str(k): int(v) for k, v in data.items()}
            log.info("starboard map loaded: %d entries from %s", len(self.map), self.path)
        except FileNotFoundError:
            log.info("starboard map: no file yet at %s - starting empty", self.path)
        except Exception:
            log.exception("starboard map load failed - starting empty")

    def _save(self) -> None:
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.map, f)
            os.replace(tmp, self.path)
        except Exception:
            log.exception("starboard map save failed")

    # ---------- helpers ----------

    async def _board_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel = discord.utils.get(guild.text_channels, name=STARBOARD_NAME)
        if channel is not None:
            return channel
        try:
            channel = await guild.create_text_channel(
                STARBOARD_NAME,
                topic="Messages the server starred - 1 star posts here.",
                reason="Starboard channel for VicBot",
            )
            log.info("created #%s in guild %s", STARBOARD_NAME, guild.id)
            return channel
        except Exception:
            log.exception("could not find or create #%s in guild %s",
                          STARBOARD_NAME, guild.id)
            return None

    @staticmethod
    def _star_count(message: discord.Message) -> int:
        for reaction in message.reactions:
            if str(reaction.emoji) == STAR_EMOJI:
                return reaction.count
        return 0

    @staticmethod
    def _build_post(message: discord.Message, count: int):
        embed = discord.Embed(
            description=message.content or None,
            timestamp=message.created_at,
            color=discord.Color.gold(),
        )
        embed.set_author(
            name=message.author.display_name,
            icon_url=message.author.display_avatar.url,
        )
        embed.add_field(name="Source", value=f"[Jump to message]({message.jump_url})",
                        inline=False)
        image_url = None
        extra_files = []
        for att in message.attachments:
            if image_url is None and (att.content_type or "").startswith("image/"):
                image_url = att.url
            else:
                extra_files.append(att)
        if image_url:
            embed.set_image(url=image_url)
        if extra_files:
            embed.add_field(
                name="Attachments",
                value="\n".join(f"[{a.filename}]({a.url})" for a in extra_files)[:1024],
                inline=False,
            )
        if not message.content and not message.attachments:
            embed.description = "*no text - probably an embed, sticker, or a vibe*"
        for extra in message.embeds:
            if extra.type == "rich" and extra.title:
                embed.add_field(name="Embed", value=extra.title[:1024], inline=False)
                break
        content = f"{STAR_EMOJI} **{count}** | {message.channel.mention}"
        return content, embed

    async def _sync(self, guild: discord.Guild, channel_id: int, message_id: int) -> None:
        """Make the starboard match the current star count of one message."""
        async with self._lock:
            board = await self._board_channel(guild)
            if board is None:
                return
            if channel_id == board.id:
                return
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                return
            try:
                message = await channel.fetch_message(message_id)
            except Exception:
                return
            if message.author.bot:
                return
            count = self._star_count(message)
            key = str(message_id)
            post_id = self.map.get(key)

            if count <= 0:
                if post_id is not None:
                    try:
                        post = await board.fetch_message(post_id)
                        await post.delete()
                    except Exception:
                        pass
                    del self.map[key]
                    self._save()
                return

            content, embed = self._build_post(message, count)
            if post_id is not None:
                try:
                    post = await board.fetch_message(post_id)
                    await post.edit(content=content, embed=embed)
                    return
                except discord.NotFound:
                    del self.map[key]  # post was deleted by hand - repost below
                except Exception:
                    log.exception("starboard post edit failed")
                    return
            try:
                post = await board.send(content=content, embed=embed)
            except Exception:
                log.exception("starboard post send failed")
                return
            self.map[key] = post.id
            self._save()

    # ---------- events ----------

    def _is_star(self, payload: discord.RawReactionActionEvent) -> bool:
        return str(payload.emoji) == STAR_EMOJI

    def _ignored(self, payload: discord.RawReactionActionEvent) -> bool:
        if payload.guild_id is None:
            return True
        if AUDIT_CHANNEL_ID is not None and payload.channel_id == AUDIT_CHANNEL_ID:
            return True
        return False

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not self._is_star(payload) or self._ignored(payload):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is not None:
            await self._sync(guild, payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if not self._is_star(payload) or self._ignored(payload):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is not None:
            await self._sync(guild, payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """If the original (or the starboard post) dies, clean the map."""
        key = str(payload.message_id)
        if key not in self.map:
            return
        async with self._lock:
            post_id = self.map.pop(key, None)
            self._save()
        if post_id is None or payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        board = discord.utils.get(guild.text_channels, name=STARBOARD_NAME)
        if board is not None:
            try:
                post = await board.fetch_message(post_id)
                await post.delete()
            except Exception:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Starboard(bot))
