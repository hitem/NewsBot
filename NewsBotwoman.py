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
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

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
# but logg this bad boi
logger.info(
    f"RWL_API_KEY loaded: {'yes' if RWL_API_KEY else 'no'} ({len(RWL_API_KEY) if RWL_API_KEY else 0} chars)")

# ── Config & State ───────────────────────────────────────────────────────────
FEEDS_FILE = 'feeds.json'
STATE_FILE = 'state.json'
MODERATOR_ROLES = {"Admins", "Super Friends"}
CET = pytz.timezone('Europe/Stockholm')
HELP_COOLDOWN_SECONDS = 5
next_poll_times = {}  # key: (provider, url, chan_id) -> next poll datetime
MAX_POSTS_PER_POLL = 10  # Maximum successful posts per feed poll
ARTICLE_HISTORY_LIMIT = 500  # Maximum remembered articles PER Discord channel
LATE_ARRIVAL_LOOKBACK_DAYS = 7  # Accept articles dated up to 7 days before last watermark

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
                {"name": "⚔️ Group", "value": "{group_name}", "inline": True},
                {"name": "🌐 Website", "value": "{website_link}", "inline": True},
                {"name": "🕵️ Discovered", "value": "{discovered}", "inline": True},
                {"name": "🔗 Leak post", "value": "{post_link}", "inline": False}
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
                {"name": "🗓️ Alleged breach date",
                    "value": "{date}",      "inline": True},
                {"name": "⚔️ Gang",
                    "value": "{claim_gang}", "inline": True},
                {"name": "🌐 Domain",
                    "value": "{domain}",    "inline": True},
                {"name": "🔗 Link",
                    "value": "[Read more]({link})", "inline": False}
            ],
            "auto_fields": False,
            "footer_text": "{added}"
        }
    }
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def canonicalize_query_url(u: str) -> str:
    try:
        p = urlparse(u)
        if not p.query:
            return u
        q = {}
        for k, v in parse_qsl(p.query, keep_blank_values=True):
            q[k] = v
        new_query = urlencode(sorted(q.items()), doseq=True)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))
    except Exception:
        return u
    
def load_json(path, default):
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return default


def save_json(path, data):
    # Atomic replace prevents an interrupted write from corrupting state.json.
    tmp = f"{path}.tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


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
    if not s:
        return ""
    s = re.sub(r'(?i)<br\s*/?>', '\n', s)
    s = re.sub(r'<[^>]+>', '', s)
    return html.unescape(s).strip()

def at_second(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def article_aliases(item: dict, provider: str = '') -> list:
    """Match stable URL(s)/ID; use title only if no stable identity exists."""
    aliases = []
    for field in ('url', 'link', 'permalink', 'post_url'):
        raw = item.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            parts = urlparse(raw.strip())
            if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
                continue
            host = parts.netloc.lower()
            if host.startswith('www.'):
                host = host[4:]
            path = parts.path.rstrip('/') or '/'
            params = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                            if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid'))
            canonical = urlunparse(('https', host, path, '', urlencode(params), ''))
            aliases.append('url:' + canonical)
        except (ValueError, TypeError):
            continue
    stable_id = item.get('id')
    if stable_id is not None and str(stable_id).strip():
        aliases.append(f'id:{provider}:' + str(stable_id).strip())
    if not aliases:
        title = item.get('post_title') or item.get('title')
        if isinstance(title, str) and title.strip():
            aliases.append('title:' + ' '.join(title.lower().split()))
    return sorted(set(aliases))


def channel_history_key(channel) -> str:
    # Includes the server explicitly. Never mix history between channels.
    return f"{channel.guild.id}:{channel.id}"


def has_been_posted(channel_key: str, aliases: list) -> bool:
    history = state.get('_posted_articles', {}).get(channel_key, [])
    keys = set(aliases)
    return bool(keys) and any(keys.intersection(entry['aliases']) for entry in history)


def remember_post(channel_key: str, aliases: list) -> bool:
    if not aliases or has_been_posted(channel_key, aliases):
        return False
    per_channel = state.setdefault('_posted_articles', {})
    history = per_channel.setdefault(channel_key, [])
    history.append({'aliases': aliases})
    del history[:-ARTICLE_HISTORY_LIMIT]
    return True


def truncate_text(value, limit: int) -> str:
    text = '' if value is None else str(value)
    return text if len(text) <= limit else text[:limit - 1] + '…'

# ── State ─────────────────────────────────────────────────────────────────────
feeds = load_json(FEEDS_FILE, [])
state = load_json(STATE_FILE, {})
feed_tasks = {}
channel_locks = {}  # prevent overlapping polls from posting to one channel simultaneously

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
    logger.info(f"State file: {os.path.abspath(STATE_FILE)}")
    for feed in feeds:
        start_feed_task(feed)
        logger.info(f"Started feed task for channel ID: {feed['channel_id']}")
    logger.info("Bot is ready to receive commands")

@bot.event
async def on_command(ctx):
    # fires when a valid command name is parsed (before it runs)
    logger.info(f"[CMD] attempt by {ctx.author} in #{ctx.channel}: {ctx.message.content}")

@bot.event
async def on_command_completion(ctx):
    logger.info(f"[CMD] success: {ctx.command} by {ctx.author} in #{ctx.channel}")

@bot.event
async def on_command_error(ctx, error):
    # log unknown commands + everything else
    if isinstance(error, commands.CommandNotFound):
        logger.info(f"[CMD] unknown by {ctx.author} in #{ctx.channel}: {ctx.message.content}")
        return
    logger.error(f"[CMD] error in {ctx.command} for {ctx.author}: {error}")

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
    url = feed['url']
    chan_id = feed['channel_id']
    provider = feed.get('provider', 'generic_json')
    key_url = canonicalize_query_url(url) if provider == "rwl_victims_pro" else url
    key = (provider, key_url, chan_id)

    if key in feed_tasks:
        return

    interval = feed.get('poll_interval_minutes', 5)

    @tasks.loop(minutes=interval)
    async def poll_feed():
        try:
            await fetch_and_post(feed)
        except Exception as e:
            logger.exception(f"[{feed.get('name')}] Poll failed")
        # update “next poll” timestamp for this provider/url/channel
        next_poll_times[key] = datetime.now(CET) + timedelta(minutes=interval)

    # set first “next poll” estimate (delayed start)
    next_poll_times[key] = datetime.now(CET) + timedelta(minutes=interval)
    feed_tasks[key] = poll_feed
    bot.loop.call_later(interval*60, poll_feed.start)

def stop_feed_task(feed):
    provider = feed.get('provider', 'generic_json')
    url = feed['url']
    key_url = canonicalize_query_url(url) if provider == "rwl_victims_pro" else url
    key = (provider, key_url, feed['channel_id'])
    task = feed_tasks.pop(key, None)
    if task:
        task.cancel()

# ── Core: Fetch, Detect New, Post ─────────────────────────────────────────────
async def fetch_and_post(feed):
    url = feed['url']
    name = feed.get('name', url)
    chan_id = feed['channel_id']
    provider = feed.get('provider', 'generic_json')
    state_key_url = canonicalize_query_url(url) if provider == 'rwl_victims_pro' else url
    state_key = f"{provider}:{state_key_url}::{chan_id}"

    channel = bot.get_channel(chan_id)
    if channel is None:
        logger.error(f"[{name}] Channel {chan_id} not found")
        return
    channel_key = channel_history_key(channel)

    # A shared lock protects channels that have multiple feeds (or an immediate fetch).
    lock = channel_locks.setdefault(channel_key, asyncio.Lock())
    async with lock:
        async with aiohttp.ClientSession() as session:
            if provider == 'rwl_victims_pro':
                victims = await fetch_rwl_victims_since(session, RWL_COUNTRY, limit=100)
                items = [normalize_rwl_victim(v) for v in victims]
                timestamp_fields = ('discovered', 'published')
                watermark_field = 'last_seen_disc'
                titles_field = 'ids_seen_at_last_disc'
            else:
                data = None
                for attempt in range(2):
                    async with session.get(url, headers={'Accept': 'application/json'}) as response:
                        if response.status == 200:
                            data = await response.json()
                            break
                        if response.status == 504 and attempt == 0:
                            logger.warning(f"[{name}] HTTP 504; retrying in 60s")
                            await asyncio.sleep(60)
                            continue
                        logger.error(f"[{name}] HTTP {response.status} for {url}")
                        return
                if data is None:
                    return
                items = data if isinstance(data, list) else data.get('items', [])
                timestamp_fields = ('added', 'published', 'date', 'timestamp', 'discovered')
                watermark_field = 'last_seen_pub'
                titles_field = 'titles_seen_at_last_pub'

            if not isinstance(items, list) or not items:
                return

            parsed = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                aliases = article_aliases(item, provider)
                if not aliases:
                    logger.warning(f"[{name}] Skipping article without URL, ID or title")
                    continue
                # Use the configured timestamp when available; other fields as fallback.
                tpl_value = None
                if provider != 'rwl_victims_pro':
                    try:
                        tpl_value = feed.get('embed', {}).get('timestamp', '').format_map(item)
                    except (KeyError, TypeError, ValueError, AttributeError):
                        pass
                candidates = [tpl_value] + [item.get(field) for field in timestamp_fields]
                pub_dt = next((dt for value in candidates
                               if isinstance(value, str) and (dt := parse_any_iso(value)).year >= 1999), None)
                if pub_dt is None:
                    logger.warning(f"[{name}] Article has no valid timestamp; using fetch time: {aliases[0]}")
                    pub_dt = datetime.now(pytz.UTC)
                title = str(item.get('post_title') or item.get('title') or '(untitled)')
                parsed.append((pub_dt, title, item, aliases))

            if not parsed:
                return
            parsed.sort(key=lambda row: (row[0], row[1]))

            raw = state.get(state_key)
            previous = raw if isinstance(raw, dict) else {}
            last_seen = previous.get(watermark_field, raw if isinstance(raw, str) else '')
            last_dt = parse_any_iso(last_seen)
            seen_at_watermark = set(previous.get(titles_field, []))

            def tie_key(item, title):
                return str(item.get('id') or '') if provider == 'rwl_victims_pro' else title

            # New feed: seed all currently visible articles so adding it never floods Discord.
            if not last_seen:
                newest = parsed[-1][0]
                at_newest = {tie_key(item, title) for dt, title, item, _ in parsed if dt == newest}
                for _, _, _, aliases in parsed:
                    remember_post(channel_key, aliases)
                state[state_key] = {
                    watermark_field: iso_utc(newest),
                    titles_field: sorted(at_newest),
                    '_dedup_bootstrapped': True
                }
                save_json(STATE_FILE, state)
                logger.info(f"[{name}] New feed seeded; no historical posts")
                return

            # First run after updating old code: remember older articles already
            # covered by the existing watermark, without re-posting them.
            if not previous.get('_dedup_bootstrapped'):
                for dt, title, item, aliases in parsed:
                    if dt < last_dt or (dt == last_dt and tie_key(item, title) in seen_at_watermark):
                        remember_post(channel_key, aliases)
                previous['_dedup_bootstrapped'] = True
                state[state_key] = previous
                save_json(STATE_FILE, state)

            # Lookback allows items to arrive late with earlier publication dates.
            cutoff = last_dt - timedelta(days=LATE_ARRIVAL_LOOKBACK_DAYS)
            pending = [row for row in parsed
                       if row[0] >= cutoff and not has_been_posted(channel_key, row[3])]
            if not pending:
                return

            sent = 0
            for pub_dt, title, item, aliases in pending:
                if sent >= MAX_POSTS_PER_POLL:
                    break
                if has_been_posted(channel_key, aliases):
                    continue
                try:
                    embed = build_dynamic_embed(feed, item)
                    await channel.send(embed=embed)
                except Exception:
                    # One bad article must not block the other news in the batch.
                    logger.exception(f"[{name}] Failed to post {title!r}; other articles will continue")
                    continue

                remember_post(channel_key, aliases)
                if pub_dt > last_dt:
                    last_dt = pub_dt
                    seen_at_watermark = {tie_key(item, title)}
                elif pub_dt == last_dt:
                    seen_at_watermark.add(tie_key(item, title))
                state[state_key] = {
                    watermark_field: iso_utc(last_dt),
                    titles_field: sorted(seen_at_watermark)[-ARTICLE_HISTORY_LIMIT:],
                    '_dedup_bootstrapped': True
                }
                save_json(STATE_FILE, state)
                sent += 1
                logger.info(f"[{name}] Posted {title!r} to guild={channel.guild.id} channel={chan_id}")
                await asyncio.sleep(1)

            if len(pending) > sent:
                logger.info(f"[{name}] {len(pending) - sent} items left pending or failed this poll")


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
    js = None
    for attempt in range(2):  # first try + 1 retry
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                js = await resp.json()
                break

            if resp.status == 504 and attempt == 0:
                retry_delay = 20  # change to 60 for 1 minute
                logger.warning(f"[RWL PRO] HTTP 504, retrying once in {retry_delay}s... url={url}")
                await asyncio.sleep(retry_delay)
                continue

            raise RuntimeError(f"rwl pro victims HTTP {resp.status}")

    if js is None:
        raise RuntimeError("rwl pro victims: no data after retry")
    victims = js.get("victims", []) if isinstance(js, dict) else []
    # Normalize some fields, just in case
    for v in victims:
        v.setdefault("permalink", v.get("post_url") or "")
        v.setdefault("website", v.get("website") or "")
        v["description"] = strip_html_to_text(v.get("description", ""))
    return victims


def normalize_rwl_victim(v: dict) -> dict:
    site = (v.get("website") or "").strip()
    site_url = site if site.startswith(
        ("http://", "https://")) else (f"https://{site}" if site else "")
    post_url = v.get("post_url") or ""

    return {
        "post_title":  v.get("post_title") or v.get("title") or "(no title)",
        "group_name":  v.get("group_name") or "",
        "website":     site,
        "website_link": f"[{site}]({site_url})" if site_url else "",
        "description": v.get("description") or "",
        "published":   v.get("published") or "",
        "discovered":  v.get("discovered") or "",
        "permalink":   v.get("permalink") or post_url,
        "post_url":    post_url,
        "post_link":   f"[Open original]({post_url})" if post_url else "",
        "id":          v.get("id") or "",
        "country":     v.get("country") or ""
    }

# ── Embed Builder ─────────────────────────────────────────────────────────────
def build_dynamic_embed(feed, item):
    tpl = feed.get('embed', {})

    def safe(template):
        try:
            return template.format_map(item) if isinstance(template, str) else ''
        except (KeyError, TypeError, ValueError, AttributeError):
            return ''

    title = truncate_text(safe(tpl.get('title')) or feed['name'], 256)
    raw_url = safe(tpl.get('url', ''))
    url = raw_url if raw_url.startswith(('https://', 'http://')) else None
    if not url:
        fallback_url = item.get('post_url') or item.get('permalink')
        if isinstance(fallback_url, str) and fallback_url.startswith(('https://', 'http://')):
            url = fallback_url

    raw_description = safe(tpl.get('description', ''))
    description = truncate_text(strip_html_to_text(raw_description), 3500) or None
    ts = safe(tpl.get('timestamp', ''))
    dt = parse_any_iso(ts) if ts else None
    if dt is not None and dt.year < 1999:
        dt = None

    embed = discord.Embed(
        title=title,
        url=url,
        description=description,
        timestamp=dt,
        color=feed.get('embed_color', 0x1ABC9C)
    )
    website = item.get('domain') or item.get('website')
    if isinstance(website, str) and website.strip():
        embed.set_thumbnail(url=f"https://logo.clearbit.com/{website.lower().strip()}?size=128")
    embed.set_author(name=truncate_text(feed['name'], 256))

    def add_field(field_name, value, inline=True):
        if not value or len(embed.fields) >= 25:
            return
        field_name = truncate_text(field_name, 256)
        available = min(1024, 5800 - len(embed) - len(field_name))
        if available > 0:
            embed.add_field(name=field_name, value=truncate_text(value, available), inline=inline)

    for field in tpl.get('fields', []):
        field_name = field.get('name', '')
        if not field_name:
            continue
        value = safe(field.get('value', ''))
        if field_name == '🔗 Link':
            match = re.search(r'\((https?://[^)]+)\)', value)
            if match:
                link = re.sub(r'(ransomware\.live/id)(?!/)', r'\1/', match.group(1))
                value = f"[Read more]({link})"
        add_field(field_name, value, field.get('inline', True))

    if tpl.get('auto_fields', False):
        shown = {field.get('name') for field in tpl.get('fields', [])}
        for key, value in item.items():
            if key not in shown:
                add_field(key, truncate_text(value, 100))

    if 'ransomware.live/v2/countryvictims/' in feed['url']:
        country = feed['url'].rstrip('/').split('/')[-1].upper()
        add_field('🔗 View on Ransomware.Live',
                  f"[Open map for {country}](https://www.ransomware.live/map/{country.lower()})", False)

    footer = safe(tpl.get('footer_text', '{timestamp}'))
    available = min(2048, 5900 - len(embed))
    if footer and available > 0:
        embed.set_footer(text=truncate_text(footer, available))
    return embed

# ── Admin Commands ────────────────────────────────────────────────────────────


@bot.command(name='newsaddfeed')
@commands.cooldown(1, 10, commands.BucketType.user)
async def news_add_feed(ctx, *args):
    if not has_moderator_role(ctx):
        logger.info(f"[CMD] denied (no role): {ctx.author} tried {ctx.command} -> {ctx.message.content}")
        return await ctx.send("🚫 You lack permissions.")
    if len(args) < 1:
        return await ctx.send("⚠️ Usage: `!newsaddfeed <name?> <url> [interval]`")

    # to avoid UnboundLocalError
    name = ""
    url = ""
    interval = 5

    # 1 arg: just URL
    if len(args) == 1:
        url = args[0]

    # 2+ args: maybe trailing interval
    else:
        try:
            interval = int(args[-1])
            url = args[-2]
            name = " ".join(args[:-2]).strip()
        except ValueError:
            url = args[-1]
            name = " ".join(args[:-1]).strip()

    if not url.startswith(("http://", "https://")):
        return await ctx.send("⚠️ Need a valid URL (http/https).")

    # Detect provider + set template + default name
    provider = "generic_json"
    tpl = {"embed_color": 0x1ABC9C, "embed": {
        "title": "{title}", "url": "{url}", "description": "{description}",
        "timestamp": "{timestamp}", "fields": [], "auto_fields": True,
        "footer_text": "{timestamp}"}}

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
            name = discord.utils.escape_markdown(
                url.split("//", 1)[1].split("/", 1)[0])
            
    # Normalize URL early so duplicate checks work reliably
    if provider == "rwl_victims_pro":
        url = canonicalize_query_url(url)

    # Prevent duplicates in same channel (match by provider+url+channel)
    if any(f.get('provider') == provider and f['url'] == url and f['channel_id'] == ctx.channel.id for f in feeds):
        return await ctx.send("⚠️ Feed already exists in this channel.")

    # Reset per-feed state key (now includes provider)
    comp = f"{provider}:{url}::{ctx.channel.id}"
    if comp in state:
        state.pop(comp)
        save_json(STATE_FILE, state)

    # Ensure saved URL is canonical for PRO feeds (prevents key drift)
    if provider == "rwl_victims_pro":
        url = canonicalize_query_url(url)

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
    feeds.append(feed)
    save_json(FEEDS_FILE, feeds)

    # Start task + immediate fetch
    start_feed_task(feed)
    await fetch_and_post(feed)

    await ctx.send(f"✅ Added **{name}** every {interval} min (provider: `{provider}`) with color `#{tpl['embed_color']:06X}`.")
    logger.info(
        f"{ctx.author} used newsaddfeed in {ctx.channel} → {name} ({provider})")


@bot.command(name='newsremovefeed')
@commands.cooldown(1, 10, commands.BucketType.user)
async def news_remove_feed(ctx, index: int):
    if not has_moderator_role(ctx):
        return await ctx.send("🚫 You lack permissions.")
    guild_feeds = [f for f in feeds if (bot.get_channel(
        f['channel_id']) or discord.Object(id=0)).guild == ctx.guild]
    if not guild_feeds:
        return await ctx.send("No feeds here.")
    if index < 1 or index > len(guild_feeds):
        return await ctx.send("⚠️ Invalid feed #.")
    feed = guild_feeds[index-1]
    stop_feed_task(feed)
    feeds.remove(feed)
    save_json(FEEDS_FILE, feeds)

    provider = feed.get("provider", "generic_json")
    comp = f"{provider}:{feed['url']}::{feed['channel_id']}"
    if comp in state:
        state.pop(comp)
        save_json(STATE_FILE, state)

    await ctx.send(f"🗑️ Removed **{feed['name']}** (#{index}).")
    logger.info(f"{ctx.author} used newsremovefeed command in {ctx.channel}")

@bot.command(name='newslistfeeds')
async def news_list_feeds(ctx):
    if not has_moderator_role(ctx):
        return await ctx.send("🚫 You lack permissions.")

    guild_feeds = [f for f in feeds if (bot.get_channel(f['channel_id']) or discord.Object(id=0)).guild == ctx.guild]
    if not guild_feeds:
        return await ctx.send("No feeds here.")

    lines = []
    for i, f in enumerate(guild_feeds, 1):
        ch = bot.get_channel(f['channel_id'])
        mention = ch.mention if ch else f"`{f['channel_id']}`"
        prov = f.get('provider', 'generic_json')
        lines.append(
            f"{i}. **{f['name']}** [{prov}] → {mention} every {f['poll_interval_minutes']} min\n<{f['url']}>"
        )

    await ctx.send("**Configured feeds:**\n" + "\n".join(lines))
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
    embed = discord.Embed(title="NewsBot Help",
                          description=help_text, colour=0x1ABC9C)
    embed = await attach_embed_info(ctx, embed)
    await ctx.send(embed=embed)
    logger.info(f"{ctx.author} used newssettings command in {ctx.channel}")


@bot.command(name='newstest')
async def news_test(ctx, index: int):
    guild_feeds = [f for f in feeds if (bot.get_channel(
        f['channel_id']) or discord.Object(id=0)).guild == ctx.guild]
    if not guild_feeds:
        return await ctx.send("No feeds here.")
    if index < 1 or index > len(guild_feeds):
        return await ctx.send("⚠️ Invalid feed #.")
    feed = guild_feeds[index-1]

    provider = feed.get("provider", "generic_json")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        try:
            if provider == "rwl_victims_pro":
                victims = await fetch_rwl_victims_since(session, RWL_COUNTRY, limit=10)
                if not victims:
                    return await ctx.send("No items found.")
                item = normalize_rwl_victim(victims[0])
                return await ctx.send(embed=build_dynamic_embed(feed, item))
            else:
                headers = {'Accept': 'application/json'}
                async with session.get(feed['url'], headers=headers) as resp:
                    data = await resp.json() if resp.status == 200 else None
        except asyncio.TimeoutError:
            return await ctx.send("❌ Request timed out.")

    if provider != "rwl_victims_pro":
        if not data:
            return await ctx.send("❌ Cannot fetch feed.")
        items = data if isinstance(data, list) else data.get('items', [])
        if not items:
            return await ctx.send("No items found.")
        items.sort(key=lambda i: (i.get('published') or ''), reverse=True)
        await ctx.send(embed=build_dynamic_embed(feed, items[0]))


@bot.command(name='newstimer')
async def news_timer(ctx, index: int):
    # List feeds in this guild
    guild_feeds = [f for f in feeds if (bot.get_channel(f['channel_id']) or discord.Object(id=0)).guild == ctx.guild]
    if not guild_feeds:
        return await ctx.send("No feeds here.")
    if index < 1 or index > len(guild_feeds):
        return await ctx.send("⚠️ Invalid feed #.")

    feed = guild_feeds[index - 1]
    provider = feed.get('provider', 'generic_json')
    url = feed['url']

    key_url = canonicalize_query_url(url) if provider == "rwl_victims_pro" else url
    key = (provider, key_url, feed['channel_id'])

    next_time = next_poll_times.get(key)
    if next_time:
        now = datetime.now(CET)
        delta = next_time - now
        total = max(0, int(delta.total_seconds()))
        minutes, seconds = divmod(total, 60)
        await ctx.send(f"⏳ Next poll for **{feed['name']}** in {minutes}m {seconds}s")
    else:
        await ctx.send(f"Timer not started or no info for **{feed['name']}**.")



if __name__ == '__main__':
    bot.run(TOKEN)
