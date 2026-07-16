import os
import re
import time
import uuid
import shutil
import asyncio
import requests
from pyrogram import Client, filters, enums
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import YTDL_MAX_FILESIZE, YT_COOKIES, INSTA_COOKIES, FB_COOKIES
from Rexbots.direct_utils import make_upload_progress, format_media_caption, format_progress

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

E_ROCKET = '<emoji id=5456140674028019486>🚀</emoji>'
E_CROSS  = '<emoji id=5210952531676504517>❌</emoji>'
E_CHECK  = '<emoji id=5206607081334906820>✔️</emoji>'
E_BOLT   = '<emoji id=5456140674028019486>⚡️</emoji>'

DOWNLOAD_DIR = "yt_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# session_id -> {"url": str, "title": str, "thumbnail": str, "chat_id": int, "reply_to": int}
_SESSIONS = {}

INSTA_PATTERN = re.compile(r"(https?://)?(www\.)?(instagram\.com|instagr\.am)/\S+", re.IGNORECASE)
PINTEREST_PATTERN = re.compile(r"(https?://)?(www\.)?(pinterest\.[a-z.]+|pin\.it)/\S+", re.IGNORECASE)
YOUTUBE_PATTERN = re.compile(
    r"(https?://)?(www\.|m\.)?(youtube\.com/(watch|shorts|live)\S+|youtu\.be/\S+)",
    re.IGNORECASE,
)
PLAYLIST_REGEX = re.compile(r'(.*)youtube\.com/(.*)[&|?]list=(?P<playlist>[^&]*)(.*)', re.IGNORECASE)

# Generic bare-link fallback (any other yt-dlp-supported site: Twitch, TikTok,
# Vimeo, SoundCloud, Dailymotion, X/Twitter video, etc). Domains already
# owned by a more specific handler are excluded so a link isn't processed
# twice.
GENERIC_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_EXCLUDED_DOMAINS = (
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
)


def _cookies_for(url: str):
    if "instagram.com" in url and INSTA_COOKIES and os.path.exists(INSTA_COOKIES):
        return INSTA_COOKIES
    if ("facebook.com" in url or "fb.watch" in url) and FB_COOKIES and os.path.exists(FB_COOKIES):
        return FB_COOKIES
    if YT_COOKIES and os.path.exists(YT_COOKIES):
        return YT_COOKIES
    return None


def _base_opts(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    cookies = _cookies_for(url)
    if cookies:
        opts["cookiefile"] = cookies
    if "instagram.com" in url or "instagr.am" in url:
        # Instagram's CDN occasionally sends a gzip-labeled response that
        # isn't valid gzip (usually a truncated rate-limit/error response),
        # which surfaces as "Error reading response: ... content-encoding:
        # gzip, but failed to decode it ... inconsistent stream state".
        # Asking the server not to compress the response at all sidesteps
        # the broken-decode path entirely.
        opts["http_headers"] = {"Accept-Encoding": "identity"}
    return opts


# Errors known to be transient/network-level rather than "this video is
# really unavailable" - worth one retry with a fresh YoutubeDL instance
# before giving up.
_TRANSIENT_ERROR_MARKERS = (
    "content-encoding: gzip, but failed to decode it",
    "inconsistent stream state",
    "the page needs to be reloaded",
)


def _is_transient_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_ERROR_MARKERS)


def _fmt_size(n):
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f" ({n:.1f}{unit})"
        n /= 1024
    return f" ({n:.1f}TB)"


def _fmt_duration(seconds):
    if not seconds:
        return "Unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _extract_info(url: str) -> dict:
    """Fetch metadata + available formats WITHOUT downloading."""
    last_err = None
    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL({**_base_opts(url), "skip_download": True}) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            last_err = e
            if attempt < 2 and _is_transient_error(e):
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise last_err


def _pick_qualities(info: dict):
    """Reduce yt-dlp's raw format list to one best option per resolution tier.

    Generic HTML5/JW-player pages (yt-dlp's "Generic" extractor — the case for
    most random video-hosting sites that aren't a named extractor) usually
    expose a direct file URL with NO height/resolution metadata at all. Those
    formats used to be silently dropped here, which made has_quality_formats()
    return False and let the link fall through to urluploader.py's raw-file
    fallback — downloading the HTML page itself instead of the actual video.
    So: keep the per-resolution logic for sites that do report heights, but
    fall back to the best available formatless option (or the top-level
    info dict itself) when nothing has a height.
    """
    formats = info.get("formats") or []
    by_height = {}
    no_height_best = None

    for f in formats:
        height = f.get("height") or 0
        has_video = f.get("vcodec") not in (None, "none")
        has_audio = f.get("acodec") not in (None, "none")
        if not has_video:
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        score = (2 if has_audio else 1, size)
        if not height:
            if not no_height_best or score > no_height_best["_score"]:
                no_height_best = {
                    "format_id": f["format_id"], "height": 0, "ext": f.get("ext", "mp4"),
                    "filesize": size, "_score": score,
                }
            continue
        current = by_height.get(height)
        if not current or score > current["_score"]:
            by_height[height] = {
                "format_id": f["format_id"], "height": height, "ext": f.get("ext", "mp4"),
                "filesize": size, "_score": score,
            }

    tiers = sorted(by_height.values(), key=lambda x: x["height"], reverse=True)

    if not tiers:
        if no_height_best:
            tiers = [no_height_best]
        elif info.get("url") or info.get("format_id"):
            # yt-dlp didn't even list a "formats" array (single-format
            # generic result) — build one synthetic tier from the top level.
            tiers = [{
                "format_id": info.get("format_id") or "best",
                "height": 0,
                "ext": info.get("ext") or "mp4",
                "filesize": info.get("filesize") or info.get("filesize_approx") or 0,
            }]

    return tiers  # show every distinct resolution available, no cap


def _download_selected(url: str, out_dir: str, format_id, audio_only: bool, height=None, progress_hook=None):
    opts = {
        **_base_opts(url),
        "outtmpl": os.path.join(out_dir, "%(title).70s.%(ext)s"),
        "max_filesize": YTDL_MAX_FILESIZE,
    }
    if progress_hook is not None:
        opts["progress_hooks"] = [progress_hook]

    def _run(fmt):
        opts["format"] = fmt
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            if audio_only:
                path = os.path.splitext(path)[0] + ".mp3"
            else:
                path = os.path.splitext(path)[0] + "." + (info.get("ext") or "mp4")
            return path, info

    if audio_only:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        return _run("bestaudio/best")

    opts["merge_output_format"] = "mp4"
    height_cap = f"[height<={height}]" if height else ""
    # Formats can go stale between building the quality menu and the tap
    # (itags rotate/throttle/disappear) — that's the "Requested format is
    # not available" error. Chain fallbacks: exact tier tapped -> an
    # equivalent height-capped combo -> plain best-of-everything, and if the
    # whole chain still errors, retry once with the simplest selector.
    fmt_chain = f"{format_id}+bestaudio/bestvideo{height_cap}+bestaudio/best{height_cap}/best"
    last_err = None
    for attempt in range(3):
        try:
            return _run(fmt_chain)
        except Exception as e:
            last_err = e
            if _is_transient_error(e) and attempt < 2:
                # CDN sometimes sends a broken gzip stream on consecutive
                # requests too - short backoff before hammering it again.
                time.sleep(2 * (attempt + 1))
                continue
            if "Requested format is not available" in str(e):
                return _run("best")
            raise
    raise last_err


def _download_thumbnail(thumb_url, out_dir):
    if not thumb_url:
        return None
    try:
        resp = requests.get(thumb_url, timeout=15)
        resp.raise_for_status()
        path = os.path.join(out_dir, "thumb.jpg")
        with open(path, "wb") as f:
            f.write(resp.content)
        return path
    except Exception:
        return None


def _quality_keyboard(session_id: str, tiers) -> InlineKeyboardMarkup:
    rows = []
    for t in tiers:
        res_label = f"{t['height']}p" if t['height'] else "Best available"
        label = f"🎬 {res_label}{_fmt_size(t['filesize'])}"
        rows.append([InlineKeyboardButton(label, callback_data=f"ytq:{session_id}:{t['format_id']}|{t['height']}")])
    rows.append([InlineKeyboardButton("🎵 MP3 (audio only)", callback_data=f"ytq:{session_id}:mp3")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"ytq:{session_id}:cancel")])
    return InlineKeyboardMarkup(rows)


def _fmt_count(n):
    if not n:
        return None
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


async def _edit_status(status_msg: Message, text: str, reply_markup=None):
    """Works whether status_msg is a plain text message or a photo message."""
    if status_msg.photo:
        return await status_msg.edit_caption(text, reply_markup=reply_markup, parse_mode=enums.ParseMode.HTML)
    return await status_msg.edit_text(text, reply_markup=reply_markup, parse_mode=enums.ParseMode.HTML)


def _make_download_progress_hook(status: Message, loop: asyncio.AbstractEventLoop):
    """yt-dlp calls progress_hooks synchronously from the worker thread that's
    running the actual download, so we can't just `await _edit_status(...)`
    here directly. Instead we hand the edit coroutine back to the bot's main
    event loop with run_coroutine_threadsafe. Throttled the same way the
    upload progress is, so it doesn't hammer Telegram's edit rate limit."""
    state = {"last_edit": 0.0, "last_pct": -1}

    def _hook(d):
        status_type = d.get("status")
        if status_type == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            now = time.time()
            pct = (downloaded * 100 / total) if total else 0
            finished_chunk = total and downloaded >= total
            if not finished_chunk and (now - state["last_edit"] < 2.5 or int(pct) == state["last_pct"]):
                return
            state["last_edit"] = now
            state["last_pct"] = int(pct)
            text = format_progress(
                pct,
                speed_bps=d.get("speed"),
                done_bytes=downloaded,
                total_bytes=total,
                elapsed_secs=d.get("elapsed"),
                eta_secs=d.get("eta"),
                title="Downloading...",
            )
            asyncio.run_coroutine_threadsafe(_edit_status(status, text), loop)
        elif status_type == "finished":
            asyncio.run_coroutine_threadsafe(
                _edit_status(status, f"<b>{E_BOLT} Processing...</b>"), loop
            )

    return _hook


def _has_quality_formats_sync(url: str) -> bool:
    """Quick check: does yt-dlp find usable video formats for this URL?
    Used by other modules (e.g. facebook.py) to decide whether to show the
    shared quality picker or fall back to a site-specific method."""
    if yt_dlp is None:
        return False
    try:
        info = _extract_info(url)
        return bool(_pick_qualities(info))
    except Exception:
        return False


async def has_quality_formats(url: str) -> bool:
    return await asyncio.to_thread(_has_quality_formats_sync, url)


async def _show_quality_picker(client: Client, message: Message, url: str):
    if yt_dlp is None:
        return await message.reply_text(
            f"<b>{E_CROSS} yt-dlp not installed.</b>\n<i>Run <code>pip install yt-dlp</code> on the host.</i>",
            parse_mode=enums.ParseMode.HTML
        )

    fetching = await message.reply_text(f"<b>{E_ROCKET} Fetching available qualities...</b>", parse_mode=enums.ParseMode.HTML)
    try:
        info = await asyncio.to_thread(_extract_info, url)
    except Exception as e:
        return await fetching.edit_text(f"<b>{E_CROSS} Couldn't fetch info:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)

    tiers = _pick_qualities(info)
    if not tiers:
        return await fetching.edit_text(f"<b>{E_CROSS} No downloadable video formats found.</b>", parse_mode=enums.ParseMode.HTML)

    title = info.get("title", "Video")
    uploader = info.get("uploader", "Unknown")
    views = _fmt_count(info.get("view_count"))
    upload_date = info.get("upload_date")  # YYYYMMDD
    if upload_date and len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    lines = [f"<b>{E_ROCKET} {title[:80]}</b>", ""]
    lines.append(f"👤 <b>By:</b> {uploader}")
    lines.append(f"⏱ <b>Duration:</b> {_fmt_duration(info.get('duration'))}")
    if views:
        lines.append(f"👁 <b>Views:</b> {views}")
    if upload_date:
        lines.append(f"📅 <b>Uploaded:</b> {upload_date}")
    lines.append("")
    lines.append("<b>Available qualities:</b>")
    for t in tiers:
        res_label = f"{t['height']}p" if t['height'] else "Best available"
        lines.append(f"✅ {res_label}{_fmt_size(t['filesize'])}")
    lines.append("🎵 MP3 (audio)")
    lines.append("")
    lines.append("<i>Tap a quality below to download:</i>")
    text = "\n".join(lines)

    keyboard = None  # built below once session_id is known
    thumb_url = info.get("thumbnail")

    session_id = uuid.uuid4().hex[:10]
    _SESSIONS[session_id] = {
        "url": url,
        "title": title,
        "thumbnail": thumb_url,
        "uploader": uploader,
        "duration": info.get("duration", 0),
        "chat_id": message.chat.id,
        "reply_to": message.id,
    }
    keyboard = _quality_keyboard(session_id, tiers)

    await fetching.delete()
    if thumb_url:
        try:
            await client.send_photo(
                message.chat.id, photo=thumb_url, caption=text, reply_markup=keyboard,
                reply_to_message_id=message.id, parse_mode=enums.ParseMode.HTML
            )
            return
        except Exception:
            pass
    await message.reply_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)


@Client.on_callback_query(filters.regex(r"^ytq:([a-f0-9]+):(\S+)$"))
async def quality_pick_callback(client: Client, callback_query: CallbackQuery):
    session_id, choice = callback_query.matches[0].group(1), callback_query.matches[0].group(2)
    session = _SESSIONS.get(session_id)
    if not session:
        return await callback_query.answer("This session expired. Send the link again.", show_alert=True)

    if choice == "cancel":
        _SESSIONS.pop(session_id, None)
        await callback_query.message.delete()
        return await callback_query.answer("Cancelled.")

    await callback_query.answer("Downloading...")
    audio_only = choice == "mp3"
    format_id, height = None, None
    if not audio_only:
        if "|" in choice:
            format_id, height_str = choice.split("|", 1)
            try:
                height = int(height_str)
            except ValueError:
                height = None
        else:
            format_id = choice  # backward-compat with any pre-existing session

    session_dir = os.path.join(DOWNLOAD_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)
    status = callback_query.message

    try:
        await _edit_status(status, f"<b>{E_ROCKET} Downloading...</b>")
        loop = asyncio.get_running_loop()
        hook = _make_download_progress_hook(status, loop)
        filepath, info = await asyncio.to_thread(
            _download_selected, session["url"], session_dir, format_id, audio_only, height, hook
        )
        if not os.path.exists(filepath):
            raise FileNotFoundError("Download finished but file was not found (likely size limit).")

        size = os.path.getsize(filepath)
        if size > YTDL_MAX_FILESIZE:
            raise ValueError(f"File too large ({round(size / (1024*1024))} MB) to upload to Telegram.")

        await _edit_status(status, f"<b>{E_BOLT} Uploading...</b>")
        thumb_path = await asyncio.to_thread(_download_thumbnail, session.get("thumbnail"), session_dir)
        caption = format_media_caption(info, credit="anujedits76")
        progress = make_upload_progress(status)

        if audio_only:
            await client.send_audio(
                session["chat_id"], filepath, thumb=thumb_path, caption=caption,
                reply_to_message_id=session["reply_to"], parse_mode=enums.ParseMode.HTML,
                progress=progress
            )
        else:
            await client.send_video(
                session["chat_id"], filepath, thumb=thumb_path, caption=caption,
                duration=int(info.get("duration") or 0),
                reply_to_message_id=session["reply_to"], parse_mode=enums.ParseMode.HTML,
                supports_streaming=True, progress=progress
            )
        await status.delete()
    except Exception as e:
        await _edit_status(status, f"<b>{E_CROSS} Download failed:</b>\n<code>{e}</code>")
    finally:
        _SESSIONS.pop(session_id, None)
        shutil.rmtree(session_dir, ignore_errors=True)


@Client.on_message(filters.command(["yt", "dl"]) & filters.private)
async def yt_video_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_BOLT} Usage:</b> <code>/yt &lt;video URL&gt;</code>\n"
            f"<i>Supports YouTube, Instagram, and 1000+ other yt-dlp-compatible sites. "
            f"Shows a quality picker with real thumbnails.</i>",
            parse_mode=enums.ParseMode.HTML
        )
    await _show_quality_picker(client, message, message.command[1])


@Client.on_message(filters.command(["yta", "song", "adl"]) & filters.private)
async def yt_audio_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_BOLT} Usage:</b> <code>/yta &lt;video URL&gt;</code> — extracts audio (mp3) directly",
            parse_mode=enums.ParseMode.HTML
        )
    url = message.command[1]
    if yt_dlp is None:
        return await message.reply_text(
            f"<b>{E_CROSS} yt-dlp not installed.</b>", parse_mode=enums.ParseMode.HTML
        )

    status = await message.reply_text(f"<b>{E_ROCKET} Downloading audio...</b>", parse_mode=enums.ParseMode.HTML)
    session_dir = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex[:10])
    os.makedirs(session_dir, exist_ok=True)
    try:
        loop = asyncio.get_running_loop()
        hook = _make_download_progress_hook(status, loop)
        filepath, info = await asyncio.to_thread(_download_selected, url, session_dir, None, True, None, hook)
        if not os.path.exists(filepath):
            raise FileNotFoundError("Download finished but file was not found.")
        await status.edit_text(f"<b>{E_BOLT} Uploading...</b>", parse_mode=enums.ParseMode.HTML)
        thumb_path = await asyncio.to_thread(_download_thumbnail, info.get("thumbnail"), session_dir)
        await client.send_audio(
            message.chat.id, filepath, thumb=thumb_path,
            caption=format_media_caption(info, credit="anujedits76"),
            reply_to_message_id=message.id, parse_mode=enums.ParseMode.HTML,
            progress=make_upload_progress(status)
        )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"<b>{E_CROSS} Download failed:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Playlist support — a link carrying &list= downloads every video at best
# quality (no per-video picker, that'd mean tapping through N menus).
# ---------------------------------------------------------------------------

def _extract_playlist_video_urls(url: str):
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    urls = []
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        webpage_url = e.get("url") or e.get("webpage_url")
        vid = e.get("id")
        if webpage_url and webpage_url.startswith("http"):
            urls.append(webpage_url)
        elif vid:
            urls.append(f"https://www.youtube.com/watch?v={vid}")
    return urls


async def _run_playlist(client: Client, message: Message, url: str):
    status = await message.reply_text(f"<b>{E_ROCKET} Fetching playlist info...</b>", parse_mode=enums.ParseMode.HTML)
    try:
        video_urls = await asyncio.to_thread(_extract_playlist_video_urls, url)
    except Exception as e:
        return await status.edit_text(f"<b>{E_CROSS} Couldn't read playlist:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)
    if not video_urls:
        return await status.edit_text(f"<b>{E_CROSS} No videos found in this playlist.</b>", parse_mode=enums.ParseMode.HTML)

    total = len(video_urls)
    await status.edit_text(f"<b>{E_ROCKET} Playlist detected — {total} video(s), best quality. Starting...</b>", parse_mode=enums.ParseMode.HTML)

    for i, video_url in enumerate(video_urls, 1):
        item_status = await message.reply_text(f"<b>{E_ROCKET} Video {i}/{total}: downloading...</b>", parse_mode=enums.ParseMode.HTML)
        session_dir = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex[:10])
        os.makedirs(session_dir, exist_ok=True)
        try:
            filepath, info = await asyncio.to_thread(_download_selected, video_url, session_dir, "bestvideo", False)
            if not os.path.exists(filepath):
                raise FileNotFoundError("Download finished but file was not found (likely size limit).")
            await item_status.edit_text(f"<b>{E_BOLT} Video {i}/{total}: uploading...</b>", parse_mode=enums.ParseMode.HTML)
            thumb_path = await asyncio.to_thread(_download_thumbnail, info.get("thumbnail"), session_dir)
            await client.send_video(
                message.chat.id, filepath, thumb=thumb_path,
                caption=format_media_caption(info, credit="anujedits76"),
                duration=int(info.get("duration") or 0),
                reply_to_message_id=message.id, parse_mode=enums.ParseMode.HTML,
                supports_streaming=True, progress=make_upload_progress(item_status)
            )
            await item_status.delete()
        except Exception as e:
            await item_status.edit_text(f"<b>{E_CROSS} Video {i}/{total} failed:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)
        finally:
            shutil.rmtree(session_dir, ignore_errors=True)

    await status.delete()


# ---------------------------------------------------------------------------
# Auto-detect: bare links (no /yt, /insta, /pin command) route straight into
# the same quality picker used by the commands above. Registered in group=1
# so they don't clash with other files' handlers, and exclude "^/" so
# commands aren't processed twice.
# ---------------------------------------------------------------------------

@Client.on_message(filters.text & filters.private & filters.regex(INSTA_PATTERN) & ~filters.regex(r"^/"), group=1)
async def insta_auto_detect(client: Client, message: Message):
    m = INSTA_PATTERN.search(message.text)
    if m:
        await _show_quality_picker(client, message, m.group(0))


@Client.on_message(filters.text & filters.private & filters.regex(PINTEREST_PATTERN) & ~filters.regex(r"^/"), group=1)
async def pinterest_auto_detect(client: Client, message: Message):
    m = PINTEREST_PATTERN.search(message.text)
    if not m:
        return
    url = m.group(0)
    # Probe first: video pins get the quality picker here; image pins/boards
    # get handed off to gallery.py's gallery-dl handler.
    is_video = await has_quality_formats(url)
    if is_video:
        await _show_quality_picker(client, message, url)
    else:
        from Rexbots.gallery import _handle as gallery_handle
        await gallery_handle(client, message, url)


@Client.on_message(
    filters.text & filters.private
    & (filters.regex(YOUTUBE_PATTERN) | filters.regex(PLAYLIST_REGEX))
    & ~filters.regex(r"^/"),
    group=1,
)
async def youtube_auto_detect(client: Client, message: Message):
    text = message.text.strip()
    if PLAYLIST_REGEX.search(text):
        await _run_playlist(client, message, text)
        return
    m = YOUTUBE_PATTERN.search(text)
    if m:
        await _show_quality_picker(client, message, m.group(0))


def _extract_generic_url(text: str):
    m = GENERIC_URL_PATTERN.search(text)
    if not m:
        return None
    url = m.group(0)
    lower = url.lower()
    return None if any(d in lower for d in _EXCLUDED_DOMAINS) else url


@Client.on_message(
    filters.text & filters.private & filters.regex(GENERIC_URL_PATTERN) & ~filters.regex(r"^/"),
    group=2,  # after the specific group=1 handlers above and in other files
)
async def generic_ytdlp_auto_detect(client: Client, message: Message):
    url = _extract_generic_url(message.text)
    if not url:
        return
    if await has_quality_formats(url):
        await _show_quality_picker(client, message, url)
