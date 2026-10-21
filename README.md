# House Bot - setup

One private bot for the friend server: music, prompt-it admin (with an
approval gate), and Minecraft status. Runs in Docker on potofgreed.

## Part 1 - Discord Developer Portal (Victor, ~5 minutes)

1. Go to https://discord.com/developers/applications - New Application -
   name it (e.g. "HouseBot") - Create.
2. Left sidebar: **Bot**. Click **Reset Token** - copy the token. This is
   the DISCORD_TOKEN for the .env below. Never paste it in chat.
3. Same Bot page, scroll to **Privileged Gateway Intents** - turn ON:
   - Server Members Intent
   - Message Content Intent
4. Left sidebar: **OAuth2 > URL Generator**.
   - Scopes: check `bot`
   - Bot Permissions: check these ONLY (not Administrator):
     View Channel, Send Messages, Read Message History, Manage Messages,
     Manage Channels, Kick Members, Ban Members, Moderate Members,
     Manage Roles, Move Members, Connect, Speak
   - Copy the generated URL, open it, pick your server, Authorize.
5. In Discord: Settings > Advanced > **Developer Mode ON**. Right-click
   your own avatar > **Copy User ID** - that's your admin whitelist ID.
   Right-click the channel you want audit logs in > Copy Channel ID.

## Part 2 - potofgreed (one time)

```
mkdir -p ~/house-bot && cd ~/house-bot
# unzip this project here
cp .env.example .env
nano .env        # paste token, your user ID, audit channel ID
docker compose up -d --build
docker compose logs -f   # look for "Logged in as ..."
```

## Part 3 - role hierarchy (important, 30 seconds)

Server Settings > Roles. The bot's role (named after the bot) must sit
**above** every role it hands out or acts on - drag it up near the top,
under only the owner/your own admin role. If it's below a role, Discord
blocks the action and the bot will tell you exactly that.

## Using it

Music (anyone):
- `!play <song or YouTube/SoundCloud URL>` - queue a track
- **Spotify links work too**: no bot can stream Spotify audio (DRM) -
  the bot reads the track names and plays the YouTube matches, which is
  what every "Spotify bot" does. Single track links need zero setup.
  Playlist/album links need free Spotify API creds: go to
  developer.spotify.com/dashboard, create an app (name it anything,
  no redirect URI needed for this), copy Client ID + Client Secret into
  the .env as SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET, restart the bot.
  Playlists queue up to 100 tracks, resolved one by one as they play.
- `!queue` `!np` `!skip` `!pause` `!resume` `!stop` `!leave`
- Idle 5 minutes = it leaves the channel on its own.

Admin (whitelisted IDs only, silent to everyone else):
Mention the bot + plain English, or start with `!do `:
- `@HouseBot give admin rights to Marcus`
- `@HouseBot kick Marcus` / `ban Marcus`
- `@HouseBot timeout Marcus for 10 minutes` / `lift the timeout on Marcus`
- `@HouseBot disconnect Marcus` / `move Marcus to Lounge`
- `@HouseBot purge the last 20 messages`

 NOTHING destructive runs off the message alone. The bot posts what it
understood with **Confirm / Cancel** buttons. Only the person who asked
can confirm, and the offer dies in 60 seconds. Hard refusals (no confirm
can override): the server owner, the bot itself, another whitelisted
admin (unless the owner asks). Every proposal and outcome is logged to
the audit channel.

Minecraft (anyone): `!mc` - up/down, player count, who's online.

Channels (whitelisted admins, same Confirm card as everything else):
- `@HouseBot create a text channel called clips`
- `@HouseBot create a voice channel called Movie Night`
- `@HouseBot create a category called Games`
- `@HouseBot create a private channel called planning for me and @Marcus`
  (private = hidden from @everyone; named people + you + the bot can see it;
  say "admin" in the request and the Admin role can see it too)
- `@HouseBot make #general read-only` / `lock #memes` / `unlock #memes`
- `@HouseBot make #planning private` / `make #planning public`
- `@HouseBot give Marcus access to #planning` / `remove Marcus from #planning`
- `@HouseBot rename #clips to highlights`
- `@HouseBot set the topic of #general to Movie night Fridays`
- `@HouseBot set slowmode on #chat to 5`
- `@HouseBot delete #old-channel` (history goes with it - the card says so)

The bot's role needs the **Manage Channels** permission for channel work.
The audit channel is protected: the bot refuses to delete, rename or
re-permission it.

## When YouTube breaks the music (it will, every month or two)

```
cd ~/house-bot
docker compose exec house-bot pip install -U yt-dlp
docker compose restart house-bot
```
That's the whole maintenance routine - the reason private bots outlive
public ones.

## Voice won't connect (logs show "WebSocket closed with 4006")

That's Discord's DAVE voice encryption rejecting an outdated library.
requirements.txt pins discord.py >= 2.5 plus the `davey` package for
exactly this reason - a full rebuild (`docker compose up -d --build`)
is the fix. Check the startup log line "discord.py version:" to prove
what's running.

Watch (anyone):
- `!status` - live health of the Minecraft server, dashboard, Jellyfin,
  and Minecraft backup freshness
- Automatic alerts in the audit/log channel: a service has to fail two
  checks in a row (checks run every 2 minutes) before a DOWN alert posts,
  and one BACK UP message posts when it recovers. Silent while healthy.
- HTTP checks are configurable in .env via WATCH_URLS (Name=URL pairs).
  Backup freshness needs the read-only backups mount in docker-compose.yml.
