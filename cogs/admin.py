"""Admin module: plain-English moderation + channel management,
behind an approval gate.

Safety model (deliberate, mirrors an approval-card workflow):
  1. Only user IDs in ADMIN_USER_IDS can talk to this module at all.
     Everyone else is ignored - silently, no error, no hints.
  2. Nothing destructive ever executes on the message alone. The bot
     replies with exactly what it understood and Confirm / Cancel
     buttons. Only the person who asked can confirm, and the offer
     expires in 60 seconds. Channel creates, deletes and permission
     changes go through the same card - server structure is not
     exempt.
  3. Hard refusals, no confirm can override them:
     - the server owner can never be a target
     - the bot can never target itself
     - a whitelisted admin can only be targeted by the server owner
     - roles at/above the bot's own top role can't be touched
       (Discord enforces this too; we check first and say so plainly)
     - the audit channel can't be deleted, renamed or re-permissioned
       (it is the paper trail - it protects itself)
  4. Every proposal, confirmation, cancellation and outcome is written
     to the audit channel + log.

Trigger: mention the bot ("@HouseBot kick Marcus") or start a message
with "!do ". Parsing is rule-based on purpose: predictable beats clever
when the output can ban somebody or delete a channel.
"""

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Awaitable, Callable, Optional

import discord
from discord.ext import commands

log = logging.getLogger("housebot.admin")

CONFIRM_TIMEOUT = 60  # seconds


@dataclass
class Action:
    """One parsed, not-yet-approved action."""
    description: str                      # plain English, shown on the card
    run: Callable[[], Awaitable[str]]     # does it, returns a result line
    destructive: bool = True
    kind: str = "member"                  # "member" or "channel"
    channel_id: Optional[int] = None      # target channel, for guard checks


# ---------------- lookups ----------------

def _find_member(guild: discord.Guild, text: str,
                 mentions: list[discord.Member]) -> Optional[discord.Member]:
    for m in mentions:
        return m
    low = text.lower()
    best = None
    for member in guild.members:
        for name in {member.display_name.lower(), member.name.lower()}:
            if name and name in low and len(name) >= 3:
                if best is None or len(name) > len(best[0]):
                    best = (name, member)
    return best[1] if best else None


def _find_role(guild: discord.Guild, text: str) -> Optional[discord.Role]:
    low = text.lower()
    roles = [r for r in guild.roles if r.name != "@everyone"]
    matched = [r for r in roles if r.name.lower() in low]
    if matched:
        return max(matched, key=lambda r: len(r.name))
    if "admin" in low:
        adminish = [r for r in roles if "admin" in r.name.lower()]
        if adminish:
            return max(adminish, key=lambda r: r.position)
    return None


def _find_voice_channel(guild: discord.Guild, text: str) -> Optional[discord.VoiceChannel]:
    low = text.lower()
    for ch in guild.voice_channels:
        if ch.name.lower() in low:
            return ch
    return None


def _find_channel(guild: discord.Guild, message: discord.Message):
    """A text/voice channel: explicit #mention first, else name match."""
    for ch in message.channel_mentions:
        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel)):
            return ch
    low = message.content.lower()
    best = None
    for ch in guild.channels:
        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel)):
            name = ch.name.lower()
            if name and name in low and (best is None or len(name) > len(best.name)):
                best = ch
    return best


def _find_category(guild: discord.Guild, text: str) -> Optional[discord.CategoryChannel]:
    low = text.lower()
    for cat in guild.categories:
        if cat.name.lower() in low:
            return cat
    return None


def _slug(name: str) -> str:
    """Discord text-channel names: lowercase, hyphens, no symbols."""
    s = re.sub(r"[^a-z0-9\- ]", "", name.lower()).strip()
    return (re.sub(r"\s+", "-", s) or "channel")[:100]


def _extract_named(text: str) -> Optional[str]:
    """Pull X out of '... called X', '... named X', optionally quoted."""
    m = re.search(r"(?:called|named)\s+[\"']?(.+?)[\"']?"
                  r"(?:\s+in\s+the\s+|\s+in\s+|\s+under\s+|\s+for\s+|$)",
                  text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _parse_duration(text: str) -> timedelta:
    m = re.search(r"(\d+)\s*(d|day|days|h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\b",
                  text.lower())
    if not m:
        return timedelta(minutes=10)
    n, unit = int(m.group(1)), m.group(2)
    if unit.startswith("d"):
        return timedelta(days=min(n, 28))
    if unit.startswith("h"):
        return timedelta(hours=n)
    return timedelta(minutes=n)


def _access_overwrite(channel, allow: bool) -> dict:
    """Permission kwargs for set_permissions, shaped by channel type."""
    if isinstance(channel, discord.VoiceChannel):
        return {"view_channel": allow, "connect": allow}
    return {"view_channel": allow, "send_messages": allow}


# ---------------- confirmation card ----------------

class ConfirmView(discord.ui.View):
    """The approval card. Only the requester can press, once."""

    def __init__(self, requester_id: int, action: Action,
                 on_outcome: Callable[[str], Awaitable[None]]):
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self.requester_id = requester_id
        self.action = action
        self.on_outcome = on_outcome
        self.decided = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who asked can confirm this.", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, text: str):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=text, view=self)
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if self.decided:
            return
        self.decided = True
        try:
            result = await self.action.run()
            await self._finish(interaction, f"Done. {result}")
            await self.on_outcome(f"CONFIRMED + DONE: {self.action.description} -> {result}")
        except discord.Forbidden:
            await self._finish(
                interaction,
                "Discord refused that - my role probably sits below the target's top role, "
                "or I'm missing a permission (channel work needs Manage Channels). "
                "Fix the role and ask again.")
            await self.on_outcome(f"FAILED (forbidden): {self.action.description}")
        except Exception as e:
            await self._finish(interaction, f"That failed: {e}")
            await self.on_outcome(f"FAILED: {self.action.description} -> {e}")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if self.decided:
            return
        self.decided = True
        await self._finish(interaction, f"Cancelled: {self.action.description}")
        await self.on_outcome(f"CANCELLED: {self.action.description}")

    async def on_timeout(self):
        if not self.decided:
            await self.on_outcome(f"EXPIRED (no confirmation): {self.action.description}")


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        from bot import ADMIN_USER_IDS, AUDIT_CHANNEL_ID, audit  # cogs load after core
        self.admin_ids = ADMIN_USER_IDS
        self.audit_channel_id = AUDIT_CHANNEL_ID
        self.audit = audit

    # ---------------- channel actions ----------------

    def parse_channel(self, message: discord.Message) -> Optional[Action]:
        guild = message.guild
        text = message.content
        low = text.lower()
        everyone = guild.default_role
        reason = f"Requested by {message.author}"

        # ---- create a category ----
        if re.search(r"\b(create|make|add)\b", low) and "categor" in low:
            name = _extract_named(text)
            if name:
                async def run(name=name):
                    cat = await guild.create_category(name, reason=reason)
                    return f"Created category **{cat.name}**."
                return Action(f"Create category **{name}**", run, kind="channel")
            return None

        # ---- create a channel ----
        if re.search(r"\b(create|make|add)\b", low) and "channel" in low:
            name = _extract_named(text)
            if not name:
                return None
            is_voice = "voice" in low or re.search(r"\bvc\b", low)
            category = _find_category(guild, text)
            private = "private" in low or "admin" in low
            guests = [m for m in message.mentions if m.id != self.bot.user.id]
            admin_role = None
            if "admin" in low:
                admin_role = _find_role(guild, text)

            async def run(name=name, is_voice=is_voice, category=category,
                          private=private, guests=guests, admin_role=admin_role):
                overwrites = None
                notes = ""
                if private:
                    overwrites = {
                        everyone: discord.PermissionOverwrite(view_channel=False),
                        guild.me: discord.PermissionOverwrite(view_channel=True),
                        message.author: discord.PermissionOverwrite(view_channel=True),
                    }
                    for g in guests:
                        overwrites[g] = discord.PermissionOverwrite(view_channel=True)
                    if admin_role is not None:
                        overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True)
                    notes = " (private: hidden from @everyone)"
                # discord.py 2.7 uses a MISSING sentinel for these
                # parameters - passing overwrites=None explicitly is an
                # error ("expects a dict"), so only pass what's set.
                kwargs = {"reason": reason}
                if category is not None:
                    kwargs["category"] = category
                if overwrites is not None:
                    kwargs["overwrites"] = overwrites
                if is_voice:
                    ch = await guild.create_voice_channel(name, **kwargs)
                    return f"Created voice channel **{ch.name}**{notes}."
                ch = await guild.create_text_channel(_slug(name), **kwargs)
                return f"Created #{ch.name}{notes}."
            kind_word = "voice channel" if is_voice else "text channel"
            desc = f"Create {kind_word} **{name}**"
            if category:
                desc += f" in category **{category.name}**"
            if private:
                desc += " - PRIVATE (hidden from @everyone"
                if guests:
                    desc += ", visible to " + ", ".join(g.display_name for g in guests)
                desc += ")"
            return Action(desc, run, kind="channel")

        ch = _find_channel(guild, message)
        if ch is None:
            return None

        member = _find_member(guild, text,
                              [m for m in message.mentions if m.id != self.bot.user.id])
        role = _find_role(guild, text)

        # ---- grant access to a channel ----
        if re.search(r"\b(give|grant|add|let)\b", low) and re.search(r"\b(access|into|in|to)\b", low):
            who = member or role
            if who is not None:
                async def run(ch=ch, who=who):
                    await ch.set_permissions(who, reason=reason, **_access_overwrite(ch, True))
                    return f"{who.display_name if isinstance(who, discord.Member) else who.name} can now see #{ch.name}."
                label = who.display_name if isinstance(who, discord.Member) else who.name
                return Action(f"Give **{label}** access to **#{ch.name}**", run,
                              kind="channel", channel_id=ch.id)

        # ---- revoke access (before delete: "remove Marcus from #x" is this) ----
        if re.search(r"\b(remove|revoke|kick|take)\b", low) and re.search(r"\bfrom\b", low):
            who = member or role
            if who is not None:
                async def run(ch=ch, who=who):
                    await ch.set_permissions(who, reason=reason, **_access_overwrite(ch, False))
                    return f"{who.display_name if isinstance(who, discord.Member) else who.name} can no longer see #{ch.name}."
                label = who.display_name if isinstance(who, discord.Member) else who.name
                return Action(f"Remove **{label}**'s access to **#{ch.name}**", run,
                              kind="channel", channel_id=ch.id)

        # ---- lock / read-only (everyone can look, nobody can post) ----
        if re.search(r"\block\b", low) or "read-only" in low or "read only" in low \
                or "announcement" in low:
            async def run(ch=ch):
                if isinstance(ch, discord.VoiceChannel):
                    await ch.set_permissions(everyone, connect=False, reason=reason)
                    return f"#{ch.name} locked - @everyone can't connect."
                await ch.set_permissions(everyone, send_messages=False, reason=reason)
                return f"#{ch.name} is now read-only for @everyone."
            return Action(f"Lock **#{ch.name}** (@everyone can't post/connect)", run,
                          kind="channel", channel_id=ch.id)

        # ---- unlock ----
        if re.search(r"\bunlock\b", low):
            async def run(ch=ch):
                if isinstance(ch, discord.VoiceChannel):
                    await ch.set_permissions(everyone, connect=None, reason=reason)
                else:
                    await ch.set_permissions(everyone, send_messages=None, reason=reason)
                return f"#{ch.name} unlocked."
            return Action(f"Unlock **#{ch.name}**", run, kind="channel", channel_id=ch.id)

        # ---- make private / make public ----
        if "private" in low or re.search(r"\bhide\b", low):
            async def run(ch=ch):
                await ch.set_permissions(everyone, view_channel=False, reason=reason)
                return f"#{ch.name} is now private - hidden from @everyone."
            return Action(f"Make **#{ch.name}** private (hidden from @everyone)", run,
                          kind="channel", channel_id=ch.id)
        if "public" in low:
            async def run(ch=ch):
                await ch.set_permissions(everyone, view_channel=None, reason=reason)
                return f"#{ch.name} is public again (inherits category visibility)."
            return Action(f"Make **#{ch.name}** public", run, kind="channel", channel_id=ch.id)

        # ---- rename ----
        if re.search(r"\brename\b", low):
            m = re.search(r"\brename\b.*?\bto\s+[\"']?([^\"']+?)[\"']?\s*$", text, re.IGNORECASE)
            if m:
                new_name = m.group(1).strip()
                display = new_name if isinstance(ch, discord.VoiceChannel) else _slug(new_name)

                async def run(ch=ch, display=display):
                    await ch.edit(name=display, reason=reason)
                    return f"Channel renamed to #{display}."
                return Action(f"Rename **#{ch.name}** to **#{display}**", run,
                              kind="channel", channel_id=ch.id)

        # ---- topic ----
        if "topic" in low and isinstance(ch, discord.TextChannel):
            m = re.search(r"\btopic\b.*?\bto\s+[\"']?(.+?)[\"']?\s*$", text, re.IGNORECASE)
            if m:
                topic = m.group(1).strip()

                async def run(ch=ch, topic=topic):
                    await ch.edit(topic=topic, reason=reason)
                    return f"#{ch.name} topic set."
                return Action(f"Set **#{ch.name}** topic to \"{topic[:80]}\"", run,
                              kind="channel", channel_id=ch.id)

        # ---- slowmode ----
        if "slowmode" in low and isinstance(ch, discord.TextChannel):
            m = re.search(r"(\d+)", text)
            secs = min(int(m.group(1)), 21600) if m else 0

            async def run(ch=ch, secs=secs):
                await ch.edit(slowmode_delay=secs, reason=reason)
                return f"#{ch.name} slowmode set to {secs}s."
            return Action(f"Set **#{ch.name}** slowmode to **{secs}s**", run,
                          kind="channel", channel_id=ch.id)

        # ---- delete channel (after revoke: its phrasing overlaps) ----
        if re.search(r"\b(delete|remove|nuke)\b", low):
            async def run(ch=ch):
                name = ch.name
                await ch.delete(reason=reason)
                return f"#{name} deleted, along with its message history."
            return Action(
                f"**DELETE channel #{ch.name}** - its entire message history goes with it",
                run, kind="channel", channel_id=ch.id)

        return None

    # ---------------- member actions ----------------

    def parse(self, message: discord.Message) -> Optional[Action]:
        # Channel work first: its phrasings ("remove X from #chan",
        # "give X access") would otherwise trip the member patterns.
        channel_action = self.parse_channel(message)
        if channel_action is not None:
            return channel_action

        guild = message.guild
        text = message.content
        low = text.lower()
        mentions = [m for m in message.mentions if m.id != self.bot.user.id]
        member = _find_member(guild, text, mentions)

        # --- voice disconnect (before server kick: "kick from voice") ---
        if re.search(r"\b(disconnect|dc)\b", low) or ("kick" in low and ("voice" in low or " vc" in low or " call" in low)):
            if member is None:
                return None
            m = member

            async def run(m=m):
                if m.voice is None or m.voice.channel is None:
                    return f"{m.display_name} isn't in a voice channel."
                await m.move_to(None, reason=f"Requested by {message.author}")
                return f"{m.display_name} disconnected from voice."
            return Action(f"Disconnect **{m.display_name}** from voice", run)

        # --- move to a voice channel ---
        if "move" in low:
            ch = _find_voice_channel(guild, text)
            if member and ch:
                m = member

                async def run(m=m, ch=ch):
                    await m.move_to(ch, reason=f"Requested by {message.author}")
                    return f"{m.display_name} moved to {ch.name}."
                return Action(f"Move **{m.display_name}** to **{ch.name}**", run)

        # --- ban ---
        if re.search(r"\bban\b", low):
            if member is None:
                return None
            m = member

            async def run(m=m):
                await m.ban(reason=f"Requested by {message.author}", delete_message_seconds=0)
                return f"{m.display_name} banned."
            return Action(f"**BAN {m.display_name}** from the server", run)

        # --- kick ---
        if re.search(r"\bkick\b", low):
            if member is None:
                return None
            m = member

            async def run(m=m):
                await m.kick(reason=f"Requested by {message.author}")
                return f"{m.display_name} kicked (can rejoin with an invite)."
            return Action(f"**KICK {m.display_name}** from the server", run)

        # --- remove timeout / unmute (before timeout) ---
        if re.search(r"\b(untimeout|unmute|remove (the )?timeout|lift (the )?timeout)\b", low):
            if member is None:
                return None
            m = member

            async def run(m=m):
                await m.timeout(None, reason=f"Requested by {message.author}")
                return f"Timeout lifted for {m.display_name}."
            return Action(f"Lift the timeout on **{m.display_name}**", run)

        # --- timeout / mute ---
        if re.search(r"\b(timeout|mute)\b", low):
            if member is None:
                return None
            m = member
            dur = _parse_duration(text)

            async def run(m=m, dur=dur):
                await m.timeout(dur, reason=f"Requested by {message.author}")
                return f"{m.display_name} timed out for {dur}."
            return Action(f"Timeout **{m.display_name}** for {dur}", run)

        # --- remove a role (before give: "remove" is unambiguous) ---
        if re.search(r"\b(remove|take|strip|revoke)\b.*\b(role|rights|admin)\b", low) or "demote" in low:
            role = _find_role(guild, text)
            if member and role:
                m, r = member, role

                async def run(m=m, r=r):
                    await m.remove_roles(r, reason=f"Requested by {message.author}")
                    return f"Removed {r.name} from {m.display_name}."
                return Action(f"Remove role **{r.name}** from **{m.display_name}**", run)

        # --- give a role / make admin / promote ---
        if re.search(r"\b(give|grant|add|make|promote|assign)\b", low):
            role = _find_role(guild, text)
            if member and role:
                m, r = member, role

                async def run(m=m, r=r):
                    await m.add_roles(r, reason=f"Requested by {message.author}")
                    return f"{m.display_name} now has {r.name}."
                return Action(f"Give role **{r.name}** to **{m.display_name}**", run)

        # --- purge messages in this channel ---
        if re.search(r"\b(purge|clear|delete)\b", low) and re.search(r"\b(message|messages|chat|last)\b", low):
            n = re.search(r"(\d+)", text)
            count = min(int(n.group(1)), 200) if n else 10
            channel = message.channel

            async def run(channel=channel, count=count):
                deleted = await channel.purge(limit=count)
                return f"Deleted {len(deleted)} messages in #{channel.name}."
            return Action(f"Delete the last **{count} messages** in #{channel.name}", run)

        return None

    # ---------------- guards ----------------

    def guard(self, message: discord.Message, action: Action) -> Optional[str]:
        """Returns a refusal line, or None if the proposal may proceed."""
        # Channel actions: the audit channel protects itself; the rest
        # is bounded by the bot's role permissions, same as members.
        if action.kind == "channel":
            if action.channel_id is not None \
                    and self.audit_channel_id is not None \
                    and action.channel_id == self.audit_channel_id:
                return ("Refused: that's the audit log channel. It doesn't get "
                        "deleted, renamed or re-permissioned - it's the paper trail.")
            return None

        guild = message.guild
        target_names = [m for m in message.mentions if m.id != self.bot.user.id]
        member = _find_member(guild, message.content, target_names)

        if member is not None:
            if member.id == guild.owner_id:
                return "Refused: that's the server owner. I never act on the owner."
            if member.id == self.bot.user.id:
                return "Refused: I won't act on myself."
            if member.id in self.admin_ids and message.author.id != guild.owner_id:
                return ("Refused: that person is a whitelisted admin. "
                        "Only the server owner can aim an action at another admin.")
            bot_member = guild.get_member(self.bot.user.id)
            if bot_member and member.top_role >= bot_member.top_role and member.id != message.author.id:
                return (f"Refused: {member.display_name}'s top role is at or above mine, "
                        "so Discord would block it anyway. Drag my role higher first.")
        return None

    # ---------------- listener ----------------

    def _triggered(self, message: discord.Message) -> bool:
        """Mention detection, belt and braces: user object, raw mention
        strings (both formats), and the bot's managed role - Discord's
        autocomplete can insert any of them for "@VicBot"."""
        if message.content.startswith("!do "):
            return True
        bot_id = self.bot.user.id
        if any(m.id == bot_id for m in message.mentions):
            return True
        if f"<@{bot_id}>" in message.content or f"<@!{bot_id}>" in message.content:
            return True
        for role in message.role_mentions:
            tags = getattr(role, "tags", None)
            if tags is not None and getattr(tags, "bot_id", None) == bot_id:
                return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if not self.admin_ids:
            return  # admin module disabled until the whitelist is set
        if message.author.id not in self.admin_ids:
            return  # not whitelisted: total silence

        if not self._triggered(message):
            return

        action = self.parse(message)
        if action is None:
            await message.reply(
                "Didn't catch that. I understand things like:\n"
                "- `kick Marcus` / `ban Marcus`\n"
                "- `timeout Marcus for 10 minutes` / `lift the timeout on Marcus`\n"
                "- `disconnect Marcus` / `move Marcus to Lounge`\n"
                "- `give admin rights to Marcus` / `remove the Mod role from Marcus`\n"
                "- `purge the last 20 messages`\n"
                "- `create a text channel called clips` / `create a voice channel called Movie Night`\n"
                "- `create a private channel called planning for me and @Marcus`\n"
                "- `make #general read-only` / `lock #memes` / `unlock #memes`\n"
                "- `give Marcus access to #planning` / `remove Marcus from #planning`\n"
                "- `rename #clips to highlights` / `delete #old-channel`\n"
                "Tip: @-mention the person, #-mention the channel - matching is the fiddly part.")
            return

        refusal = self.guard(message, action)
        if refusal:
            await message.reply(refusal)
            await self.audit(message.guild,
                             f"REFUSED for {message.author}: {action.description} ({refusal})")
            return

        async def outcome(line: str):
            await self.audit(message.guild, f"[{message.author}] {line}")

        await self.audit(message.guild,
                         f"PROPOSED by {message.author}: {action.description}")
        view = ConfirmView(message.author.id, action, outcome)
        await message.reply(
            f"Here's what I understood:\n**{action.description}**\n"
            "Nothing happens unless you confirm.",
            view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
