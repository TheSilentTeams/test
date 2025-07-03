import os
import re
import aiohttp
import urllib.parse
import asyncio
from pymongo import MongoClient
from pyrogram import Client, filters, utils
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import quote_plus
from bs4 import BeautifulSoup

# === Config ===
api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")
bot_token = os.getenv("BOT_TOKEN")
mongo_url = os.getenv("MONGO_URL")
SHORTNER_API = os.getenv("SHORTNER_API")
TERA_COOKIE = os.getenv("TERA_COOKIE")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL"))
OWNER_ID = int(os.getenv("OWNER_ID"))

TERABOX_DOMAINS = [
    "terabox.com", "terabox.app", "1024tera.com", "terasharelink.com",
    "nephobox.com", "1024terabox.com", "4funbox.com", "mirrobox.com",
    "momerybox.com", "teraboxapp.com", "terafileshare.com"
]
domain_pattern = "|".join(re.escape(domain) for domain in TERABOX_DOMAINS)
url_pattern = re.compile(
    rf'https?://(?:www\.)?(?:{domain_pattern})(?:/s/\S+|/sharing/link\?surl=\S+)',
    re.IGNORECASE)

# === DB ===
client = Client("verif_bot", api_id, api_hash, bot_token=bot_token)
mongo = MongoClient(mongo_url)
db = mongo["verifybot"]
users = db["verified_users"]

def get_peer_type_new(peer_id: int) -> str:
    peer_id_str = str(peer_id)
    if not peer_id_str.startswith("-"):
        return "user"
    elif peer_id_str.startswith("-100"):
        return "channel"
    return "chat"

utils.get_peer_type = get_peer_type_new

# === TeraBox Downloader ===
class DDLException(Exception):
    pass

def normalize_link(link: str) -> str:
    parsed = urllib.parse.urlparse(link)
    if parsed.path.startswith("/s/"):
        return f"https://www.terabox.com{parsed.path}"
    elif parsed.path == "/sharing/link":
        query = urllib.parse.parse_qs(parsed.query)
        surl = query.get("surl", [None])[0]
        if surl:
            return f"https://www.terabox.com/s/{surl}"
    raise DDLException("❌ Invalid link: could not extract 'surl'")

def extract_key_and_path(url):
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    key = qs.get("surl", [None])[0]
    if not key and "/s/" in parsed.path:
        key = parsed.path.split("/s/")[-1]
    path = urllib.parse.unquote(qs.get("path", ["/"])[0])
    return key, path

async def fetch(session, url):
    for _ in range(5):
        try:
            async with session.get(url) as response:
                return await response.text(), str(response.url)
        except Exception:
            await asyncio.sleep(1)
    raise DDLException(f"Failed to fetch {url}")

async def fetch_json(session, url):
    for _ in range(5):
        try:
            async with session.get(url) as response:
                return await response.json()
        except Exception:
            await asyncio.sleep(1)
    raise DDLException(f"Failed to fetch JSON from {url}")

async def crawl_folder(session, jsToken, key, dir_path):
    links = []
    url = f"https://www.terabox.app/share/list?app_id=250528&jsToken={jsToken}&shorturl={key}&dir={dir_path}"
    data = await fetch_json(session, url)
    for item in data.get("list", []):
        if str(item.get("isdir")) == "0":
            dlink = item.get("dlink", "").replace(".com", ".app")
            name = item.get("server_filename", "Unknown")
            size = int(item.get("size", 0)) / (1024**2)
            thumb = item.get("thumbs", {}).get("url3")
            links.append((dlink, name, f"{size:.2f} MB", thumb))
        else:
            sub_path = item.get("path")
            links += await crawl_folder(session, jsToken, key, sub_path)
    return links

async def terabox(url: str):
    url = normalize_link(url)
    headers = {"Cookie": f"ndus={TERA_COOKIE}", "User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        _, final_url = await fetch(session, url)
        key, _ = extract_key_and_path(final_url)
        html, _ = await fetch(
            session, f"https://www.terabox.app/wap/share/filelist?surl={key}")
        soup = BeautifulSoup(html, "lxml")
        jsToken = next(
            (fs.string.split("%22")[1] for fs in soup.find_all("script")
             if fs.string and fs.string.startswith("try {eval(decodeURIComponent") and "%22" in fs.string), None)
        if not jsToken:
            raise DDLException("jsToken not found in page")
        root_data = await fetch_json(
            session,
            f"https://www.terabox.app/share/list?app_id=250528&jsToken={jsToken}&shorturl={key}&root=1"
        )
        if root_data.get("errno") != 0:
            raise DDLException("API error, check cookie or token")
        items = root_data.get("list", [])
        if len(items) == 1 and str(items[0].get("isdir")) == "0":
            item = items[0]
            dlink = item.get("dlink", "").replace(".com", ".app")
            name = item.get("server_filename", "Unknown")
            size = int(item.get("size", 0)) / (1024**2)
            thumb = item.get("thumbs", {}).get("url3")
            return [(dlink, name, f"{size:.2f} MB", thumb)]
        results = []
        for item in items:
            if str(item.get("isdir")) == "1":
                results += await crawl_folder(session, jsToken, key, item.get("path"))
        if not results:
            raise DDLException("❌ No downloadable files found")
        return results

# === Commands ===
@client.on_message(filters.command("start"))
async def handle_start(client, message):
    name = message.from_user.first_name or message.from_user.username or "there"
    await client.send_video(
        chat_id=message.chat.id,
        video="https://envs.sh/2OS.mp4",
        caption=(f"👋 **Hello {name}**, I'm your Terabox Direct Download Bot!\n"
                 "📟 Just send me a Terabox link.\n\n"
                 " **Download** - Orginal Terabox Download link maybe need vpn\n"
                 " **Proxy Download** - Slow but No need for vpn\n\n"
                 "⏳ **By: @Silent_Bots** "))

# === Message Handler ===
@client.on_message(filters.private & ~filters.command(["start", "users", "broadcast", "up"]))
async def handle_any_message(client, message):
    matches = url_pattern.findall(message.text or "")
    if not matches:
        return await message.reply("❌ No valid TeraBox link found.")
    for url in matches:
        msg = await message.reply("🔍 Extracting direct download link...")
        try:
            files = await terabox(url)
            for dlink, name, size, thumb in files:
                text = f"\n✅ **File:** {name}\n📦 **Size:** {size}\n"
                fixed_dlink = dlink.replace(".app", ".com")
                proxy_link = f"https://thesilentteams.shivcollegelife.workers.dev/?url={quote_plus(fixed_dlink)}"
                buttons = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("⬇️ Direct Link", url=dlink),
                        InlineKeyboardButton("⚡ Proxy Download", url=proxy_link)
                    ]
                ])
                await client.send_photo(
                    message.chat.id,
                    thumb or "https://via.placeholder.com/500x300?text=No+Thumbnail",
                    caption=text,
                    reply_markup=buttons)
                await client.send_photo(
                    LOG_CHANNEL,
                    thumb or "https://via.placeholder.com/500x300?text=No+Thumbnail",
                    caption=(f"👤 [{message.from_user.first_name}](tg://user?id={message.from_user.id}) `{message.from_user.id}`\nSent: {url}\n{text}"),
                    reply_markup=buttons)
            await msg.delete()
        except Exception as e:
            await msg.edit(f"❌ Error: {str(e)}")

@client.on_message(filters.command("users") & filters.user(OWNER_ID))
async def handle_users(client, message):
    total = users.count_documents({"verified_at": {"$exists": True}})
    await message.reply(f"👥 Total Verified Users: `{total}`")

@client.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_handler(client, message):
    if len(message.command) < 2:
        return await message.reply("❗ Usage: /broadcast <message>")
    text = message.text.split(None, 1)[1]
    cursor = users.find({"verified_at": {"$exists": True}})
    success = failed = 0
    for user in cursor:
        try:
            await client.send_message(user["user_id"], text)
            success += 1
        except:
            failed += 1
    await message.reply(f"✅ Broadcast finished!\n\nSent: `{success}`\nFailed: `{failed}`")

@client.on_message(filters.command("up") & filters.user(OWNER_ID))
async def update_cookie(client, message):
    global TERA_COOKIE
    if len(message.command) < 2:
        return await message.reply("❗ Usage: /up <new_cookie>")
    TERA_COOKIE = message.text.split(None, 1)[1].strip()
    await message.reply("✅ Cookie updated successfully.")

print("Bot running...")
client.run()
