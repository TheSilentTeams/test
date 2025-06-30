import os
import re
import secrets
import aiohttp
import urllib.parse
import asyncio
from datetime import datetime, timedelta
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
    "momerybox.com", "teraboxapp.com","terafileshare.com"
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
cache = {}


def get_peer_type_new(peer_id: int) -> str:
    peer_id_str = str(peer_id)
    if not peer_id_str.startswith("-"):
        return "user"
    elif peer_id_str.startswith("-100"):
        return "channel"
    return "chat"


utils.get_peer_type = get_peer_type_new

# === Verification ===
VERIFICATION_DURATION = timedelta(hours=24)
TOKEN_EXPIRY = timedelta(minutes=10)


def is_verified(user_id):
    now = datetime.utcnow()
    if user_id in cache:
        return now - cache[user_id] < VERIFICATION_DURATION
    record = users.find_one({"user_id": user_id})
    if record and "verified_at" in record:
        verified_at = record["verified_at"]
        cache[user_id] = verified_at
        return now - verified_at < VERIFICATION_DURATION
    return False


def time_left(user_id):
    if user_id in cache:
        verified_at = cache[user_id]
    else:
        record = users.find_one({"user_id": user_id})
        if not record or "verified_at" not in record:
            return None
        verified_at = record["verified_at"]
        cache[user_id] = verified_at
    now = datetime.utcnow()
    delta = now - verified_at
    if delta > VERIFICATION_DURATION:
        return None
    return VERIFICATION_DURATION - delta


async def send_verification_prompt(client, user_id: int, chat_id: int):
    token = secrets.token_urlsafe(16)
    now = datetime.utcnow()
    users.update_one({"user_id": user_id}, {
        "$set": {
            "token": token,
            "token_created": now
        },
        "$unset": {
            "verified_at": ""
        }
    },
                     upsert=True)

    deep_link = f"https://t.me/{client.me.username}?start=verify_{token}"
    encoded_link = urllib.parse.quote(deep_link, safe='')
    short_url = deep_link
    try:
        short_api = f"https://shortner.in/api?api={SHORTNER_API}&url={encoded_link}&format=text"
        async with aiohttp.ClientSession() as session:
            async with session.get(short_api) as resp:
                if resp.status == 200:
                    result = await resp.text()
                    if result.strip().startswith("http"):
                        short_url = result.strip()
    except Exception as e:
        print("Shorten failed:", e)

    text = "🔐 Please verify yourself by clicking below:"
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Verify Now", url=short_url)]])
    await client.send_message(chat_id,
                              text,
                              reply_markup=markup,
                              disable_web_page_preview=True)
    await client.send_message(
        LOG_CHANNEL,
        f"👤 [{chat_id}](tg://user?id={user_id}) requested verification\nLink: {short_url}"
    )


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
             if fs.string and fs.string.startswith(
                 "try {eval(decodeURIComponent") and "%22" in fs.string), None)
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
                results += await crawl_folder(session, jsToken, key,
                                              item.get("path"))
        if not results:
            raise DDLException("❌ No downloadable files found")
        return results


# === Commands ===
@client.on_message(filters.command("start"))
async def handle_start(client, message):
    args = message.text.split()
    name = message.from_user.first_name or message.from_user.username or "there"
    is_first = users.find_one({"user_id": message.from_user.id}) is None

    if is_first:
        await client.send_message(
            LOG_CHANNEL,
            f"👤 New User: [{name}](tg://user?id={message.from_user.id}) `{message.from_user.id}`\nStarted bot."
        )

    if len(args) == 2 and args[1].startswith("verify_"):
        token = args[1].split("verify_")[1]
        now = datetime.utcnow()
        user = users.find_one({"token": token})
        if not user:
            return await message.reply(
                "❌ Invalid or expired verification link.")
        if "token_created" in user and now - user[
                "token_created"] > TOKEN_EXPIRY:
            return await message.reply("❌ Verification link expired.")
        if user["user_id"] != message.from_user.id:
            return await message.reply("❌ This link was not generated for you."
                                       )

        users.update_one({"user_id": user["user_id"]}, {
            "$set": {
                "verified_at": now
            },
            "$unset": {
                "token": "",
                "token_created": ""
            }
        })
        cache[user["user_id"]] = now
        return await message.reply("✅ Verified! You now have access.")

    await client.send_video(
        chat_id=message.chat.id,
        video="https://envs.sh/2OS.mp4",
        caption=(f"👋 **Hello {name}**, I'm your Terabox Direct Download Bot!\n"
                 "📟 Just send me a Terabox link after verifying.\n"
                 "⏳ **Verification:** 24 hours\n\n"
                 " **Download** - Orginal Terabox Download link maybe need vpn\n **Proxy Download** - Slow but No need for vpn\n\n"
                 "⏳ **By: @Silent_Bots** "))


@client.on_message(filters.command("check"))
async def check_verification(client, message):
    user_id = message.from_user.id
    remaining = time_left(user_id)
    if not remaining:
        await message.reply("❌ Not verified or access expired.")
        return await send_verification_prompt(client, user_id, message.chat.id)
    mins = int(remaining.total_seconds() // 60)
    hours = mins // 60
    mins = mins % 60
    await message.reply(f"⏳ Time left: {hours}h {mins}m")


# === Message Handler ===
@client.on_message(
    filters.private
    & ~filters.command(["start", "check", "users", "broadcast", "up"]))
async def handle_any_message(client, message):
    user_id = message.from_user.id
    if not is_verified(user_id):
        return await send_verification_prompt(client, user_id, message.chat.id)
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
                    thumb
                    or "https://via.placeholder.com/500x300?text=No+Thumbnail",
                    caption=text,
                    reply_markup=buttons)
                await client.send_photo(
                    LOG_CHANNEL,
                    thumb
                    or "https://via.placeholder.com/500x300?text=No+Thumbnail",
                    caption=
                    (f"👤 [{message.from_user.first_name}](tg://user?id={message.from_user.id}) `{message.from_user.id}`\nSent: {url}\n{text}"
                     ),
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
    await message.reply(
        f"✅ Broadcast finished!\n\nSent: `{success}`\nFailed: `{failed}`")


@client.on_message(filters.command("up") & filters.user(OWNER_ID))
async def update_cookie(client, message):
    global TERA_COOKIE
    if len(message.command) < 2:
        return await message.reply("❗ Usage: /up <new_cookie>")
    TERA_COOKIE = message.text.split(None, 1)[1].strip()
    await message.reply("✅ Cookie updated successfully.")


print("Bot running...")
client.run()
