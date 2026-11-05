import os, re, asyncio, tempfile, shutil, threading, subprocess
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


def download(url: str, audio: bool):
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
    }
    # کوکی یوتیوب (اختیاری): فایل Secret در Render
    secret = "/etc/secrets/cookies.txt"
    if os.path.exists(secret):
        local = os.path.join(tmp, "cookies.txt")
        shutil.copy(secret, local)  # کپی چون پوشهٔ secrets فقط‌خواندنی است
        opts["cookiefile"] = local
    if audio:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        opts["format"] = "b[height<=720][ext=mp4]/bv*[height<=720]+ba/b"
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


def cut_clip(src: str, dst: str):
    """فقط ۲۰ ثانیهٔ اول را نگه می‌دارد تا تشخیص سریع‌تر و سبک‌تر باشد."""
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", src, "-t", "20",
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
        text = f"🎵 {track.get('title', '?')}\n👤 {track.get('subtitle', '?')}"
        kb = None
        if track.get("url"):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 مشاهده در Shazam", url=track["url"])]])
        await status.edit_text(text, reply_markup=kb)
    except Exception:
        await status.edit_text("❌ تشخیص انجام نشد. دوباره امتحان کن.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def on_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = URL_RE.search(update.message.text or "")
    if not m:
        return
    ctx.user_data["url"] = m.group(0)
    if not await is_member(ctx.bot, update.effective_user.id):
        await update.message.reply_text(JOIN_TEXT, reply_markup=join_kb())
        return
    await update.message.reply_text("چی می‌خوای؟", reply_markup=choice_kb())


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
    await q.edit_message_text("⏳ در حال دانلود...")
    tmp = None
    try:
        tmp, path = await asyncio.to_thread(download, url, audio)
        if os.path.getsize(path) > MAX_SIZE:
            await q.edit_message_text("❌ حجم فایل بیشتر از ۵۰ مگابایته و تلگرام اجازه ارسال نمیده.")
            return
        with open(path, "rb") as f:
            if audio:
                await q.message.reply_audio(f)
            else:
                await q.message.reply_video(f, supports_streaming=True)
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
    app.add_handler(CallbackQueryHandler(on_choice))
    app.run_polling()
