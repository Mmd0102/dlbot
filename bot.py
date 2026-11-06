import os, re, asyncio, tempfile, shutil, threading, subprocess, uuid, time, math, traceback
import json, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import yt_dlp
import imageio_ffmpeg
from shazamio import Shazam
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters, ContextTypes)

TOKEN = os.environ["BOT_TOKEN"]
MAX_SIZE = 49 * 1024 * 1024  # سقف آپلود ربات‌های تلگرام ~50MB
URL_RE = re.compile(r"https?://\S+")
# یوزرنیم کانال برای عضویت اجباری، مثل @mychannel (اگر خالی باشد عضویت اجباری خاموش است)
CHANNEL = os.environ.get("CHANNEL", "").strip()
# پیام سنجاق‌شده برای کاربران (اگر خالی باشد، سنجاق نمی‌شود)
NOTICE = os.environ.get("NOTICE", "").strip()
DOWNLOAD_TIMEOUT = 600  # ثانیه؛ بعد از این دانلود قطع می‌شود تا ربات گیر نکند
MAX_DOWNLOAD = 300 * 1024 * 1024  # بزرگ‌ترین حجمی که روی هاست رایگان دانلود می‌کنیم
# حداقل فاصلهٔ دو لینک پشت‌سرهم از یک کاربر (ثانیه). با متغیر LINK_COOLDOWN قابل تغییر است
LINK_COOLDOWN = int(os.environ.get("LINK_COOLDOWN", "30"))
# حداکثر تعداد دانلود همزمان (برای اینکه هاست رایگان کم نیاورد)
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "3"))
# کش دائمی روی Upstash Redis (رایگان). اگر تنظیم نشود، کش فقط در حافظه می‌ماند
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip()
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
BOT_ID = TOKEN.split(":")[0]  # شناسهٔ فایل‌ها مخصوص همین ربات است


def add_cookies(opts: dict):
    """کوکی یوتیوب (اختیاری): از متغیر YT_COOKIES یا فایل Secret.
    فایل کوکی در یک پوشهٔ جدا ساخته می‌شود (نه کنار فایل‌های دانلودی)،
    و مسیر آن پوشه برگردانده می‌شود تا بعد از کار پاک شود."""
    secret = "/etc/secrets/cookies.txt"
    env_cookies = os.environ.get("YT_COOKIES", "")
    if not env_cookies and not os.path.exists(secret):
        return None
    ck = tempfile.mkdtemp()
    local = os.path.join(ck, "cookies.txt")
    if env_cookies:
        with open(local, "w") as cf:
            cf.write(env_cookies)
    else:
        shutil.copy(secret, local)  # کپی چون پوشهٔ secrets فقط‌خواندنی است
    opts["cookiefile"] = local
    return ck


def probe(url: str):
    """یک بار اطلاعات لینک را می‌گیرد و برمی‌گرداند:
    quals: کیفیت‌ها و حجم تقریبی [(ارتفاع, حجم_بایت), ...]
    direct: لینک مستقیم MP4 کوچک (اگر باشد) تا خود تلگرام فایل را بگیرد"""
    ck = None
    try:
        opts = {"quiet": True, "noplaylist": True, "socket_timeout": 20}
        ck = add_cookies(opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    finally:
        if ck:
            shutil.rmtree(ck, ignore_errors=True)
    duration = info.get("duration") or 0
    formats = info.get("formats") or []
    if not formats and info.get("url"):  # لینک مستقیم تک‌فرمتی
        formats = [info]

    def fsize(f):
        s = f.get("filesize") or f.get("filesize_approx")
        if not s and f.get("tbr") and duration:
            s = int(f["tbr"] * 1000 / 8 * duration)
        return s

    # بزرگ‌ترین فایل صوتی تنها (برای اضافه کردن به ویدیوهای بدون صدا)
    audio_size = 0
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none"):
            audio_size = max(audio_size, fsize(f) or 0)
    best = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") == "none":
            continue
        s = fsize(f)
        if not s:
            continue
        if f.get("acodec") == "none":  # ویدیوی بدون صدا: حجم صدا هم اضافه شود
            s += audio_size
        best[h] = max(best.get(h, 0), s)
    # لینک مستقیم MP4 کوچک (ویدیو و صدا در یک فایل)
    direct, direct_h = None, -1
    for f in formats:
        if (f.get("protocol") in ("https", "http") and f.get("ext") == "mp4"
                and f.get("vcodec") != "none" and f.get("acodec") != "none"
                and f.get("url")):
            s = fsize(f)
            h = f.get("height") or 0
            if s and s <= 19 * 1024 * 1024 and h <= 720 and h > direct_h:
                direct, direct_h = f["url"], h
    heights = sorted({f["height"] for f in formats
                      if f.get("height") and f.get("vcodec") != "none"})
    return {"quals": sorted(best.items()), "direct": direct,
            "heights": heights, "duration": duration}


def download(url: str, audio: bool, height: int = None):
    tmp = tempfile.mkdtemp()
    if "spotify.com" in url:
        subprocess.run(
            ["spotdl", "download", url, "--output", tmp,
             "--format", "mp3", "--ffmpeg", imageio_ffmpeg.get_ffmpeg_exe()],
            check=True, timeout=300,
        )
        files = [f for f in os.listdir(tmp) if f.endswith(".mp3")]
        if not files:
            raise RuntimeError("no file")
        return tmp, os.path.join(tmp, files[0])
    opts = {
        "outtmpl": f"{tmp}/%(title).60s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "retries": 3,
        "max_filesize": MAX_DOWNLOAD,  # فایل‌های خیلی بزرگ‌تر رد می‌شوند
        "concurrent_fragment_downloads": 4,
    }
    deadline = time.time() + DOWNLOAD_TIMEOUT

    def hook(d):
        if time.time() > deadline:
            raise yt_dlp.utils.DownloadCancelled("timeout")

    opts["progress_hooks"] = [hook]
    ck = add_cookies(opts)
    if audio:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif height:
        # کیفیتی که کاربر انتخاب کرده
        opts["format"] = f"bv*[height<={height}]+ba/b[height<={height}]/b"
    else:
        # پیش‌فرض: کیفیت حداکثر ۴۸۰ تا سریع‌تر دانلود شود و زیر ۵۰ مگابایت بماند
        opts["format"] = ("b[height<=480][filesize<45M]/b[height<=360]/"
                          "bv*[height<=360]+ba/b[height<=480]/b")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    finally:
        if ck:
            shutil.rmtree(ck, ignore_errors=True)
    # فقط فایل‌های رسانه‌ای؛ بزرگ‌ترین فایل همان ویدیو یا آهنگ است
    files = [os.path.join(tmp, f) for f in os.listdir(tmp)
             if os.path.isfile(os.path.join(tmp, f))
             and not f.endswith((".part", ".ytdl", ".txt", ".json", ".temp"))]
    if not files:
        raise RuntimeError("no file")
    return tmp, max(files, key=os.path.getsize)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "لینک ویدیو یا آهنگ رو بفرست 🎬🎵\n"
        "برای تشخیص آهنگ هم یه ویس، فایل صوتی یا ویدیو بفرست 🎧")
    if NOTICE:
        # پیام اطلاع‌رسانی را می‌فرستد و در چت کاربر سنجاق می‌کند
        msg = await update.message.reply_text(NOTICE)
        try:
            await msg.pin(disable_notification=True)
        except Exception:
            pass
    else:
        await unpin_old(ctx, update.effective_chat.id)


async def unpin_old(ctx, chat_id: int):
    """پیام‌های سنجاق‌شدهٔ قبلی (مثل پیام مشکل یوتیوب) را از چت کاربر برمی‌دارد."""
    try:
        await ctx.bot.unpin_all_chat_messages(chat_id)
    except Exception:
        pass


async def is_member(bot, user_id: int) -> bool:
    """عضویت اجباری: ربات باید در کانال ادمین باشد."""
    if not CHANNEL:
        return True
    try:
        m = await bot.get_chat_member(CHANNEL, user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False


def join_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 عضویت در کانال", url=f"https://t.me/{CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton("✅ عضو شدم", callback_data="check")],
    ])


def choice_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 ویدیو", callback_data="v"),
        InlineKeyboardButton("🎵 آهنگ (MP3)", callback_data="a"),
    ]])


JOIN_TEXT = "برای استفاده از ربات اول باید در کانال ما عضو بشی 👇"


def download_song(query: str):
    """آهنگ را با اسم پیدا و MP3 می‌کند: اول ساندکلاود، اگر نشد spotdl."""
    tmp = tempfile.mkdtemp()
    opts = {
        "outtmpl": f"{tmp}/%(title).60s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"scsearch1:{query}"])
    except Exception:
        pass
    files = [f for f in os.listdir(tmp) if f.endswith(".mp3")]
    if not files:
        try:
            subprocess.run(
                ["spotdl", "download", query, "--output", tmp, "--format", "mp3",
                 "--ffmpeg", imageio_ffmpeg.get_ffmpeg_exe()],
                check=True, timeout=300,
            )
        except Exception:
            pass
        files = [f for f in os.listdir(tmp) if f.endswith(".mp3")]
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("no file")
    return tmp, os.path.join(tmp, files[0])


def cut_clip(src: str, dst: str, start: int = 0):
    """فقط ۲۰ ثانیه از فایل را نگه می‌دارد تا تشخیص سریع‌تر و سبک‌تر باشد."""
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-ss", str(start), "-i", src, "-t", "20",
         "-vn", "-ac", "1", "-ar", "44100", dst],
        check=True, capture_output=True, timeout=120,
    )


def track_text(track: dict) -> str:
    """اسم آهنگ، خواننده و (در صورت وجود) آلبوم و سال انتشار."""
    lines = [f"🎵 {track.get('title', '?')}"]
    if track.get("subtitle"):
        lines.append(f"👤 {track['subtitle']}")
    meta = {}
    for sec in track.get("sections") or []:
        for m in sec.get("metadata") or []:
            meta[m.get("title")] = m.get("text")
    if meta.get("Album"):
        lines.append(f"💿 {meta['Album']}")
    if meta.get("Released"):
        lines.append(f"📅 {meta['Released']}")
    return "\n".join(lines)


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """تشخیص آهنگ از ویس، فایل صوتی، ویدیو یا ویدیو‌مسیج."""
    msg = update.message
    if not await is_member(ctx.bot, update.effective_user.id):
        await msg.reply_text(JOIN_TEXT, reply_markup=join_kb())
        return
    media = msg.voice or msg.audio or msg.video or msg.video_note
    if media.file_size and media.file_size > 20 * 1024 * 1024:
        await msg.reply_text("❌ فایل بیشتر از ۲۰ مگابایته. یه تیکهٔ کوتاه‌تر بفرست.")
        return
    status = await msg.reply_text("🎧 در حال تشخیص آهنگ...")
    tmp = tempfile.mkdtemp()
    try:
        f = await ctx.bot.get_file(media.file_id)
        src = os.path.join(tmp, "in")
        await f.download_to_drive(src)
        clip = os.path.join(tmp, "clip.mp3")
        await asyncio.to_thread(cut_clip, src, clip)
        shazam = Shazam()
        recognize = getattr(shazam, "recognize", None) or shazam.recognize_song
        out = await recognize(clip)
        track = out.get("track")
        if not track:
            await status.edit_text("😕 آهنگ تشخیص داده نشد. یه تیکهٔ واضح‌تر و طولانی‌تر (۱۰ تا ۲۰ ثانیه) بفرست.")
            return
        title, artist = track.get("title", "?"), track.get("subtitle", "")
        text = track_text(track)
        await status.edit_text(text + "\n\n⏳ در حال دانلود آهنگ...")
        song_tmp = None
        try:
            song_tmp, song = await asyncio.to_thread(download_song, f"{artist} {title}")
            if os.path.getsize(song) > MAX_SIZE:
                raise RuntimeError("too big")
            with open(song, "rb") as sf:
                await msg.reply_audio(sf, title=title, performer=artist)
            await status.edit_text(text)
        except Exception:
            await status.edit_text(text + "\n\n❌ خود آهنگ پیدا نشد، فقط اسمش تشخیص داده شد.")
        finally:
            if song_tmp:
                shutil.rmtree(song_tmp, ignore_errors=True)
    except Exception:
        await status.edit_text("❌ تشخیص انجام نشد. دوباره امتحان کن.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def on_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = URL_RE.search(update.message.text or "")
    if not m:
        return
    # فاصلهٔ زمانی بین دو لینک هر کاربر تا فشار زیادی به ربات نیاید
    wait = LINK_COOLDOWN - (time.time() - ctx.user_data.get("last_link", 0))
    if wait > 0:
        await update.message.reply_text(
            f"⏳ لطفاً {math.ceil(wait)} ثانیه دیگه صبر کن و بعد لینک بعدی رو بفرست.")
        return
    ctx.user_data["url"] = m.group(0)
    if not NOTICE and not ctx.user_data.get("unpinned"):
        ctx.user_data["unpinned"] = True
        await unpin_old(ctx, update.effective_chat.id)
    if not await is_member(ctx.bot, update.effective_user.id):
        await update.message.reply_text(JOIN_TEXT, reply_markup=join_kb())
        return
    ctx.user_data["last_link"] = time.time()
    link = ctx.user_data["url"]
    status = await update.message.reply_text("⏳ در حال پردازش...")
    await process(Msg(status), ctx, link, "spotify.com" in link)


async def on_music(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """دکمهٔ «دریافت موسیقی» زیر ویدیو."""
    q = update.callback_query
    if not await is_member(ctx.bot, q.from_user.id):
        await q.answer("اول باید در کانال عضو بشی ❌", show_alert=True)
        return
    url = ctx.bot_data.get("links", {}).get(q.data[2:])
    if not url:
        await q.answer("این دکمه منقضی شده، لینک رو دوباره بفرست.", show_alert=True)
        return
    await q.answer("⏳ در حال پیدا کردن آهنگ...")
    status = await q.message.reply_text("🎧 در حال تشخیص آهنگ این محتوا...")
    tmp = song_tmp = None
    text = None
    try:
        # ۱) صدای محتوا را می‌گیریم
        tmp, path = await asyncio.to_thread(download, url, True)
        # ۲) آهنگ را با Shazam تشخیص می‌دهیم (اول از ابتدا، بعد از ثانیهٔ ۱۵)
        shazam = Shazam()
        recognize = getattr(shazam, "recognize", None) or shazam.recognize_song
        track = None
        for start in (0, 15):
            clip = os.path.join(tmp, f"clip{start}.mp3")
            try:
                await asyncio.to_thread(cut_clip, path, clip, start)
                track = (await recognize(clip)).get("track")
            except Exception:
                track = None
            if track:
                break
        if not track:
            await status.edit_text("😕 آهنگ این محتوا تشخیص داده نشد.")
            return
        title, artist = track.get("title", "?"), track.get("subtitle", "")
        text = track_text(track)
        await status.edit_text(text + "\n\n⏳ در حال دانلود آهنگ کامل...")
        # ۳) خود آهنگ کامل را پیدا و ارسال می‌کنیم
        song_tmp, song = await asyncio.to_thread(download_song, f"{artist} {title}")
        if os.path.getsize(song) > MAX_SIZE:
            raise RuntimeError("too big")
        with open(song, "rb") as sf:
            await q.message.reply_audio(sf, title=title, performer=artist)
        await status.edit_text(text)
    except Exception:
        traceback.print_exc()
        if text:  # آهنگ تشخیص داده شده ولی فایلش پیدا یا ارسال نشد
            await status.edit_text(text + "\n\n❌ خود آهنگ پیدا نشد، فقط مشخصاتش تشخیص داده شد.")
        else:
            await status.edit_text("❌ پیدا کردن آهنگ انجام نشد.")
    finally:
        for d in (tmp, song_tmp):
            if d:
                shutil.rmtree(d, ignore_errors=True)


def split_video(path: str):
    """ویدیوی بزرگ‌تر از ۵۰ مگابایت را بدون افت کیفیت به چند بخش تقسیم می‌کند."""
    size = os.path.getsize(path)
    if size <= MAX_SIZE:
        return [path]
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run([ffmpeg, "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m:
        raise RuntimeError("no duration")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    parts = math.ceil(size / (30 * 1024 * 1024))  # حاشیهٔ اطمینان چون برش روی کلیدفریم‌هاست
    seg = max(int(duration / parts), 5)
    out_dir = os.path.dirname(path)
    subprocess.run(
        [ffmpeg, "-y", "-i", path, "-map", "0", "-c", "copy", "-f", "segment",
         "-segment_time", str(seg), "-reset_timestamps", "1",
         os.path.join(out_dir, "part%03d.mp4")],
        check=True, capture_output=True, timeout=300,
    )
    files = sorted(os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.startswith("part"))
    if not files or any(os.path.getsize(f) > MAX_SIZE for f in files):
        raise RuntimeError("part too big")
    return files


def save_link(ctx, url: str) -> str:
    """لینک را در حافظه نگه می‌دارد و یک شناسهٔ کوتاه برای دکمه‌ها برمی‌گرداند."""
    token = uuid.uuid4().hex[:12]
    links = ctx.bot_data.setdefault("links", {})
    if len(links) > 500:
        links.pop(next(iter(links)))
    links[token] = url
    return token


def music_kb(ctx, url: str):
    """دکمهٔ «دریافت موسیقی» زیر ویدیو."""
    token = save_link(ctx, url)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎵دریافت موسیقی🎵", callback_data=f"m:{token}")]])


def fmt_mb(size: int) -> str:
    return f"{size / (1024 * 1024):.0f} MB"


TRACKING_PARAMS = {"si", "igshid", "fbclid", "feature", "ref", "ref_", "gclid", "pp", "s"}


def normalize_url(url: str) -> str:
    """لینک‌های مختلف یک محتوا را به یک شکل واحد درمی‌آورد تا کش درست کار کند."""
    u = urllib.parse.urlsplit(url.strip())
    host = u.netloc.lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    path = u.path.rstrip("/")
    q = urllib.parse.parse_qs(u.query)
    if host == "youtu.be" and path:
        return "yt:" + path.lstrip("/").split("/")[0]
    if host in ("youtube.com", "music.youtube.com"):
        if path == "/watch" and q.get("v"):
            return "yt:" + q["v"][0]
        m = re.match(r"^/(shorts|embed|live)/([\w-]{6,})", path)
        if m:
            return "yt:" + m.group(2)
    keep = sorted((k, v[0]) for k, v in q.items()
                  if not (k.startswith("utm_") or k in TRACKING_PARAMS))
    return host + path + ("?" + urllib.parse.urlencode(keep) if keep else "")


def ckey(url: str, variant: int) -> str:
    return f"{BOT_ID}|{normalize_url(url)}|{variant}"


def _remote(cmd):
    """یک دستور Redis را از طریق REST اجرا می‌کند؛ در صورت خطا None."""
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return None
    req = urllib.request.Request(
        UPSTASH_URL, data=json.dumps(cmd).encode(), method="POST",
        headers={"Authorization": f"Bearer {UPSTASH_TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get("result")
    except Exception:
        return None


def remote_get(key):
    res = _remote(["GET", key])
    if not res:
        return None
    try:
        return [tuple(x) for x in json.loads(res)]
    except Exception:
        return None


class Msg:
    """پیام وضعیت را شبیه callback_query می‌کند تا همان توابع برای لینک مستقیم هم کار کنند."""

    def __init__(self, message):
        self.message = message

    async def edit_message_text(self, *a, **k):
        return await self.message.edit_text(*a, **k)

    async def delete_message(self):
        return await self.message.delete()


AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac")
_sem = None


def get_sem():
    """محدودیت تعداد دانلود همزمان."""
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_PARALLEL)
    return _sem


async def remember(ctx, key, items):
    """شناسهٔ فایل‌های ارسال‌شده را نگه می‌دارد تا دفعهٔ بعد فوری ارسال شود."""
    cache = ctx.bot_data.setdefault("cache", {})
    if len(cache) > 2000:
        cache.pop(next(iter(cache)))
    cache[key] = items
    await asyncio.to_thread(_remote, ["SET", key, json.dumps(items)])


def file_ref(msg):
    """شناسهٔ فایل ارسال‌شده: (file_id, 'audio' یا 'video')، یا None."""
    if msg.audio:
        return msg.audio.file_id, "audio"
    if msg.video:
        return msg.video.file_id, "video"
    return None


async def send_cached(q, ctx, key, url: str) -> bool:
    """اگر قبلاً همین محتوا ارسال شده، فوری با شناسهٔ فایل می‌فرستد."""
    cache = ctx.bot_data.setdefault("cache", {})
    items = cache.get(key)
    if not items:
        items = await asyncio.to_thread(remote_get, key)  # کش دائمی
        if items:
            cache[key] = items
    if not items:
        return False
    try:
        for i, (fid, kind) in enumerate(items, 1):
            cap = f"بخش {i} از {len(items)}" if len(items) > 1 else None
            if kind == "audio":
                await q.message.reply_audio(fid, caption=cap)
            else:
                await q.message.reply_video(
                    fid, supports_streaming=True, caption=cap,
                    reply_markup=music_kb(ctx, url) if i == len(items) else None)
        await q.delete_message()
        return True
    except Exception:
        cache.pop(key, None)  # شناسه معتبر نبود؛ از کش پاک می‌شود
        await asyncio.to_thread(_remote, ["DEL", key])
        return False


async def deliver_video(q, ctx, url: str, height: int = None):
    """ویدیو را دانلود می‌کند و می‌فرستد (در صورت نیاز در چند بخش)."""
    key = ckey(url, height or 0)
    if await send_cached(q, ctx, key, url):
        return
    tmp = None
    try:
        async with get_sem():
            tmp, path = await asyncio.to_thread(download, url, False, height)
            parts = await asyncio.to_thread(split_video, path)
        refs = []
        for i, p in enumerate(parts, 1):
            is_audio = p.lower().endswith(AUDIO_EXTS)
            cap = f"بخش {i} از {len(parts)}" if len(parts) > 1 else None
            with open(p, "rb") as f:
                if is_audio:  # سایت‌های موسیقی (مثل ساندکلاود)
                    msg = await q.message.reply_audio(f, caption=cap)
                else:
                    msg = await q.message.reply_video(
                        f, supports_streaming=True, caption=cap,
                        reply_markup=music_kb(ctx, url) if i == len(parts) else None)
            refs.append(file_ref(msg))
        if all(refs):
            await remember(ctx, key, refs)
        await q.delete_message()
    except Exception:
        traceback.print_exc()  # خطا در Logs رندر ثبت می‌شود
        await q.edit_message_text("❌ دانلود نشد (زمان تمام شد یا حجم زیاد بود). یه کیفیت پایین‌تر یا لینک دیگه امتحان کن.")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


async def deliver_audio(q, ctx, url: str):
    """فایل صوتی (مثل اسپاتیفای) را دانلود و ارسال می‌کند."""
    key = ckey(url, -1)
    if await send_cached(q, ctx, key, url):
        return
    await q.edit_message_text("⏳ در حال دانلود...")
    tmp = None
    try:
        async with get_sem():
            tmp, path = await asyncio.to_thread(download, url, True)
        if os.path.getsize(path) > MAX_SIZE:
            await q.edit_message_text("❌ حجم فایل بیشتر از ۵۰ مگابایته و تلگرام اجازه ارسال نمیده.")
            return
        with open(path, "rb") as f:
            msg = await q.message.reply_audio(f)
        ref = file_ref(msg)
        if ref:
            await remember(ctx, key, [ref])
        await q.delete_message()
    except Exception:
        traceback.print_exc()
        await q.edit_message_text("❌ دانلود نشد. لینک یا پشتیبانی سایت رو چک کن.")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


async def process(q, ctx, url: str, audio: bool):
    """کل مسیر یک لینک: کش، ارسال مستقیم، منوی کیفیت یا دانلود."""
    if audio:
        await deliver_audio(q, ctx, url)
        return
    key = ckey(url, 0)
    if await send_cached(q, ctx, key, url):
        return
    await q.edit_message_text("🔎 در حال بررسی لینک...")
    try:
        info = await asyncio.to_thread(probe, url)
    except Exception:
        traceback.print_exc()  # دلیل خطا در Logs رندر ثبت می‌شود
        info = {"quals": [], "direct": None, "heights": [], "duration": 0}
    # راه سریع: لینک مستقیم کوچک را به تلگرام می‌دهیم تا خودش فایل را بگیرد
    if info["direct"]:
        try:
            msg = await q.message.reply_video(
                info["direct"], supports_streaming=True, reply_markup=music_kb(ctx, url))
            ref = file_ref(msg)
            if ref:
                await remember(ctx, key, [ref])
            await q.delete_message()
            return
        except Exception:
            pass  # نشد؟ می‌رویم سراغ دانلود معمولی
    quals = [(h, s) for h, s in info["quals"] if s <= MAX_DOWNLOAD]
    options = None
    if quals and max(s for _, s in quals) > MAX_SIZE:
        # حجم ویدیو از ۵۰ مگابایت بیشتر است
        options = quals[-6:]
        note = "📦 حجم این ویدیو از ۵۰ مگابایت بیشتره.\nکیفیت مورد نظرت رو انتخاب کن (حجم‌ها تقریبیه):"
    elif not quals and info.get("heights") and (info.get("duration") or 0) > 600:
        # حجم‌ها معلوم نیست ولی ویدیو بلند است: کیفیت‌ها را بدون حجم نشان می‌دهیم
        options = [(h, None) for h in info["heights"][-6:]]
        note = "📦 این ویدیو طولانیه و ممکنه حجمش از ۵۰ مگابایت بیشتر باشه.\nکیفیت مورد نظرت رو انتخاب کن:"
    if options:
        token = save_link(ctx, url)
        rows = []
        for h, sz in options:
            if sz is None:
                label = f"{h}p — حجم نامشخص"
            else:
                label = f"{h}p — {fmt_mb(sz)}"
                if sz > MAX_SIZE:
                    # ترتیب برعکس نوشته شده چون تلگرام در این خط ترکیبی، عدد را جابه‌جا نشان می‌دهد
                    label += f" (بخش {math.ceil(sz / (30 * 1024 * 1024))})"
            rows.append([InlineKeyboardButton(label, callback_data=f"q:{token}:{h}")])
        await q.edit_message_text(note, reply_markup=InlineKeyboardMarkup(rows))
        return
    await q.edit_message_text("⏳ در حال دانلود...")
    await deliver_video(q, ctx, url)


async def on_quality(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """کاربر یکی از کیفیت‌های پیشنهادی را انتخاب کرد."""
    q = update.callback_query
    if not await is_member(ctx.bot, q.from_user.id):
        await q.answer("اول باید در کانال عضو بشی ❌", show_alert=True)
        return
    try:
        _, token, h = q.data.split(":")
        height = int(h)
    except ValueError:
        await q.answer()
        return
    url = ctx.bot_data.get("links", {}).get(token)
    if not url:
        await q.answer("این گزینه منقضی شده، لینک رو دوباره بفرست.", show_alert=True)
        return
    await q.answer()
    await q.edit_message_text(f"⏳ در حال دانلود با کیفیت {height}p ...")
    await deliver_video(q, ctx, url, height)


async def on_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """دکمه‌های «عضو شدم» و دکمه‌های قدیمی ویدیو/آهنگ."""
    q = update.callback_query
    if not await is_member(ctx.bot, q.from_user.id):
        if q.data == "check":
            await q.answer("هنوز عضو کانال نشدی ❌", show_alert=True)
        else:
            await q.answer()
            await q.edit_message_text(JOIN_TEXT, reply_markup=join_kb())
        return
    await q.answer()
    url = ctx.user_data.get("url")
    if not url:
        await q.edit_message_text("لینک رو دوباره بفرست.")
        return
    audio = q.data == "a" or "spotify.com" in url
    await process(q, ctx, url, audio)


# سرور کوچک برای اینکه هاست‌های رایگان (Render/Koyeb) ربات رو خاموش نکنن
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        # UptimeRobot رایگان با درخواست HEAD بررسی می‌کند
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def run_health():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=run_health, daemon=True).start()
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    app.add_handler(CallbackQueryHandler(on_music, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^(v|a|check)$"))
    app.run_polling()
