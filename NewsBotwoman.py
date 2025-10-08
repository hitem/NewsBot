import os
import json
import logging
import re
import html
import asyncio
from datetime import datetime, timedelta

import pytz
import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from urllib.parse import urlencode

# ── Load env ─────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.environ['DISCORD_BOT_TOKEN']
RWL_API_KEY = os.getenv("RWL_API_KEY")
RWL_COUNTRY = os.getenv("RWL_COUNTRY", "SE")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='[%(levelname)s]: %(message)s')
logger = logging.getLogger()
# suppress discord.py internal logs
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("discord.ext.commands").setLevel(logging.ERROR)
logger.info(f"RWL_API_KEY loaded: {'yes' if RWL_API_KEY else 'no'} ({len(RWL_API_KEY) if RWL_API_KEY else 0} chars)")

# ── Config & State ───────────────────────────────────────────────────────────
FEEDS_FILE      = 'feeds.json'
STATE_FILE      = 'state.json'
MODERATOR_ROLES = {"Admins", "Super Friends"}
CET = pytz.timezone('Europe/Stockholm')
HELP_COOLDOWN_SECONDS = 5
next_poll_times = {}  # key: (url, chan_id), value: datetime of next poll

# ── Ransomware.Live embed‐templates ──────────────────────────────────────────
RANSOMWARE_TEMPLATES = {
    "victims": {
        "embed_color": 0xFF0000,
        "embed": {
            "title":       "{post_title}",
            "url":         "{permalink}",
            "description": "{description}",
            "timestamp":   "{discovered}",
            "fields": [
                {"name":   "⚔️ Group",    "value":  "{group_name}", "inline": True},
                {"name":   "🌐 Website",  "value":  "{website}",    "inline": True},
                {"name":   "🕵️ Discovered","value":  "{discovered}",  "inline": True}
            ],
            "auto_fields": False,
            "footer_text": "{discovered}"
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

def iso_utc(dt: datetime) -> str:
    # Always return ISO with 'Z'
    return dt.astimezone(pytz.UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"

def parse_any_iso(s: str) -> datetime:
    # Accept "YYYY-MM-DD HH:MM:SS.ssssss" or "YYYY-MM-DDTHH:MM:SSZ"
    if not s:
        return datetime.min.replace(tzinfo=pytz.UTC)
    s = s.strip().replace(" ", "T")
    if s.endswith("Z"):
        try:
            return datetime.fromisoformat(s[:-1]).replace(tzinfo=pytz.UTC)
        except Exception:
            pass
    try:
        # If no tz, assume UTC
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=pytz.UTC) if dt.tzinfo is None else dt.astimezone(pytz.UTC)
    except Exception:
        return datetime.min.replace(tzinfo=pytz.UTC)

def strip_html_to_text(s: str) -> str:
    if not s: return ""
    s = re.sub(r'(?i)<br\s*/?>', '\n', s)
    s = re.sub(r'<[^>]+>', '', s)
    return html.unescape(s).strip()

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

    interval = feed.get('poll_interval_minutes', 5)
    @tasks.loop(minutes=interval)
    async def poll_feed():
        try:
            await fetch_and_post(feed)
        except Exception as e:
            logger.error(f"[{feed.get('name')}] Error: {e}")
        # After finishing, set next poll time
        next_poll_times[key] = datetime.now(CET) + timedelta(minutes=interval)

    # Set first poll time (delayed start)
    next_poll_times[key] = datetime.now(CET) + timedelta(minutes=interval)
    feed_tasks[key] = poll_feed
    bot.loop.call_later(interval*60, poll_feed.start)

def stop_feed_task(feed):
    key = (feed['url'], feed['channel_id'])
    task = feed_tasks.pop(key, None)
    if task:
        task.cancel()

# ── Core: Fetch, Detect New, Post ─────────────────────────────────────────────
async def fetch_and_post(feed):
    logger.info(f"[{name}] provider={feed.get('provider')} url={url}")
    url, name, chan_id = feed['url'], feed.get('name', feed['url']), feed['channel_id']
    provider = feed.get("provider", "generic_json")
    state_key = f"{provider}:{url}::{chan_id}"

    channel = bot.get_channel(chan_id)
    if channel is None:
        logger.error(f"[{name}] Channel {chan_id} not found")
        return

    async with aiohttp.ClientSession() as session:
        # ===== Provider: RWL victims PRO (ordered by discovered DESC) =====
        if provider == "rwl_victims_pro":
            # 1) Load state (discovered watermark + ids at that time)
            raw_s = state.get(state_key) or {}
            last_disc = raw_s.get("last_seen_disc", "")
            ids_at_last = set(raw_s.get("ids_seen_at_last_disc", []))

            # 2) Fetch latest slice
            victims = await fetch_rwl_victims_since(session, RWL_COUNTRY, limit=100)
            if not victims:
                return

            # 3) First run: seed watermark to newest discovered, no posts
            newest_disc = victims[0].get("discovered", "")
            if not last_disc:
                state[state_key] = {"last_seen_disc": newest_disc, "ids_seen_at_last_disc": []}
                save_json(STATE_FILE, state)
                logger.info(f"[{name}] Seeded watermark {newest_disc} (no posts).")
                return

            # 4) Collect new items (respect equals only if id not seen at last watermark)
            to_post = []
            for v in victims:
                disc = v.get("discovered", "")
                vid  = v.get("id", "")
                if not disc or not vid:
                    continue
                if disc > last_disc or (disc == last_disc and vid not in ids_at_last):
                    to_post.append(normalize_rwl_victim(v))

            if not to_post:
                return

            # 5) Post oldest→newest for stable order
            to_post.sort(key=lambda i: (parse_any_iso(i["discovered"]), i["post_title"]))
            for item in to_post:
                embed = build_dynamic_embed(feed, item)
                await channel.send(embed=embed)
                logger.info(f"[{name}] Posted: {item['post_title']} (disc {item['discovered']})")
                await asyncio.sleep(1)

            # 6) Advance watermark and ids
            max_disc = max(parse_any_iso(i["discovered"]) for i in to_post)
            max_disc_s = iso_utc(max_disc)
            if max_disc_s > last_disc:
                ids_last = [i["id"] for i in to_post if parse_any_iso(i["discovered"]) == max_disc]
                state[state_key] = {"last_seen_disc": max_disc_s, "ids_seen_at_last_disc": ids_last}
            else:
                ids_last = list(ids_at_last | {i["id"] for i in to_post if i["discovered"] == last_disc})
                state[state_key] = {"last_seen_disc": last_disc, "ids_seen_at_last_disc": ids_last}
            save_json(STATE_FILE, state)
            return

        # ===== Legacy/free & generic JSON =====
        # 1) Fetch JSON (no API key here)
        headers = {'Accept': 'application/json'}
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                logger.error(f"[{name}] HTTP {resp.status}")
                return
            data = await resp.json()

        # 2) Parse based on template timestamp (published-based)
        tpl = feed.get('embed', {})
        timestamp_tpl = tpl.get('timestamp', '{timestamp}')
        raw = data if isinstance(data, list) else data.get('items', [])
        parsed = []
        for item in raw:
            try:
                dt_pub = datetime.fromisoformat(timestamp_tpl.format_map(item))
            except:
                dt_pub = datetime.min
            try:
                dt_disc = datetime.fromisoformat(item.get('discovered', '') or '1900-01-01T00:00:00')
            except:
                dt_disc = datetime.min
            parsed.append((dt_pub, dt_disc, item))
        parsed.sort(key=lambda x: (x[0], x[1]), reverse=True)
        if not parsed:
            return

        # 3) Load state (published + titles)
        raw_s = state.get(state_key)
        if isinstance(raw_s, dict):
            last_pub = raw_s.get('last_seen_pub', '')
            titles_at_last = set(raw_s.get('titles_seen_at_last_pub', []))
        else:
            last_pub = raw_s if isinstance(raw_s, str) else ''
            titles_at_last = set()

        # 4) Collect new
        to_post = []
        for dt_pub, dt_disc, item in parsed:
            pubstr = dt_pub.isoformat()
            title = item.get('post_title') or item.get('title') or f"__fallback__{pubstr}"
            if pubstr > last_pub or (pubstr == last_pub and title not in titles_at_last):
                to_post.append((dt_pub, dt_disc, item))

        if not to_post:
            return

        # 5) Post oldest→newest for stable order
        to_post.sort(key=lambda x: (x[0], x[2].get('post_title','')))
        for dt_pub, dt_disc, item in to_post:
            pubstr = dt_pub.isoformat()
            resolved_title = item.get('post_title') or item.get('title') or f"__fallback__{pubstr}"
            embed = build_dynamic_embed(feed, item)
            await channel.send(embed=embed)

            if pubstr > last_pub:
                last_pub = pubstr
                titles_at_last = {resolved_title}
            else:
                titles_at_last.add(resolved_title)

            state[state_key] = {
                'last_seen_pub': last_pub,
                'titles_seen_at_last_pub': list(titles_at_last)
            }
            save_json(STATE_FILE, state)
            logger.info(f"[{name}] Posted: {resolved_title} at {pubstr} in channel {chan_id}")
            await asyncio.sleep(1)

# ── RWL Pro victims fetcher ───────────────────────────────────────────────────
async def fetch_rwl_victims_since(session: aiohttp.ClientSession, country: str, limit: int = 100, page: int = 1):
    base = "https://api-pro.ransomware.live/victims/search"
    q = {
        "country": country,
        "order": "discovered",
        "direction": "desc",
        "limit": limit
    }
    url = f"{base}?{urlencode(q)}"
    headers = {"Accept": "application/json"}
    if RWL_API_KEY:
        headers["X-API-KEY"] = RWL_API_KEY
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"rwl pro victims HTTP {resp.status}")
        js = await resp.json()
    victims = js.get("victims", []) if isinstance(js, dict) else []
    # Normalize some fields, just in case
    for v in victims:
        v.setdefault("permalink", v.get("post_url") or "")
        v.setdefault("website", v.get("website") or "")
        v["description"] = strip_html_to_text(v.get("description", ""))
    return victims

def normalize_rwl_victim(v: dict) -> dict:
    return {
        "post_title":  v.get("post_title") or v.get("title") or "(no title)",
        "group_name":  v.get("group_name") or "",
        "website":     v.get("website")    or "",
        "description": v.get("description") or "",
        "published":   v.get("published")   or "",
        "discovered":  v.get("discovered")  or "",
        "permalink":   v.get("permalink")   or v.get("post_url") or "",
        "post_url":    v.get("post_url")    or "",
        "id":          v.get("id")          or "",    # stable dedupe key
        "country":     v.get("country")     or ""
    }

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
    if not has_moderator_role(ctx):
        return await ctx.send("🚫 You lack permissions.")
    if len(args) < 1:
        return await ctx.send("⚠️ Usage: `!newsaddfeed <name?> <url> [interval]`")

    # Parse args: optional name, required URL, optional interval
    try:
        interval = int(args[-1]); url = args[-2]; name = " ".join(args[:-2]).strip()
    except:
        interval = 5; url = args[-1]; name = " ".join(args[:-1]).strip()

    if not url.startswith(("http://","https://")):
        return await ctx.send("⚠️ Need a valid URL (http/https).")

    # Detect provider + set template + default name
    provider = "generic_json"
    tpl = {"embed_color":0x1ABC9C,"embed":{
        "title":"{title}","url":"{url}","description":"{description}",
        "timestamp":"{timestamp}","fields":[],"auto_fields":True,
        "footer_text":"{timestamp}"}}

    # ---- Pro victims (discovered) ----
    if "api-pro.ransomware.live/victims/search" in url:
        provider = "rwl_victims_pro"
        tpl = RANSOMWARE_TEMPLATES["victims"]
        qparts = []
        if "country=" not in url:
            qparts.append(f"country={RWL_COUNTRY}")
        if "order=" not in url:
            qparts.append("order=discovered")
        if "direction=" not in url:
            qparts.append("direction=desc")
        if "limit=" not in url:
            qparts.append("limit=100")
        if qparts:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{'&'.join(qparts)}"
        if not name:
            name = f"RWL Victims ({RWL_COUNTRY})"

    # ---- Free victims JSON (legacy) ----
    elif "countryvictims" in url:
        provider = "rwl_victims_free"
        tpl = RANSOMWARE_TEMPLATES["victims"]
        if not name:
            cn = url.rstrip('/').split('/')[-1].upper()
            name = f"Ransomware Victims ({cn})"

    # ---- Free cyberattacks JSON (legacy) ----
    elif "countrycyberattacks" in url:
        provider = "rwl_cyberattacks_free"
        tpl = RANSOMWARE_TEMPLATES["cyberattacks"]
        if not name:
            cn = url.rstrip('/').split('/')[-1].upper()
            name = f"Ransomware News ({cn})"

    # ---- Generic JSON fallback ----
    else:
        if not name:
            name = discord.utils.escape_markdown(url.split("//",1)[1].split("/",1)[0])

    # Prevent duplicates in same channel (match by provider+url+channel)
    if any(f.get('provider') == provider and f['url'] == url and f['channel_id'] == ctx.channel.id for f in feeds):
        return await ctx.send("⚠️ Feed already exists in this channel.")

    # Reset per-feed state key (now includes provider)
    comp = f"{provider}:{url}::{ctx.channel.id}"
    if comp in state:
        state.pop(comp); save_json(STATE_FILE, state)

    # Build and persist the feed config
    feed = {
        "name": name,
        "url": url,
        "poll_interval_minutes": interval,
        "channel_id": ctx.channel.id,
        "embed_color": tpl["embed_color"],
        "embed": tpl["embed"],
        "provider": provider
    }
    feeds.append(feed); save_json(FEEDS_FILE, feeds)

    # Start task + immediate fetch
    start_feed_task(feed)
    await fetch_and_post(feed)

    await ctx.send(f"✅ Added **{name}** every {interval} min (provider: `{provider}`) with color `#{tpl['embed_color']:06X}`.")
    logger.info(f"{ctx.author} used newsaddfeed in {ctx.channel} → {name} ({provider})")

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

    provider = feed.get("provider","generic_json")
    comp = f"{provider}:{feed['url']}::{feed['channel_id']}"
    if comp in state:
        state.pop(comp); save_json(STATE_FILE, state)

    await ctx.send(f"🗑️ Removed **{feed['name']}** (#{index}).")
    logger.info(f"{ctx.author} used newsremovefeed command in {ctx.channel}")

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
    logger.info(f"{ctx.author} used newslistfeeds command in {ctx.channel}")

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
        "- `!newstimer <#>` — Show time until next poll for a feed.\n"
    )
    embed = discord.Embed(title="NewsBot Help", description=help_text, colour=0x1ABC9C)
    embed = await attach_embed_info(ctx, embed)
    await ctx.send(embed=embed)
    logger.info(f"{ctx.author} used newssettings command in {ctx.channel}")

@bot.command(name='newstest')
async def news_test(ctx, index: int):
    guild_feeds = [f for f in feeds if (bot.get_channel(f['channel_id']) or discord.Object(id=0)).guild == ctx.guild]
    if not guild_feeds: return await ctx.send("No feeds here.")
    if index<1 or index>len(guild_feeds): return await ctx.send("⚠️ Invalid feed #.")
    feed = guild_feeds[index-1]

    provider = feed.get("provider","generic_json")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        try:
            if provider == "rwl_victims_pro":
                victims = await fetch_rwl_victims_since(session, RWL_COUNTRY, limit=10)
                if not victims:
                    return await ctx.send("No items found.")
                item = normalize_rwl_victim(victims[0])
                return await ctx.send(embed=build_dynamic_embed(feed, item))
            else:
                headers = {'Accept':'application/json'}
                async with session.get(feed['url'], headers=headers) as resp:
                    data = await resp.json() if resp.status==200 else None
        except asyncio.TimeoutError:
            return await ctx.send("❌ Request timed out.")

    if provider != "rwl_victims_pro":
        if not data:
            return await ctx.send("❌ Cannot fetch feed.")
        items = data if isinstance(data,list) else data.get('items',[])
        if not items:
            return await ctx.send("No items found.")
        items.sort(key=lambda i: (i.get('published') or ''), reverse=True)
        await ctx.send(embed=build_dynamic_embed(feed, items[0]))

@bot.command(name='newstimer')
async def news_timer(ctx, index: int):
    guild_feeds = [f for f in feeds if (bot.get_channel(f['channel_id']) or discord.Object(id=0)).guild == ctx.guild]
    if not guild_feeds:
        return await ctx.send("No feeds here.")
    if index < 1 or index > len(guild_feeds):
        return await ctx.send("⚠️ Invalid feed #.")
    feed = guild_feeds[index-1]
    key = (feed['url'], feed['channel_id'])
    next_time = next_poll_times.get(key)
    if next_time:
        now = datetime.now(CET)
        delta = next_time - now
        minutes, seconds = divmod(int(delta.total_seconds()), 60)
        await ctx.send(f"⏳ Next poll for **{feed['name']}** in {minutes}m {seconds}s")
    else:
        await ctx.send(f"Timer not started or no info for **{feed['name']}**.")
    logger.info(f"{ctx.author} used newstimer command in {ctx.channel}")

if __name__ == '__main__':
    bot.run(TOKEN)
