"""
Telegram-бот для редактирования тегов аудио (название, исполнитель, обложка).

Поддерживаемые форматы: MP3, FLAC, M4A/MP4.

Установка:
    pip install python-telegram-bot mutagen

Запуск:
    export BOT_TOKEN="твой_токен_от_BotFather"
    python bot.py

Токен получить у @BotFather в Telegram командой /newbot.
"""

import os
import logging
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, APIC, ID3NoHeaderError
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WORK_DIR = Path("bot_files")
WORK_DIR.mkdir(exist_ok=True)

# Состояния, которые бот ждёт от пользователя дальше
STATE_NONE = "none"
STATE_TITLE = "title"
STATE_ARTIST = "artist"
STATE_COVER = "cover"


def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("✏️ Название", callback_data="set_title"),
            InlineKeyboardButton("🎤 Исполнитель", callback_data="set_artist"),
        ],
        [InlineKeyboardButton("🖼 Обложка", callback_data="set_cover")],
        [InlineKeyboardButton("✅ Готово, отправить файл", callback_data="finish")],
    ]
    return InlineKeyboardMarkup(buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Пришли мне аудиофайл (mp3, flac или m4a), "
        "и я помогу поменять у него название, исполнителя и обложку."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как пользоваться:\n"
        "1. Пришли аудиофайл (mp3, flac или m4a)\n"
        "2. Кнопками выбери, что поменять: название, исполнителя, обложку\n"
        "3. Пришли новое значение (текст или фото)\n"
        "4. Нажми «Готово» — получишь файл с новыми тегами"
    )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    audio = update.message.audio or update.message.document
    if audio is None:
        await update.message.reply_text("Не вижу аудиофайл, попробуй ещё раз.")
        return

    file_name = audio.file_name or f"{audio.file_unique_id}.mp3"
    ext = Path(file_name).suffix.lower()
    if ext not in (".mp3", ".flac", ".m4a", ".mp4"):
        await update.message.reply_text(
            "Пока поддерживаю только mp3, flac и m4a. Пришли файл в одном из этих форматов."
        )
        return

    user_dir = WORK_DIR / str(update.effective_user.id)
    user_dir.mkdir(exist_ok=True)
    local_path = user_dir / f"current{ext}"

    tg_file = await context.bot.get_file(audio.file_id)
    await tg_file.download_to_drive(custom_path=str(local_path))

    context.user_data["file_path"] = str(local_path)
    context.user_data["ext"] = ext
    context.user_data["state"] = STATE_NONE
    context.user_data["new_title"] = None
    context.user_data["new_artist"] = None
    context.user_data["cover_path"] = None

    await update.message.reply_text(
        "Файл получен! Что меняем?", reply_markup=main_menu_keyboard()
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if "file_path" not in context.user_data:
        await query.edit_message_text("Сначала пришли аудиофайл командой /start.")
        return

    if query.data == "set_title":
        context.user_data["state"] = STATE_TITLE
        await query.edit_message_text("Пришли новое название трека текстом.")
    elif query.data == "set_artist":
        context.user_data["state"] = STATE_ARTIST
        await query.edit_message_text("Пришли имя исполнителя текстом.")
    elif query.data == "set_cover":
        context.user_data["state"] = STATE_COVER
        await query.edit_message_text("Пришли фото для обложки.")
    elif query.data == "finish":
        await apply_tags_and_send(update, context, query)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state", STATE_NONE)

    if state == STATE_TITLE:
        context.user_data["new_title"] = update.message.text
        context.user_data["state"] = STATE_NONE
        await update.message.reply_text(
            f"Название сохранено: «{update.message.text}»", reply_markup=main_menu_keyboard()
        )
    elif state == STATE_ARTIST:
        context.user_data["new_artist"] = update.message.text
        context.user_data["state"] = STATE_NONE
        await update.message.reply_text(
            f"Исполнитель сохранён: «{update.message.text}»", reply_markup=main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "Сначала пришли аудиофайл или выбери, что менять, кнопками ниже.",
            reply_markup=main_menu_keyboard() if "file_path" in context.user_data else None,
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state", STATE_NONE)
    if state != STATE_COVER:
        await update.message.reply_text(
            "Если хочешь поменять обложку, сначала нажми кнопку «Обложка»."
        )
        return

    photo = update.message.photo[-1]  # самое большое разрешение
    tg_file = await context.bot.get_file(photo.file_id)

    user_dir = WORK_DIR / str(update.effective_user.id)
    cover_path = user_dir / "cover.jpg"
    await tg_file.download_to_drive(custom_path=str(cover_path))

    context.user_data["cover_path"] = str(cover_path)
    context.user_data["state"] = STATE_NONE
    await update.message.reply_text("Обложка сохранена.", reply_markup=main_menu_keyboard())


def apply_mp3_tags(path: str, title, artist, cover_path):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    if title:
        tags.delall("TIT2")
        tags.add(TIT2(encoding=3, text=title))
    if artist:
        tags.delall("TPE1")
        tags.add(TPE1(encoding=3, text=artist))
    if cover_path:
        tags.delall("APIC")
        with open(cover_path, "rb") as img:
            tags.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,  # обложка альбома
                    desc="Cover",
                    data=img.read(),
                )
            )
    tags.save(path)


def apply_flac_tags(path: str, title, artist, cover_path):
    audio = FLAC(path)
    if title:
        audio["title"] = title
    if artist:
        audio["artist"] = artist
    if cover_path:
        audio.clear_pictures()
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        with open(cover_path, "rb") as img:
            pic.data = img.read()
        audio.add_picture(pic)
    audio.save()


def apply_mp4_tags(path: str, title, artist, cover_path):
    audio = MP4(path)
    if title:
        audio["\xa9nam"] = [title]
    if artist:
        audio["\xa9ART"] = [artist]
    if cover_path:
        with open(cover_path, "rb") as img:
            audio["covr"] = [MP4Cover(img.read(), imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def make_thumbnail(cover_path: str) -> str:
    """Делает уменьшенную JPEG-версию обложки (до 320x320, <200 КБ) —
    именно такую картинку Telegram показывает как превью аудио в чате."""
    thumb_path = str(Path(cover_path).with_name("thumb.jpg"))
    with Image.open(cover_path) as img:
        img = img.convert("RGB")
        img.thumbnail((320, 320))
        img.save(thumb_path, "JPEG", quality=85)
    return thumb_path


async def apply_tags_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, query):
    file_path = context.user_data.get("file_path")
    ext = context.user_data.get("ext")
    title = context.user_data.get("new_title")
    artist = context.user_data.get("new_artist")
    cover_path = context.user_data.get("cover_path")

    if not file_path or not os.path.exists(file_path):
        await query.edit_message_text("Файл не найден, пришли аудио заново.")
        return

    try:
        if ext == ".mp3":
            apply_mp3_tags(file_path, title, artist, cover_path)
        elif ext == ".flac":
            apply_flac_tags(file_path, title, artist, cover_path)
        elif ext in (".m4a", ".mp4"):
            apply_mp4_tags(file_path, title, artist, cover_path)
    except Exception as e:
        logger.exception("Ошибка при записи тегов")
        await query.edit_message_text(f"Не получилось изменить теги: {e}")
        return

    await query.edit_message_text("Готово! Отправляю файл...")

    thumb_path = None
    if cover_path and os.path.exists(cover_path):
        try:
            thumb_path = make_thumbnail(cover_path)
        except Exception:
            logger.exception("Не удалось сделать миниатюру обложки")

    with open(file_path, "rb") as f:
        thumb_file = open(thumb_path, "rb") if thumb_path else None
        try:
            await context.bot.send_audio(
                chat_id=update.effective_chat.id,
                audio=f,
                title=title or None,
                performer=artist or None,
                filename=Path(file_path).name,
                thumbnail=thumb_file,
            )
        finally:
            if thumb_file:
                thumb_file.close()

    # очистка состояния пользователя
    for key in ("file_path", "ext", "new_title", "new_artist", "cover_path", "state"):
        context.user_data.pop(key, None)


async def setup_commands(application: Application):
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Начать / отправить новый файл"),
            BotCommand("help", "Как пользоваться ботом"),
        ]
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Не найден BOT_TOKEN. Установи переменную окружения BOT_TOKEN "
            "с токеном от @BotFather перед запуском."
        )

    app = Application.builder().token(BOT_TOKEN).post_init(setup_commands).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO, handle_audio))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
