import re
import aiohttp
from pyrogram import Client, filters, enums
from pyrogram.types import Message

from Rexbots.direct_utils import (
    make_output_folder, safe_filename, stream_download, upload_file,
    E_CHECK, E_CROSS, E_INFO
)

PATTERN = re.compile(
    r"(https?://)?(www\.)?(terabox\.com|1024terabox\.com|teraboxapp\.com|freeterabox\.com|nephobox\.com|4funbox\.com)/\S+",
    re.IGNORECASE
)

# Third-party extraction API — unofficial, may change/break at any time.
_TERABOX_API = "https://ytshorts.savetube.me/api/v1/terabox-downloader"


def extract_url(text: str):
    m = PATTERN.search(text)
    return m.group(0) if m else None


async def _fetch_direct_url(link: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(_TERABOX_API, data={"url": link}, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()

    if not data.get("response"):
        raise ValueError("Empty response from TeraBox API — link may be private or invalid.")

    try:
        item = data["response"][0]
        resolutions = item["resolutions"]
        filename = safe_filename(item.get("title") or item.get("name"), "terabox_file")
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"Unexpected TeraBox API format: {e}")

    fast_url = resolutions.get("Fast Download", "")
    slow_url = resolutions.get("HD Video", "")
    direct_url = fast_url or slow_url
    if not direct_url:
        raise ValueError("No download URL found for this TeraBox link.")

    return direct_url, filename


async def _handle(client: Client, message: Message, url: str):
    status = await message.reply_text(f"<b>{E_INFO} TeraBox link detected — extracting...</b>", parse_mode=enums.ParseMode.HTML)
    try:
        direct_url, filename = await _fetch_direct_url(url)
        folder = make_output_folder("terabox")
        dest = f"{folder}/{message.id}_{filename}"
        await stream_download(direct_url, dest, status, "Downloading from TeraBox")
        await upload_file(client, message, dest, status, f"<b>{E_CHECK} TeraBox File</b>\n<code>{filename}</code>")
    except Exception as e:
        await status.edit_text(f"<b>{E_CROSS} Error:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.text & filters.private & filters.regex(PATTERN), group=1)
async def terabox_auto_detect(client: Client, message: Message):
    url = extract_url(message.text)
    if url:
        await _handle(client, message, url)


@Client.on_message(filters.command("terabox") & filters.private)
async def terabox_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/terabox &lt;terabox URL&gt;</code>",
            parse_mode=enums.ParseMode.HTML
        )
    url = extract_url(message.command[1]) or message.command[1]
    await _handle(client, message, url)
