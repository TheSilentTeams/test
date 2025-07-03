import os
import re
import aiohttp
import asyncio
from datetime import datetime
from pymongo import MongoClient
from pyrogram import Client, filters, utils
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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
    "momerybox.com", "teraboxapp.com"
]
domain_pattern = "|".join(re.escape(domain) for domain in TERABOX_DOMAINS)
url_pattern = re.compile(rf'https?://(?:www\.)?(?:{domain_pattern})/s/\S+', re.IGNORECASE)

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

# === Verification Skipped ===
def is_verified(user_id):
    return True

def time_left(user_id):
    return None

# === TeraBox Downloader ===
class DDLException(Exception):
    pass

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

async def terabox(url: str):
    headers = {"Cookie": f"ndus={TERA_COOKIE}", "User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        _, final_url = await fetch(session, url)
        key = final_url.split("?surl=")[-1]
        html, _ = await fetch(session, f"http://www.terabox.com/wap/share/filelist?surl={key}")
        soup = BeautifulSoup(html, "lxml")
        jsToken = next((fs.string.split("%22")[1] for fs in soup.find_all("script")
                        if fs.string and fs.string.startswith("try {eval(decodeURIComponent") and "%22" in fs.string), None)
        if not jsToken:
            raise DDLException("jsToken not found in page")
        result = await fetch_json(session, f"https://www.terabox.com/share/list?app_id=250528&jsToken={jsToken}&shorturl={key}&root=1")
        if result["errno"] != 0:
            raise DDLException(f"{result['errmsg']} - Check cookie")
        items = result.get("list", [])
        if len(items) != 1:
            raise DDLException("Only one file allowed, or none found")
        item = items[0]
        if item.get("isdir") != "0":
            raise DDLException("Folders are not supported")
        dlink = item.get("dlink", "").replace(".com", ".app")
        name = item.get("server_filename", "Unknown")
        size = int(item.get("size", 0)) / (1024**2)
        size_str = f"{size:.2f} MB"
        thumb = item.get("thumbs", {}).get("url3")
        return dlink, name, size_str, thumb

# === Commands ===
@client.on_message(filters.command("start"))
async def handle_start(client, message):
    name = message.from_user.first_name or message.from_user.username or "there"
    is_first = users.find_one({"user_id": message.from_user.id}) is None

    if is_first:
        users.insert_one({"user_id": message.from_user.id})
        await client.send_message(LOG_CHANNEL, f"👤 New User: [{name}](tg://user?id={message.from_user.id}) `{message.from_user.id}`\nStarted bot.")

    await client.send_video(
        chat_id=message.chat.id,
        video="https://envs.sh/2OS.mp4",
        caption=(
            f"👋 **Hello {name}**, I'm your Terabox Direct Download Bot!\n"
            "📟 Just send me a Terabox link.\n\n"
            "✅ **Verification Skipped in Dev Mode**\n"
            "📁 File Links only supported.\n\n"
            "⏳ **By: @Silent_Bots** ")
    )

@client.on_message(filters.command("check"))
async def check_verification(client, message):
    await message.reply("✅ Verification skipped. You're good to go!")

@client.on_message(filters.private & ~filters.command(["start", "check", "users", "broadcast", "up"]))
async def handle_any_message(client, message):
    matches = url_pattern.findall(message.text or "")
    if not matches:
        return await message.reply("❌ No valid TeraBox link found.")
    for url in matches:
        msg = await message.reply("🔍 Extracting direct download link...")
        try:
            dlink, name, size, thumb = await terabox(url)
            text = f"\n✅ **File:** {name}\n📦 **Size:** {size}\n"
            buttons = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ Download ⬇️", url=dlink)]])
            await client.send_photo(message.chat.id, thumb or "https://via.placeholder.com/500x300?text=No+Thumbnail", caption=text, reply_markup=buttons)
            await client.send_photo(LOG_CHANNEL, thumb or "https://via.placeholder.com/500x300?text=No+Thumbnail", caption=(f"👤 [{message.from_user.first_name}](tg://user?id={message.from_user.id}) `{message.from_user.id}`\nSent: {url}\n{text}"), reply_markup=buttons)
            await msg.delete()
        except Exception as e:
            await msg.edit(f"❌ Error: {str(e)}")

@client.on_message(filters.command("users") & filters.user(OWNER_ID))
async def handle_users(client, message):
    total = users.count_documents({})
    await message.reply(f"👥 Total Users: `{total}`")

@client.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_handler(client, message):
    if len(message.command) < 2:
        return await message.reply("❗ Usage: /broadcast <message>")
    text = message.text.split(None, 1)[1]
    cursor = users.find({})
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
