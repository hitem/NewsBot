import os
import json
import logging
import re
import html
import asyncio
from datetime import datetime

import pytz
import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ── Load env ─────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.environ['DISCORD_BOT_TOKEN']

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='[%(levelname)s]: %(message)s')
logger = logging.getLogger()
# suppress discord.py internal logs
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("discord.ext.commands").setLevel(logging.ERROR)

# ── Config & State ───────────────────────────────────────────────────────────
FEEDS_FILE      = 'feeds.json'
STATE_FILE      = 'state.json'
MODERATOR_ROLES = {"Admins", "Super Friends"}
CET = pytz.timezone('Europe/Stockholm')
HELP_COOLDOWN_SECONDS = 5

# ── Ransomware.Live embed‐templates ──────────────────────────────────────────
RANSOMWARE_TEMPLATES = {
    "victims": {
        "embed_color": 0xFF0000,
        "embed": {
            "title":       "{post_title}",
            "url":         "{post_url}",
            "description": "{description}",
            "timestamp":   "{published}",
            "fields": [
                {"name":   "⚔️ Group",    "value":  "{group_name}", "inline": True},
                {"name":   "🌐 Website",  "value":  "{website}",    "inline": True},
                {"name":   "🕵️ Discovered","value":  "{discovered}",  "inline": True}
            ],
            "auto_fields": False,
            "footer_text": "{published}"
        }
    },
    "cyberattacks": {
        "embed_color": 0xFF6630,
        "embed": {
            "title":       "{title}",
            "url":         "{url}",
            "description": "{summary}",
            "timestamp":   "{added}",
            "fields": [
                {"name": "🗓️ Alleged breach date", "value": "{date}",      "inline": True},
                {"name": "⚔️ Gang",                "value": "{claim_gang}","inline": True},
                {"name": "🌐 Domain",              "value": "{domain}",    "inline": True},
                {"name": "🔗 Link",                "value": "[Read more]({link})","inline": False}
            ],
            "auto_fields": False,
            "footer_text": "{added}"
        }
    }
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_json(path, default):
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def has_moderator_role(ctx):
    return any(r.name in MODERATOR_ROLES for r in ctx.author.roles)

# ── State ─────────────────────────────────────────────────────────────────────
feeds      = load_json(FEEDS_FILE, [])
state      = load_json(STATE_FILE, {})
feed_tasks = {}

# ── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    logger.info("#############################################################")
    logger.info("#   Created by hitem      #github.com/hitem       NewsBot   #")
    logger.info("#############################################################")
    logger.info(f"Logged in as {bot.user.name}")
    for feed in feeds:
        start_feed_task(feed)
        logger.info(f"Started feed task for channel ID: {feed['channel_id']}")
    logger.info("Bot is ready to receive commands")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    logger.error(f"Error in {ctx.command}: {error}")

@bot.event
async def on_disconnect():
    logger.warning("Lost connection to Discord…")

@bot.event
async def on_resumed():
    logger.info("Reconnected to Discord (session resumed)")

@bot.event
async def on_connect():
    logger.info("Connected to Discord gateway")

# ── Feed Task Management ─────────────────────────────────────────────────────

def start_feed_task(feed):
    url, chan_id = feed['url'], feed['channel_id']
    key = (url, chan_id)
    if key in feed_tasks:
        return

    @tasks.loop(minutes=feed.get('poll_interval_minutes', 5))
    async def poll_feed():
        try:
            await fetch_and_post(feed)
        except Exception as e:
            logger.error(f"[{feed.get('name')}] Error: {e}")

    feed_tasks[key] = poll_feed
    bot.loop.call_later(feed.get('poll_interval_minutes', 5)*60, poll_feed.start)

def stop_feed_task(feed):
    key = (feed['url'], feed['channel_id'])
    task = feed_tasks.pop(key, None)
    if task:
        task.cancel()

# ── Core: Fetch, Detect New, Post ─────────────────────────────────────────────
async def fetch_and_post(feed):
    url, name, chan_id = feed['url'], feed.get('name', feed['url']), feed['channel_id']
    state_key = f"{url}::{chan_id}"

    # 1) Fetch JSON
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers={'Accept': 'application/json'}) as resp:
            if resp.status != 200:
                logger.error(f"[{name}] HTTP {resp.status}")
                return
            data = await resp.json()

    # 2) Build (pub, disc, item)
    tpl           = feed.get('embed', {})
    timestamp_tpl = tpl.get('timestamp', '{timestamp}')
    raw           = data if isinstance(data, list) else data.get('items', [])
    parsed = []
    for item in raw:
        # published
        try:
            dt_pub = datetime.fromisoformat(timestamp_tpl.format_map(item))
        except:
            dt_pub = datetime.min
        # discovered
        try:
            dt_disc = datetime.fromisoformat(item.get('discovered', ''))
        except:
            dt_disc = datetime.min
        parsed.append((dt_pub, dt_disc, item))

    parsed.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if not parsed:
        return

    # 3) Load/migrate state
    raw_s = state.get(state_key)
    if isinstance(raw_s, dict):
        last_pub  = datetime.fromisoformat(raw_s.get('last_seen_pub'))
        last_disc = datetime.fromisoformat(raw_s.get('last_seen_disc'))
    else:
        try:
            last_pub = datetime.fromisoformat(raw_s)
        except:
            last_pub = datetime.min
        last_disc = datetime.min

    # 4) Collect new
    to_post = []
    for dt_pub, dt_disc, item in parsed:
        if (dt_pub, dt_disc) > (last_pub, last_disc):
            to_post.append((dt_pub, dt_disc, item))
    if not to_post:
        return

    # 5) Post in chronological order
    channel = bot.get_channel(chan_id)
    if channel is None:
        logger.error(f"[{name}] Channel {chan_id} not found")
        return

    for dt_pub, dt_disc, item in reversed(to_post):
        embed = build_dynamic_embed(feed, item)
        await channel.send(embed=embed)

        last_pub, last_disc = dt_pub, dt_disc
        state[state_key] = {
            'last_seen_pub':  last_pub.isoformat(),
            'last_seen_disc': last_disc.isoformat()
        }
        save_json(STATE_FILE, state)
        logger.info(f"[{name}] Posted up to {state[state_key]} in channel {chan_id}")
        await asyncio.sleep(1)

# ── Embed Builder ─────────────────────────────────────────────────────────────
def build_dynamic_embed(feed, item):
    tpl = feed.get('embed', {})
    def safe(s: str) -> str:
        try:
            return s.format_map(item)
        except:
            return ''

    title = safe(tpl.get('title')) or feed['name']
    raw_u = safe(tpl.get('url', ''))
    url   = raw_u if raw_u.startswith(('http://','https://')) else None

    rd = safe(tpl.get('description',''))
    rd = re.sub(r'<br\s*/?>','\n',rd,flags=re.IGNORECASE)
    rd = re.sub(r'<[^>]+>','',rd)
    desc = html.unescape(rd).strip() or None

    ts = safe(tpl.get('timestamp',''))
    try:
        dt = datetime.fromisoformat(ts)
    except:
        dt = datetime.now(CET)

    embed = discord.Embed(
        title=title,
        url=url,
        description=desc,
        timestamp=dt,
        color=feed.get('embed_color', 0x1ABC9C)
    )
    website = item.get('domain') or item.get('website')
    if website:
        logo = f"https://logo.clearbit.com/{website.lower().strip()}?size=128"
        embed.set_thumbnail(url=logo)
    embed.set_author(name=feed['name'])

    for fld in tpl.get('fields', []):
        name = fld.get('name','')
        rawv = fld.get('value','')
        if not name or not rawv: continue
        val = safe(rawv)
        if name == "🔗 Link":
            m = re.search(r'\((https?://[^\)]+)\)', val)
            if m:
                link = re.sub(r'(ransomware\.live/id)(?!/)',r'\1/',m.group(1))
                val = f"[Read more]({link})"
        embed.add_field(name=name, value=val, inline=fld.get('inline',True))

    if tpl.get('auto_fields', False):
        shown = {f['name'] for f in tpl.get('fields', [])}
        for k,v in item.items():
            if k in shown: continue
            t = str(v)
            if len(t)>100: t = t[:100].rsplit(' ',1)[0] + '…'
            embed.add_field(name=k, value=t, inline=True)

    if 'ransomware.live/v2/countryvictims/' in feed['url']:
        try:
            c = feed['url'].rstrip('/').split('/')[-1].upper()
            m = f"https://www.ransomware.live/map/{c.lower()}"
            embed.add_field(name="🔗 View on Ransomware.Live", value=f"[Open map for {c}]({m})", inline=False)
        except:
            pass

    ftxt = tpl.get('footer_text','{timestamp}')
    embed.set_footer(text=safe(ftxt))
    return embed

# ── Admin Commands ────────────────────────────────────────────────────────────
@bot.command(name='newsaddfeed')
@commands.cooldown(1,10,commands.BucketType.user)
async def news_add_feed(ctx, *args):
    if not has_moderator_role(ctx): return await ctx.send("🚫 You lack permissions.")
    if len(args)<1:        return await ctx.send("⚠️ Usage: `!newsaddfeed <name?> <url> [interval]`")
    
    try:
        interval = int(args[-1]); url = args[-2]; name = " ".join(args[:-2])
    except:
        interval = 5; url = args[-1]; name = " ".join(args[:-1])
    if not url.startswith("http"): return await ctx.send("⚠️ Need a valid URL.")

    if not name:
        if "countryvictims" in url:
            cn = url.rstrip('/').split('/')[-1].upper()
            name = f"Ransomware Victims ({cn})"
        elif "countrycyberattacks" in url:
            cn = url.rstrip('/').split('/')[-1].upper()
            name = f"Ransomware News ({cn})"
        else:
            name = discord.utils.escape_markdown(url.split("//",1)[1].split("/",1)[0])

    if any(f['url']==url and f['channel_id']==ctx.channel.id for f in feeds):
        return await ctx.send("⚠️ Feed already exists in this channel.")
    comp = f"{url}::{ctx.channel.id}"
    if comp in state:
        state.pop(comp); save_json(STATE_FILE, state)

    if "countryvictims" in url:
        tpl = RANSOMWARE_TEMPLATES["victims"]
    elif "countrycyberattacks" in url:
        tpl = RANSOMWARE_TEMPLATES["cyberattacks"]
    else:
        tpl = {"embed_color":0x1ABC9C,"embed":{
            "title":"{title}","url":"{url}","description":"{description}",
            "timestamp":"{timestamp}","fields":[],"auto_fields":True,
            "footer_text":"{timestamp}"}}

    feed = {"name":name,"url":url,"poll_interval_minutes":interval,
            "channel_id":ctx.channel.id,
            "embed_color":tpl["embed_color"],"embed":tpl["embed"]}
    feeds.append(feed); save_json(FEEDS_FILE, feeds)
    start_feed_task(feed)
    await fetch_and_post(feed)
    await ctx.send(f"✅ Added **{name}** every {interval} min with color `#{tpl['embed_color']:06X}`.")

@bot.command(name='newsremovefeed')
@commands.cooldown(1,10,commands.BucketType.user)
async def news_remove_feed(ctx, index: int):
    if not has_moderator_role(ctx): return await ctx.send("🚫 You lack permissions.")
    guild_feeds = [f for f in feeds if (bot.get_channel(f['channel_id']) or discord.Object(id=0)).guild == ctx.guild]
    if not guild_feeds: return await ctx.send("No feeds here.")
    if index<1 or index>len(guild_feeds): return await ctx.send("⚠️ Invalid feed #.")
    feed = guild_feeds[index-1]
    stop_feed_task(feed)
    feeds.remove(feed); save_json(FEEDS_FILE, feeds)
    comp = f"{feed['url']}::{feed['channel_id']}"
    if comp in state:
        state.pop(comp); save_json(STATE_FILE, state)
    await ctx.send(f"🗑️ Removed **{feed['name']}** (#{index}).")

@bot.command(name='newslistfeeds')
async def news_list_feeds(ctx):
    if not has_moderator_role(ctx): return await ctx.send("🚫 You lack permissions.")
    guild_feeds = [f for f in feeds if (bot.get_channel(f['channel_id']) or discord.Object(id=0)).guild == ctx.guild]
    if not guild_feeds: return await ctx.send("No feeds here.")
    lines=[]
    for i,f in enumerate(guild_feeds,1):
        ch = bot.get_channel(f['channel_id'])
        mention = ch.mention if ch else f"`{f['channel_id']}`"
        lines.append(f"{i}. **{f['name']}** → {mention} every {f['poll_interval_minutes']} min\n<{f['url']}>")
    await ctx.send("**Configured feeds:**\n"+"\n".join(lines))

async def attach_embed_info(ctx, embed: discord.Embed) -> discord.Embed:
    if ctx.guild and ctx.guild.icon:
        icon = ctx.guild.icon.url
        embed.set_author(name=ctx.bot.user.name, icon_url=icon)
        embed.set_thumbnail(url=icon)
    else:
        embed.set_author(name=ctx.bot.user.name)
    embed.set_footer(text="by: hitem")
    return embed

@bot.command(name='newssettings')
@commands.cooldown(1, HELP_COOLDOWN_SECONDS, commands.BucketType.user)
async def news_settings(ctx):
    help_text = (
        "- `!newsaddfeed <name?> <url> [interval]` — Add a feed.\n"
        "- `!newsremovefeed <#>` — Remove a feed.\n"
        "- `!newslistfeeds` — List feeds.\n"
        "- `!newstest <#>` — Show latest entry.\n"
        "- `!newssettings` — Show this help card.\n"
    )
    embed = discord.Embed(title="NewsBot Help", description=help_text, colour=0x1ABC9C)
    embed = await attach_embed_info(ctx, embed)
    await ctx.send(embed=embed)
    logger.info(f"{ctx.author} used newssettings command")

@bot.command(name='newstest')
async def news_test(ctx, index: int):
    guild_feeds = [f for f in feeds if (bot.get_channel(f['channel_id']) or discord.Object(id=0)).guild == ctx.guild]
    if not guild_feeds: return await ctx.send("No feeds here.")
    if index<1 or index>len(guild_feeds): return await ctx.send("⚠️ Invalid feed #.")
    feed = guild_feeds[index-1]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        try:
            async with session.get(feed['url'], headers={'Accept':'application/json'}) as resp:
                data = await resp.json() if resp.status==200 else None
        except asyncio.TimeoutError:
            return await ctx.send("❌ Request timed out.")
    if not data:
        return await ctx.send("❌ Cannot fetch feed.")
    items = data if isinstance(data,list) else data.get('items',[])
    if not items:
        return await ctx.send("No items found.")
    items.sort(key=lambda i: (i.get('published') or ''), reverse=True)
    await ctx.send(embed=build_dynamic_embed(feed, items[0]))

if __name__ == '__main__':
    bot.run(TOKEN)
