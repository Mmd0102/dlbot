import os, re, asyncio, tempfile, shutil, threading, subprocess, uuid, time, math
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


def get_direct_url(url: str):
    """لینک مستقیم MP4 را بدون دانلود پیدا می‌کند تا خود تلگرام فایل را بگیرد.
    فقط وقتی جواب می‌دهد که فایل مستقیم باشد و حجمش زیر ۱۹ مگابایت باشد."""
    opts = {
        "quiet": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "format": "b[protocol=https][ext=mp4][height<=720]/b[protocol=http][ext=mp4][height<=720]",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    size = info.get("filesize") or info.get("filesize_approx")
    if not size or size > 19 * 1024 * 1024 or not info.get("url"):
        return None
    return info["url"]


def add_cookies(opts: dict, tmp: str):
    """کوکی یوتیوب (اختیاری): از متغیر YT_COOKIES یا فایل Secret."""
    secret = "/etc/secrets/cookies.txt"
    env_cookies = os.environ.get("YT_COOKIES", "")
    if env_cookies:
        local = os.path.join(tmp, "cookies.txt")
        with open(local, "w") as cf:
            cf.write(env_cookies)
        opts["cookiefile"] = local
    elif os.path.exists(secret):
        local = os.path.join(tmp, "cookies.txt")
        shutil.copy(secret, local)  # کپی چون پوشهٔ secrets فقط‌خواندنی است
        opts["cookiefile"] = local


def list_qualities(url: str):
    """کیفیت‌های موجود و حجم تقریبی هر کدام را برمی‌گرداند: [(ارتفاع, حجم_بایت), ...]"""
    tmp = tempfile.mkdtemp()
    try:
        opts = {"quiet": True, "noplaylist": True, "socket_timeout": 20}
        add_cookies(opts, tmp)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    duration = info.get("duration") or 0
    formats = info.get("formats") or []

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
    return sorted(best.items())


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
    add_cookies(opts, tmp)
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
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = os.listdir(tmp)
    if not files:
        raise RuntimeError("no file")
    return tmp, os.path.join(tmp, files[0])


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
        text = f"🎵 {title}\n👤 {artist}"
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
    ctx.user_data["url"] = m.group(0)
    if not NOTICE and not ctx.user_data.get("unpinned"):
        ctx.user_data["unpinned"] = True
        await unpin_old(ctx, update.effective_chat.id)
    if not await is_member(ctx.bot, update.effective_user.id):
        await update.message.reply_text(JOIN_TEXT, reply_markup=join_kb())
        return
    await update.message.reply_text("چی می‌خوای؟", reply_markup=choice_kb())


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
        text = f"🎵 {title}\n👤 {artist}"
        await status.edit_text(text + "\n\n⏳ در حال دانلود آهنگ کامل...")
        # ۳) خود آهنگ کامل را پیدا و ارسال می‌کنیم
        song_tmp, song = await asyncio.to_thread(download_song, f"{artist} {title}")
        if os.path.getsize(song) > MAX_SIZE:
            raise RuntimeError("too big")
        with open(song, "rb") as sf:
            await q.message.reply_audio(sf, title=title, performer=artist)
        await status.edit_text(text)
    except Exception:
        await status.edit_text("❌ پیدا کردن آهنگ کامل انجام نشد.")
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


async def deliver_video(q, ctx, url: str, height: int = None):
    """ویدیو را دانلود می‌کند و می‌فرستد (در صورت نیاز در چند بخش)."""
    # راه سریع: لینک مستقیم را به تلگرام می‌دهیم تا خودش فایل را بگیرد
    if height is None:
        try:
            direct = await asyncio.to_thread(get_direct_url, url)
            if direct:
                await q.message.reply_video(direct, supports_streaming=True,
                                            reply_markup=music_kb(ctx, url))
                await q.delete_message()
                return
        except Exception:
            pass  # نشد؟ می‌رویم سراغ دانلود معمولی
    tmp = None
    try:
        tmp, path = await asyncio.to_thread(download, url, False, height)
        parts = await asyncio.to_thread(split_video, path)
        for i, p in enumerate(parts, 1):
            with open(p, "rb") as f:
                await q.message.reply_video(
                    f, supports_streaming=True,
                    caption=f"بخش {i} از {len(parts)}" if len(parts) > 1 else None,
                    reply_markup=music_kb(ctx, url) if i == len(parts) else None)
        await q.delete_message()
    except Exception:
        await q.edit_message_text("❌ دانلود نشد (زمان تمام شد یا حجم زیاد بود). یه کیفیت پایین‌تر یا لینک دیگه امتحان کن.")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


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
    if q.data == "check":
        await q.edit_message_text("✅ عضویتت تأیید شد. چی می‌خوای؟", reply_markup=choice_kb())
        return
    audio = q.data == "a" or "spotify.com" in url
    if not audio:
        await q.edit_message_text("🔎 در حال بررسی کیفیت‌ها...")
        try:
            quals = await asyncio.to_thread(list_qualities, url)
        except Exception:
            quals = []
        quals = [(h, s) for h, s in quals if s <= MAX_DOWNLOAD]
        # اگر حجم ویدیو از ۵۰ مگابایت بیشتر است، کیفیت‌ها را به کاربر نشان می‌دهیم
        if quals and max(s for _, s in quals) > MAX_SIZE:
            token = save_link(ctx, url)
            rows = []
            for h, s in quals[-6:]:
                label = f"{h}p — {fmt_mb(s)}"
                if s > MAX_SIZE:
                    # ترتیب برعکس نوشته شده چون تلگرام در این خط ترکیبی، عدد را جابه‌جا نشان می‌دهد
                    label += f" (بخش {math.ceil(s / (30 * 1024 * 1024))})"
                rows.append([InlineKeyboardButton(label, callback_data=f"q:{token}:{h}")])
            await q.edit_message_text(
                "📦 حجم این ویدیو از ۵۰ مگابایت بیشتره.\n"
                "کیفیت مورد نظرت رو انتخاب کن (حجم‌ها تقریبیه):",
                reply_markup=InlineKeyboardMarkup(rows))
            return
        await q.edit_message_text("⏳ در حال دانلود...")
        await deliver_video(q, ctx, url)
        return
    await q.edit_message_text("⏳ در حال دانلود...")
    tmp = None
    try:
        tmp, path = await asyncio.to_thread(download, url, True)
        if os.path.getsize(path) > MAX_SIZE:
            await q.edit_message_text("❌ حجم فایل بیشتر از ۵۰ مگابایته و تلگرام اجازه ارسال نمیده.")
            return
        with open(path, "rb") as f:
            await q.message.reply_audio(f)
        await q.delete_message()
    except Exception:
        await q.edit_message_text("❌ دانلود نشد. لینک یا پشتیبانی سایت رو چک کن.")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


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
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    app.add_handler(CallbackQueryHandler(on_music, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^(v|a|check)$"))
    app.run_polling()
