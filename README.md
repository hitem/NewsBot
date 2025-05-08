# NewsBot README

This guide walks you through installing, configuring, and running your NewsBot—an extensible Discord bot for polling various news APIs and posting formatted embeds.

---

## Prerequisites

- **Ubuntu/Debian** (or any Linux) server with root or sudo access  
- **Python 3.9+** and **pip**  
- A Discord bot token with **Message Content Intent** enabled  
- A Discord channel ID where the bot will post updates  

---

## Installation

1. **Update packages and install Python**

   ```bash
   sudo apt update
   sudo apt install python3 python3-pip python3-venv git
   ```

2. **Clone the repository**

   ```bash
   git clone https://github.com/hitem/NewsBot.git
   cd NewsBot
   ```

3. **Create and activate a virtual environment**

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

4. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

---

## Configuration

1. **Create a `.env` file** in the project root by copying the example:

   ```bash
   cp .env_example .env
   ```

2. **Edit `.env`** and set your values:

   ```ini
   DISCORD_BOT_TOKEN=your_discord_bot_token_here
   ```

3. Enable Developer Mode & Copy Channel ID

   - In Discord click **User Settings** (⚙️) → **Advanced** → **Developer Mode** → **Enable**.  
   - Navigate to the channel where you want posts, right‑click its name in the channel list, and select **Copy ID**.  
   - Paste that numeric ID when configuring `feeds.json`.

4. **Seed `feeds.json`** with at least one feed (only 1 first feed is needed, else will be added dynamicly). Example:

   ```json
   [
   {
     "name": "Ransomware victims (SE)",
     "url": "https://api.ransomware.live/v2/countryvictims/SE",
     "poll_interval_minutes": 10,
     "channel_id": 123456789012345678,    // ← SET YOUR CHANNEL ID
     "embed_color": 16711680,             // 0xFF0000 <- SET YOUR COLOR
     "embed": {
       "title":       "{post_title}",
       "url":         "{post_url}",
       "description": "{description}",
       "timestamp":   "{published}",
       "fields": [
         {
           "name":   "⚔️ Group",
           "value":  "{group_name}",
           "inline": true
         },
         {
           "name":   "🌐 Website",
           "value":  "{website}",
           "inline": true
         },
         {
           "name":   "🕵️ Discovered",
           "value":  "{discovered}",
           "inline": true
         }
       ],
       "auto_fields": false,
       "footer_text": "{published}"
     }
   }
   ]

   ```
   Its good practice to make your own style to make the card posted to teams look better ofc. Else the bot will use a dynamic one that just adds "all fields" (which for some newsfeeds can look bulky).
   If you have an API you want to post from, copy the JSON and ask chatgipity for a clean looking version of that feed to add to your state.json (and show that structure).

5. **Initialize `state.json`** as an empty JSON object:

   ```bash
   echo "{}" > state.json
   ```

---

## Running the Bot

With your virtual environment active:

```bash
pipenv run python NewsBotwoman.py
```

You should see a splash banner in the logs and then periodic polling logs. The bot will post new items into the configured channel.

---

## Running as a Systemd Service

To keep the bot running across reboots, set it up as a systemd service:

1. **Create the service file**

   ```bash
   sudo nano /etc/systemd/system/newsbot.service
   ```

2. **Paste the following**, updating paths and your username:

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

3. **Enable and start the service**

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable newsbot
   sudo systemctl start newsbot
   ```

4. **Check status**

   ```bash
   sudo systemctl status newsbot
   ```

---

## Admin Commands

Use the following Discord commands (moderators only) to manage feeds without editing JSON directly:

- `!newsaddfeed <name> <url> [interval]` — Add a new feed to this channel (optional polling interval in minutes; defaults to 5)  
- `!newsremovefeed <url>`               — Remove an existing feed by its URL  
- `!newslistfeeds`                      — List all configured feeds and their target channels  
- `!newssettings`                       — Show the NewsBot settings & commands help card  
- `!newstest <feed number>`             — Fetch and display the very latest entry from the specified feed  

---

Enjoy your dynamic, extensible NewsBot! Feel free to open an issue or contribute on GitHub.
