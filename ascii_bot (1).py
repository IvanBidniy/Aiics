"""Telegram-бот для перетворення відео на кольорове ASCII."""

import asyncio
from getpass import getpass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SYMBOLS = " .,:;!+*#%@"
COLS, ROWS = 90, 40
CELL_W, CELL_H = 10, 15
WIDTH, HEIGHT = COLS * CELL_W, ROWS * CELL_H
FPS = 24
MAX_SECONDS = 120
MAX_INPUT = 20_000_000
MAX_OUTPUT = 50_000_000


def glyph_masks():
    font = ImageFont.load_default(size=14)
    masks = []
    for symbol in SYMBOLS:
        image = Image.new("L", (CELL_W, CELL_H))
        ImageDraw.Draw(image).text(
            (CELL_W // 2, CELL_H // 2), symbol, font=font, fill=255, anchor="mm"
        )
        masks.append(np.asarray(image, dtype=np.uint16))
    return np.stack(masks)


def render_frame(raw, masks):
    colors = np.frombuffer(raw, dtype=np.uint8).reshape(ROWS, COLS, 3)
    colors = np.clip(colors.astype(np.float32) * 1.12 + 8, 0, 255)
    gray = colors @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    indices = (gray * ((len(SYMBOLS) - 1) / 255)).astype(np.intp)

    levels = colors.astype(np.uint16) // 16 * 16
    cells = masks[indices][..., None] * levels[:, :, None, None, :] // 255
    return cells.transpose(0, 2, 1, 3, 4).astype(np.uint8).tobytes()


def convert_video(source, output):
    masks = glyph_masks()
    # Scale by display aspect ratio, including the shape of a character cell.
    cell_aspect = f"(dar*{CELL_H}/{CELL_W})"
    video_filter = (
        f"fps={FPS},"
        f"scale=w='max(1,trunc(min({COLS},{ROWS}*{cell_aspect})))':"
        f"h='max(1,trunc(min({ROWS},{COLS}/{cell_aspect})))':flags=area,"
        f"format=rgb24,pad={COLS}:{ROWS}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    ffmpeg = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    decoder = ffmpeg + [
        "-i", str(source), "-map", "0:V:0", "-an",
        "-filter_threads", "1", "-vf", video_filter,
        "-threads", "1", "-f", "rawvideo", "pipe:1",
    ]
    encoder = ffmpeg + [
        "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", str(FPS),
        "-i", "pipe:0", "-i", str(source),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-maxrate", "2500k", "-bufsize", "5000k", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-af", "apad", "-shortest",
        "-movflags", "+faststart", str(output),
    ]

    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(decoder, stdout=subprocess.PIPE, stderr=errors) as reader:
            try:
                with subprocess.Popen(encoder, stdin=subprocess.PIPE, stderr=errors) as writer:
                    try:
                        frame_count = 0
                        while raw := reader.stdout.read(COLS * ROWS * 3):
                            if len(raw) != COLS * ROWS * 3:
                                raise RuntimeError("Неповний відеокадр.")
                            frame_count += 1
                            if frame_count > FPS * MAX_SECONDS:
                                raise ValueError(f"Надішли відео до {MAX_SECONDS} секунд.")
                            writer.stdin.write(render_frame(raw, masks))
                        writer.stdin.close()
                        if reader.wait() or writer.wait() or not frame_count:
                            raise RuntimeError("FFmpeg не зміг обробити відео.")
                    except BaseException:
                        writer.kill()
                        raise
            except BaseException:
                reader.kill()
                raise

    if output.stat().st_size > MAX_OUTPUT:
        raise ValueError("Готове відео перевищує 50 МБ. Спробуй коротший ролик.")


def main():
    from telegram.error import TelegramError
    from telegram.ext import Application, CommandHandler, MessageHandler, filters

    if not shutil.which("ffmpeg"):
        raise SystemExit("Встанови FFmpeg і додай його до PATH.")

    token = os.environ.get("TELEGRAM_BOT_TOKEN") or getpass("Токен від BotFather: ")
    token = token.strip()
    if not token:
        raise SystemExit("Потрібен токен від BotFather.")

    processing = asyncio.Lock()

    async def start(update, context):
        await update.effective_message.reply_text(
            "Надішли відео до 20 МБ і до 2 хвилин. Поверну кольорове ASCII-відео у MP4."
        )

    async def receive_video(update, context):
        message = update.effective_message
        media = message.video or message.document
        if media.file_size and media.file_size > MAX_INPUT:
            await message.reply_text("Надішли відео до 20 МБ.")
            return
        if processing.locked():
            await message.reply_text("Бот уже обробляє відео. Спробуй трохи пізніше.")
            return

        async with processing:
            await message.reply_text("Перетворюю відео на ASCII…")
            try:
                with tempfile.TemporaryDirectory(prefix="ascii_bot_") as directory:
                    source = Path(directory) / "input.video"
                    output = Path(directory) / "ascii.mp4"
                    remote_file = await context.bot.get_file(media.file_id)
                    await remote_file.download_to_drive(source, read_timeout=120)
                    await asyncio.to_thread(convert_video, source, output)
                    with output.open("rb") as video:
                        await message.reply_video(
                            video, supports_streaming=True, width=WIDTH, height=HEIGHT,
                            write_timeout=120, read_timeout=120,
                        )
            except ValueError as error:
                await message.reply_text(str(error))
            except TelegramError:
                await message.reply_text("Telegram не зміг передати файл. Спробуй ще раз.")
            except (OSError, RuntimeError):
                await message.reply_text("Не вдалося обробити відео. Спробуй інший MP4.")

    app = Application.builder().token(token).concurrent_updates(4).build()
    app.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.VIDEO | filters.Document.VIDEO),
        receive_video,
    ))
    print("Бот запущено. Надішли йому відео в Telegram.")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
