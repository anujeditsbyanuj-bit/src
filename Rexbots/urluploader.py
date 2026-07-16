# Generic URL Uploader
# Ported from Url-uploader-Bot-V4 (Plugin/echo.py + Plugin/dl_button.py), rewritten
# to fit this bot's plugin style and reuse Rexbots/direct_utils.py.
#
# Every other downloader plugin in this bot (catbox, gofile, pixeldrain, mediafire,
# streamtape, terabox, mega, gdrive, ytdl's yt-dlp fallback, etc.) already claims
# its own domains. This plugin is the LAST-RESORT fallback: any bare http(s) link
# that isn't one of those known hosts, and that yt-dlp itself doesn't recognise as
# a media site, gets treated as a plain direct-download link — downloaded as-is
# and uploaded to Telegram. This is the one capability the original bot didn't
# have (it could only pull from Telegram channels / specific hosts, not from an
# arbitrary raw file URL).

import re
from pyrogram import Client, filters, enums
from pyrogram.types import Message

from Rexbots.direct_utils import (
    make_output_folder, safe_filename, stream_download, upload_file,
    E_CHECK, E_CROSS, E_INFO
)

GENERIC_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

# Anything already owned by another plugin (or that shouldn't be treated as a
# raw file, like t.me links which are handled by start.py's save handler).
_EXCLUDED_DOMAINS = (
    "t.me", "telegram.me",
    "youtube.com", "youtu.be", "instagram.com", "instagr.am",
    "pinterest.", "pin.it",
    "facebook.com", "fb.watch", "fb.com",
    "mega.nz", "drive.google.com", "gofile.io", "mediafire.com",
    "pixeldrain.com", "streamtape.", "stape.", "catbox.moe",
    "terabox.com", "1024terabox.com", "teraboxapp.com", "freeterabox.com",
    "nephobox.com", "4funbox.com", "magnet:", ".torrent",
    "twitter.com", "x.com", "pixiv.net", "deviantart.com", "artstation.com",
    "flickr.com", "tumblr.com", "reddit.com", "imgur.com",
    "danbooru.donmai.us", "gelbooru.com", "konachan.com", "yande.re",
    "safebooru.org", "zerochan.net", "furaffinity.net", "bsky.app",
    "mxplayer.in", "mxplay.com",
    "fembed.com", "fembed-hd.com", "femax20.com", "vanfem.com", "suzihaza.com",
    "embedsito.com", "owodeuwu.xyz", "plusto.link", "watchse.icu", "feurl.com",
)


def extract_url(text: str):
    m = GENERIC_URL_PATTERN.search(text)
    if not m:
        return None
    url = m.group(0)
    lower = url.lower()
    return None if any(d in lower for d in _EXCLUDED_DOMAINS) else url


async def _handle(client: Client, message: Message, url: str):
    status = await message.reply_text(
        f"<b>{E_INFO} Link detected, downloading...</b>", parse_mode=enums.ParseMode.HTML
    )
    folder = make_output_folder("urluploader")
    filename = safe_filename(url.split("/")[-1].split("?")[0], "downloaded_file")
    dest = f"{folder}/{message.id}_{filename}"
    try:
        await stream_download(url, dest, status, "Downloading File")
        await upload_file(
            client, message, dest, status,
            f"<b>{E_CHECK} Uploaded</b>\n<code>{filename}</code>"
        )
    except Exception as e:
        await status.edit_text(f"<b>{E_CROSS} Error:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)


@Client.on_message(
    filters.text & filters.private & filters.regex(GENERIC_URL_PATTERN) & ~filters.regex(r"^/"),
    group=3,  # last resort: after specific-host handlers (group=1) and yt-dlp's own generic fallback (group=2)
)
async def generic_url_auto_detect(client: Client, message: Message):
    url = extract_url(message.text)
    if not url:
        return

    # If yt-dlp itself recognises this as a media page, ytdl.py's group=2
    # handler already offered a quality picker for it — don't double-handle.
    try:
        from Rexbots.ytdl import has_quality_formats
        if await has_quality_formats(url):
            return
    except Exception:
        pass

    await _handle(client, message, url)


@Client.on_message(filters.command(["url", "direct"]) & filters.private)
async def url_upload_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/url &lt;direct download link&gt;</code>\n"
            f"<i>Downloads any direct link and uploads it to Telegram.</i>",
            parse_mode=enums.ParseMode.HTML
        )
    raw = message.text.split(None, 1)[1].strip()
    url = extract_url(raw) or raw
    await _handle(client, message, url)
