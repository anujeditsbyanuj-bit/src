import os
import re
import asyncio
import aiohttp
from pyrogram import Client, filters, enums
from pyrogram.types import Message

from Rexbots.direct_utils import (
    make_output_folder, safe_filename, stream_download, upload_file,
    E_CHECK, E_CROSS, E_INFO
)

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# NOTE: Supports public ("Anyone with the link") single FILES only.
# Private files or folders need OAuth + Drive API (google-api-python-client),
# which is a separate setup — ask if that's needed.
PATTERN = re.compile(r"(https?://)?(www\.)?drive\.google\.com/\S+", re.IGNORECASE)

ID_PATTERN = re.compile(r"/d/([-\w]+)|[?&]id=([-\w]+)")


def extract_url(text: str):
    m = PATTERN.search(text)
    return m.group(0) if m else None


def _extract_id(link: str):
    m = ID_PATTERN.search(link)
    if not m:
        return None
    return m.group(1) or m.group(2)


# ---------------------------------------------------------------------------
# Primary method: yt-dlp's own Google Drive extractor.
# Google has changed the manual "uc?export=download&confirm=..." dance
# several times (virus-scan warning page, different confirm-token markup for
# large files, etc). Rather than re-scraping that HTML ourselves, hand the
# whole thing to yt-dlp — its GoogleDrive extractor is actively maintained
# against exactly these changes, so it's the more durable option.
# ---------------------------------------------------------------------------

def _ytdlp_download(url: str, out_dir: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": os.path.join(out_dir, "%(title).100s.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        return path, info


async def _try_ytdlp(client: Client, message: Message, status, url: str):
    if yt_dlp is None:
        return False
    await status.edit_text(f"<b>{E_INFO} Fetching from Google Drive...</b>", parse_mode=enums.ParseMode.HTML)
    folder = make_output_folder("gdrive")
    try:
        path, info = await asyncio.to_thread(_ytdlp_download, url, folder)
    except Exception:
        return False  # fall through to the manual scrape method below

    if not path or not os.path.exists(path):
        return False

    filename = safe_filename(os.path.basename(path), "gdrive_file")
    await upload_file(client, message, path, status, f"<b>{E_CHECK} Google Drive File</b>\n<code>{filename}</code>")
    return True


# ---------------------------------------------------------------------------
# Fallback method: manual scrape of the uc?export=download confirm page.
# Kept as a backup for whatever yt-dlp's extractor doesn't (yet) handle.
# ---------------------------------------------------------------------------

async def _resolve_direct_url(file_id: str):
    base = "https://drive.google.com/uc?export=download"
    filename = None
    async with aiohttp.ClientSession() as session:
        async with session.get(base, params={"id": file_id}) as resp:
            html = await resp.text()
            name_m = re.search(r'"(?:filename|title)"\s*:\s*"([^"]+)"', html) or re.search(r'<span[^>]*id="download-title"[^>]*>([^<]+)</span>', html)
            if name_m:
                filename = name_m.group(1)
            token_m = re.search(r'confirm=([0-9A-Za-z_-]+)', html) or re.search(r'name="confirm"\s+value="([^"]+)"', html)
            if token_m:
                return f"{base}&confirm={token_m.group(1)}&id={file_id}", filename
    return f"{base}&id={file_id}", filename


async def _fallback_manual(client: Client, message: Message, status, file_id: str):
    direct_url, drive_filename = await _resolve_direct_url(file_id)
    filename = safe_filename(drive_filename, f"gdrive_{file_id}")
    folder = make_output_folder("gdrive")
    dest = f"{folder}/{message.id}_{filename}"
    await status.edit_text(f"<b>{E_INFO} Google Drive link detected (fallback method)...</b>", parse_mode=enums.ParseMode.HTML)
    await stream_download(direct_url, dest, status, "Downloading from Google Drive")
    await upload_file(client, message, dest, status, f"<b>{E_CHECK} Google Drive File</b>\n<code>{filename}</code>")


async def _handle(client: Client, message: Message, url: str):
    status = await message.reply_text(f"<b>{E_INFO} Google Drive link detected...</b>", parse_mode=enums.ParseMode.HTML)
    file_id = _extract_id(url)
    if not file_id:
        return await status.edit_text(f"<b>{E_CROSS} Could not find a file ID in this link.</b>", parse_mode=enums.ParseMode.HTML)

    if "/folders/" in url:
        return await status.edit_text(
            f"<b>{E_CROSS} Folder links aren't supported yet.</b>\n"
            f"<i>Only single public files work right now — folders need Google Drive API setup.</i>",
            parse_mode=enums.ParseMode.HTML
        )

    try:
        if await _try_ytdlp(client, message, status, url):
            return
        await _fallback_manual(client, message, status, file_id)
    except Exception as e:
        await status.edit_text(
            f"<b>{E_CROSS} Error:</b>\n<code>{e}</code>\n"
            f"<i>File may be private — only 'Anyone with the link' files work without API setup.</i>",
            parse_mode=enums.ParseMode.HTML
        )


@Client.on_message(filters.text & filters.private & filters.regex(PATTERN), group=1)
async def gdrive_auto_detect(client: Client, message: Message):
    url = extract_url(message.text)
    if url:
        await _handle(client, message, url)


@Client.on_message(filters.command("gdrive") & filters.private)
async def gdrive_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/gdrive &lt;drive.google.com URL&gt;</code>",
            parse_mode=enums.ParseMode.HTML
        )
    url = extract_url(message.command[1]) or message.command[1]
    await _handle(client, message, url)
