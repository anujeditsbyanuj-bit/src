import re
import aiohttp
from pyrogram import Client, filters, enums
from pyrogram.types import Message

from Rexbots.direct_utils import (
    make_output_folder, safe_filename, stream_download, upload_file,
    DEFAULT_HEADERS, E_CHECK, E_CROSS, E_INFO
)

PATTERN = re.compile(r"(https?://)?(www\.)?(streamtape\.\w+|stape\.\w+)/\S+", re.IGNORECASE)


def extract_url(text: str):
    m = PATTERN.search(text)
    return m.group(0) if m else None


async def _extract_direct_url(link: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(link, headers=DEFAULT_HEADERS) as resp:
            html = await resp.text()

    url = None
    m = re.search(r"getElementById\('norobotlink'\)\.href\s*=\s*['\"]([^'\"]+)", html)
    if m:
        url = m.group(1)
        if url.startswith("//"):
            url = "https:" + url

    if not url:
        raise ValueError("Could not extract StreamTape direct link. Video may be removed.")

    title_m = re.search(r'<title>([^<]+)</title>', html)
    filename = title_m.group(1).strip() if title_m else "streamtape_video"
    filename = re.sub(r'[^\w\s\-.]', '', filename).strip()
    if not filename.lower().endswith(('.mp4', '.mkv', '.avi', '.webm')):
        filename += '.mp4'
    return url, filename


async def _handle(client: Client, message: Message, url: str):
    status = await message.reply_text(f"<b>{E_INFO} StreamTape link detected...</b>", parse_mode=enums.ParseMode.HTML)
    try:
        direct_url, filename = await _extract_direct_url(url)
        filename = safe_filename(filename, "streamtape_video.mp4")
        folder = make_output_folder("streamtape")
        dest = f"{folder}/{message.id}_{filename}"
        await stream_download(direct_url, dest, status, "Downloading from StreamTape")
        await upload_file(client, message, dest, status, f"<b>{E_CHECK} StreamTape Video</b>\n<code>{filename}</code>")
    except Exception as e:
        await status.edit_text(f"<b>{E_CROSS} Error:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.text & filters.private & filters.regex(PATTERN), group=1)
async def streamtape_auto_detect(client: Client, message: Message):
    url = extract_url(message.text)
    if url:
        await _handle(client, message, url)


@Client.on_message(filters.command("stape") & filters.private)
async def streamtape_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/stape &lt;streamtape URL&gt;</code>",
            parse_mode=enums.ParseMode.HTML
        )
    url = extract_url(message.command[1]) or message.command[1]
    await _handle(client, message, url)
