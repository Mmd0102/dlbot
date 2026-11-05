import os, re, asyncio, tempfile, shutil, threading, subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

import yt_dlp
import imageio_ffmpeg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters, ContextTypes)

TOKEN = os.environ["BOT_TOKEN"]
MAX_SIZE = 49 * 1024 * 1024  # سقف آپلود ربات‌های تلگرام ~50MB
URL_RE = re.compile(r"https?://\S+")


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
    await update.message.reply_text("لینک ویدیو یا آهنگ رو بفرست 🎬🎵")


async def on_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = URL_RE.search(update.message.text or "")
    if not m:
        return
    ctx.user_data["url"] = m.group(0)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 ویدیو", callback_data="v"),
        InlineKeyboardButton("🎵 آهنگ (MP3)", callback_data="a"),
    ]])
    await update.message.reply_text("چی می‌خوای؟", reply_markup=kb)


async def on_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    url = ctx.user_data.get("url")
    if not url:
        await q.edit_message_text("لینک رو دوباره بفرست.")
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    app.add_handler(CallbackQueryHandler(on_choice))
    app.run_polling()
