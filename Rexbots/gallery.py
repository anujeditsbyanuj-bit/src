import re
import os
import glob
import shutil
from pyrogram import Client, filters, enums
from pyrogram.types import Message

from Rexbots.direct_utils import make_output_folder, upload_file, run_subprocess_with_progress, E_CHECK, E_CROSS, E_INFO

GALLERY_SITES = [
    "twitter.com", "x.com", "pinterest.com", "pixiv.net", "deviantart.com",
    "artstation.com", "flickr.com", "tumblr.com", "reddit.com", "imgur.com",
    "danbooru.donmai.us", "gelbooru.com", "konachan.com", "yande.re",
    "safebooru.org", "zerochan.net", "furaffinity.net", "bsky.app",
]

# Pinterest is deliberately excluded here — ytdl.py already has its own
# Pinterest auto-detect that probes each link first (video pin -> yt-dlp,
# image pin/board -> gallery._handle here). Matching pinterest.com again in
# this file's own auto-detect would double-fire both handlers on one message.
_AUTO_DETECT_SITES = [s for s in GALLERY_SITES if s != "pinterest.com"]
GALLERY_PATTERN = re.compile(
    r"(https?://)?(www\.)?(" + "|".join(re.escape(s) for s in _AUTO_DETECT_SITES) + r")/\S+",
    re.IGNORECASE,
)


def extract_url(text: str):
    text = text.strip()
    if not text.startswith("http"):
        return None
    lower = text.lower()
    return text if any(site in lower for site in GALLERY_SITES) else None


def _gallery_dl_available() -> bool:
    return shutil.which("gallery-dl") is not None


def _make_gallery_line_parser():
    """gallery-dl prints one line per downloaded file by default (its
    destination path). There's no single percentage for a whole gallery
    (total count isn't known upfront), so we report a running file count
    instead of a percentage — still gives live feedback instead of a frozen
    message, in the same visual style as the other downloaders."""
    from Rexbots.direct_utils import E_BOLT, E_CLOCK, fmt_duration
    state = {"count": 0}

    def parse(line: str, elapsed: float):
        if not line or line.startswith(("[", "#")):
            return None  # skip gallery-dl's own log/warning lines
        state["count"] += 1
        return (
            f"<b>{E_BOLT} Downloading gallery...</b>\n\n"
            f"<b>Progress:</b> {state['count']} file(s) downloaded so far\n"
            f"{E_CLOCK} <b>Elapsed:</b> {fmt_duration(elapsed)}"
        )

    return parse


async def _handle(client: Client, message: Message, url: str):
    status = await message.reply_text(f"<b>{E_INFO} Gallery link detected...</b>", parse_mode=enums.ParseMode.HTML)

    if not _gallery_dl_available():
        return await status.edit_text(
            f"<b>{E_CROSS} 'gallery-dl' is not installed.</b>\n"
            f"<i>Install it first: <code>pip install gallery-dl</code></i>",
            parse_mode=enums.ParseMode.HTML
        )

    base = make_output_folder("gallery")
    gallery_dir = os.path.join(base, f"g_{message.id}")
    os.makedirs(gallery_dir, exist_ok=True)

    await status.edit_text(f"<b>{E_INFO} Downloading gallery...</b>", parse_mode=enums.ParseMode.HTML)

    cmd = ["gallery-dl", "--directory", gallery_dir, "--no-mtime", url]
    returncode, tail = await run_subprocess_with_progress(
        cmd, status, "Downloading gallery", _make_gallery_line_parser(), interval=3.0
    )

    if returncode != 0:
        err = tail[:300] or f"gallery-dl exited with code {returncode}"
        return await status.edit_text(f"<b>{E_CROSS} Gallery download failed:</b>\n<code>{err}</code>", parse_mode=enums.ParseMode.HTML)

    exts = ("*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.mp4", "*.webm", "*.mkv")
    files = []
    for ext in exts:
        files.extend(glob.glob(os.path.join(gallery_dir, "**", ext), recursive=True))
    files.sort()

    if not files:
        return await status.edit_text(f"<b>{E_CROSS} No media found at this link.</b>", parse_mode=enums.ParseMode.HTML)

    total = len(files)
    for i, path in enumerate(files, 1):
        fname = os.path.basename(path)
        await upload_file(client, message, path, status, f"<b>{E_CHECK} Gallery ({i}/{total})</b>\n<code>{fname}</code>")
        if i < total:
            status = await message.reply_text(f"<b>{E_INFO} Uploading {i + 1}/{total}...</b>", parse_mode=enums.ParseMode.HTML)

    shutil.rmtree(gallery_dir, ignore_errors=True)


def _extract_auto_url(text: str):
    m = GALLERY_PATTERN.search(text)
    return m.group(0) if m else None


# Bare gallery-site link (Twitter/X, Reddit, Tumblr, Pixiv, DeviantArt, etc.)
# pasted with no /gallery command. Same pattern as the other auto-detect
# handlers (mega.py, gdrive.py, terabox.py, ...). Pinterest excluded — see
# note on GALLERY_PATTERN above.
@Client.on_message(
    filters.text & filters.private & filters.regex(GALLERY_PATTERN) & ~filters.regex(r"^/"),
    group=1,
)
async def gallery_auto_detect(client: Client, message: Message):
    url = _extract_auto_url(message.text)
    if url:
        await _handle(client, message, url)


@Client.on_message(filters.command("gallery") & filters.private)
async def gallery_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/gallery &lt;twitter/pinterest/reddit/... URL&gt;</code>\n"
            f"<i>Auto-detection is off for this one to avoid clashing with normal chat links — "
            f"use the command directly.</i>",
            parse_mode=enums.ParseMode.HTML
        )
    url = extract_url(message.command[1]) or message.command[1]
    await _handle(client, message, url)
