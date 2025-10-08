# NewsBot

An extensible Discord bot that polls JSON feeds (including Ransomware.live) and posts clean, formatted embeds.  
Supports per-feed intervals, duplicate-safe watermarking, and moderator-only management commands.

<p align="center">
  <!-- Optional banner -->
  <img src="hurdurr.png" alt="NewsBot – Headlines Ticker" width="350">
</p>

## Features

- Add/remove/list feeds per server (guild) with commands.
- Per-feed polling interval (minutes).
- Duplicate-safe posting via time-based watermarks.
- Smart embed builder (templates for Ransomware.live **victims** and **cyberattacks**; generic JSON fallback).
- Optional cap on posts per poll (default `MAX_POSTS_PER_POLL = 10`).
- Friendly “next poll” timer per feed.

## Prerequisites

- Linux server (Debian/Ubuntu or similar)
- Python **3.9+**
- A Discord bot with **Message Content Intent** enabled
- Channel(s) where the bot will post

## Installation

1. **Install system deps**
   ```bash
   sudo apt update
   sudo apt install -y python3 python3-venv python3-pip git
   ```

2. **Clone**
   ```bash
   git clone https://github.com/hitem/NewsBot.git
   cd NewsBot
   ```

3. **Create venv**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
  ```

4. **Install Python deps**
   ```bash
   pip install -r requirements.txt
   ```

## Configuration

1. **Environment file**
   ```bash
   cp .env_example .env
   ```
   Edit `.env`:
   ```ini
   DISCORD_BOT_TOKEN=your_discord_bot_token_here

   # Optional (used for Ransomware.live PRO victims endpoint)
   RWL_API_KEY=your_rwl_api_key_if_you_have_one
   RWL_COUNTRY=SE
   ```

2. **Developer Mode → Channel ID (optional)**
   - Discord → ⚙️ **User Settings** → **Advanced** → enable **Developer Mode**  
   - Right-click a channel → **Copy ID** (you’ll only need this if you seed `feeds.json` manually)

3. **Seed state files**
   If starting from scratch:
   ```bash
   echo "[]" > feeds.json
   echo "{}" > state.json
   ```
   > You can add the first feed via command (recommended), so `feeds.json` can stay empty initially.

---

## Running

With your virtual environment active:
```bash
source venv/bin/activate
python NewsBotwoman.py
```

You should see a startup banner and periodic polling logs. New items will be posted into the configured channel(s).

## Run as a systemd Service (optional)

1. **Create service file**
   ```bash
   sudo nano /etc/systemd/system/newsbot.service
   ```

2. **Paste & edit paths/user**
   ```ini
   [Unit]
   Description=Discord NewsBot Service
   After=network.target

   [Service]
   Type=simple
   User=your_username
   WorkingDirectory=/path/to/NewsBot
   EnvironmentFile=/path/to/NewsBot/.env
   ExecStart=/path/to/NewsBot/venv/bin/python NewsBotwoman.py
   Restart=on-failure
   StandardOutput=journal
   StandardError=journal

   [Install]
   WantedBy=multi-user.target
   ```

3. **Enable + start**
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable newsbot
   sudo systemctl start newsbot
   sudo systemctl status newsbot
   ```

## Commands (Moderator-only)

> Your moderator roles are configured in code:  
> `MODERATOR_ROLES = {"Admins", "Super Friends"}`

- `!newsaddfeed <name?> <url> [interval]` — Add a feed to the **current channel**  
  Examples:
  ```
  !newsaddfeed https://api.ransomware.live/v2/countryvictims/SE
  !newsaddfeed "RWL Cyberattacks (SE)" https://api.ransomware.live/v2/countrycyberattacks/SE 10
  !newsaddfeed "My JSON" https://example.com/feed.json 15
  ```
  Special handling:
  - `api-pro.ransomware.live/victims/search` → **RWL Victims PRO** template (needs `RWL_API_KEY`)
  - `.../countryvictims/<CC>` → **RWL victims (free)**
  - `.../countrycyberattacks/<CC>` → **RWL cyberattacks (free)**
  - Otherwise → generic JSON template

- `!newsremovefeed <#>` — Remove a feed **by index** (see list). Also clears its watermark state.  
- `!newslistfeeds` — List all feeds in this server with indexes, channels, provider, interval.  
- `!newstest <#>` — Fetch & display the latest entry from that feed (doesn’t persist).  
- `!newstimer <#>` — Show time until next poll for that feed.  
- `!newssettings` — Show this help card.

## Providers & Templates

- **RWL Victims PRO** (`api-pro.ransomware.live/victims/search`)
  - Uses `RWL_API_KEY` if set.
  - Watermarking by `discovered` + a set of IDs at the same timestamp to avoid dupes.
  - Query string is canonicalized for stable keys.

- **RWL Victims Free** (`.../countryvictims/<CC>`) and **RWL Cyberattacks Free** (`.../countrycyberattacks/<CC>`)
  - Prebuilt embed templates.
  - Watermarking by `published` (or similar), with tie-break on titles.

- **Generic JSON**
  - Expects a list or `{ "items": [...] }`.
  - Tries common fields (`title`, `url`, `description`, `timestamp/added/date/published`).
  - `auto_fields: true` shows remaining keys compactly.


## State & Duplication Control

- `feeds.json` — list of configured feeds (name, url, channel_id, interval, provider, template).
- `state.json` — watermark per **(provider + url + channel)**:
  - PRO victims: `last_seen_disc` + `ids_seen_at_last_disc`
  - Others: `last_seen_pub` + `titles_seen_at_last_pub`
- First run seeds watermark to newest item (no backfill spam).
- Per-poll cap: `MAX_POSTS_PER_POLL = 10` in code (posts oldest→newest within the new slice).

## Logging

Tail the service logs:
```bash
sudo journalctl -u newsbot -f
```

## Permissions / Intents

In the Discord Developer Portal:

1. Enable **Message Content Intent**.  
2. Invite the bot with permissions:
   - View Channels
   - Read Message History
   - Send Messages
   - Embed Links
   - (Optional) Attach Files


## Tips

- Customize the `embed` template per feed (color, fields, footer) for a cleaner card.  
- Set `RWL_COUNTRY` in `.env` for PRO victims; defaults like `order=discovered&direction=desc&limit=100` are auto-filled if missing.  
- If a feed changes JSON shape, run `!newstest` to inspect and then tweak the template keys.

