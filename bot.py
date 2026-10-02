"""
Telegram-бот для редактирования тегов аудио (название, исполнитель, обложка).

Поддерживаемые форматы: MP3, FLAC, M4A/MP4.

Установка:
    pip install -r requirements.txt

Запуск:
    export BOT_TOKEN="твой_токен_от_BotFather"
    python bot.py

Токен получить у @BotFather в Telegram командой /newbot.
"""

import os
import asyncio
import logging
import subprocess
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    PicklePersistence,
)

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, APIC, ID3NoHeaderError
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image
import httpx
import imageio_ffmpeg

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
AUDD_API_TOKEN = os.environ.get("AUDD_API_TOKEN", "")  # ключ с audd.io, для распознавания треков
ADMIN_ID = os.environ.get("ADMIN_ID", "")  # твой Telegram user id — для /broadcast
WORK_DIR = Path("bot_files")
WORK_DIR.mkdir(exist_ok=True)
PERSISTENCE_FILE = "bot_persistence.pkl"
VIDEO_NOTE_MAX_SECONDS = 60  # ограничение самого Telegram для кружков

# Состояния, которые бот ждёт от пользователя дальше
STATE_NONE = "none"
STATE_TITLE = "title"
STATE_ARTIST = "artist"
STATE_COVER = "cover"
STATE_SET_CHANNEL = "set_channel"


def remember_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запоминает user_id в общем хранилище бота, чтобы потом можно было
    сделать рассылку всем, кто хоть раз писал боту."""
    if not update.effective_user:
        return
    known = context.bot_data.setdefault("known_users", set())
    known.add(update.effective_user.id)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("✏️ Название", callback_data="set_title"),
            InlineKeyboardButton("🎤 Исполнитель", callback_data="set_artist"),
        ],
        [InlineKeyboardButton("🖼 Обложка", callback_data="set_cover")],
    ]
    if AUDD_API_TOKEN:
        buttons.append(
            [InlineKeyboardButton("🔍 Определить трек по звуку", callback_data="recognize")]
        )
    buttons.append([InlineKeyboardButton("✅ Готово, отправить файл", callback_data="finish")])
    return InlineKeyboardMarkup(buttons)


async def recognize_track(file_path: str) -> dict | None:
    """Отправляет аудио в AudD.io и возвращает {'title':..., 'artist':...} или None."""
    if not AUDD_API_TOKEN:
        return None
    async with httpx.AsyncClient(timeout=30) as client:
        with open(file_path, "rb") as f:
            response = await client.post(
                "https://api.audd.io/",
                data={"api_token": AUDD_API_TOKEN, "return": ""},
                files={"file": f},
            )
    data = response.json()
    result = data.get("result")
    if not result:
        return None
    return {"title": result.get("title"), "artist": result.get("artist")}


async def convert_to_video_note(input_path: str) -> str:
    """Обрезает видео до квадрата и максимум 60 секунд, кодирует под кружок."""
    output_path = str(Path(input_path).with_name("video_note.mp4"))
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg, "-y", "-i", input_path,
        "-t", str(VIDEO_NOTE_MAX_SECONDS),
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=480:480",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        output_path,
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore")[-500:])
    return output_path


async def extract_audio(input_path: str) -> str:
    """Вытаскивает звуковую дорожку из видео/кружка в mp3."""
    output_path = str(Path(input_path).with_name("extracted_audio.mp3"))
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg, "-y", "-i", input_path,
        "-vn",  # без видео
        "-c:a", "libmp3lame", "-q:a", "2",
        output_path,
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore")[-500:])
    return output_path


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update, context)
    video = update.message.video or update.message.document
    if video is None:
        return

    user_dir = WORK_DIR / str(update.effective_user.id)
    user_dir.mkdir(exist_ok=True)
    input_path = user_dir / "input_video.mp4"

    tg_file = await context.bot.get_file(video.file_id)
    await tg_file.download_to_drive(custom_path=str(input_path))
    context.user_data["video_path"] = str(input_path)

    await update.message.reply_text(
        "Видео получено! Что сделать?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔵 Сделать кружок", callback_data="video_to_circle")],
                [InlineKeyboardButton("🎵 Извлечь аудио", callback_data="video_extract_audio")],
            ]
        ),
    )


async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update, context)
    video_note = update.message.video_note
    if video_note is None:
        return

    status = await update.message.reply_text("Извлекаю звук из кружка...")

    user_dir = WORK_DIR / str(update.effective_user.id)
    user_dir.mkdir(exist_ok=True)
    input_path = user_dir / "input_circle.mp4"

    tg_file = await context.bot.get_file(video_note.file_id)
    await tg_file.download_to_drive(custom_path=str(input_path))

    try:
        output_path = await extract_audio(str(input_path))
    except Exception as e:
        logger.exception("Ошибка извлечения звука из кружка")
        await status.edit_text(f"Не получилось извлечь звук: {e}")
        return

    await status.edit_text("Готово! Отправляю аудио...")
    with open(output_path, "rb") as f:
        await context.bot.send_audio(chat_id=update.effective_chat.id, audio=f, filename="audio.mp3")


async def handle_video_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, query):
    input_path = context.user_data.get("video_path")
    if not input_path or not os.path.exists(input_path):
        await query.edit_message_text("Видео не найдено, пришли заново.")
        return

    if query.data == "video_to_circle":
        await query.edit_message_text("Делаю кружок, подожди немного...")
        try:
            output_path = await convert_to_video_note(input_path)
        except Exception as e:
            logger.exception("Ошибка конвертации видео в кружок")
            await query.edit_message_text(
                "Не получилось сделать кружок. Проверь, что видео не слишком "
                f"тяжёлое и в обычном формате.\n\nОшибка: {e}"
            )
            return
        await query.edit_message_text("Готово! Отправляю кружок...")
        with open(output_path, "rb") as f:
            await context.bot.send_video_note(chat_id=update.effective_chat.id, video_note=f)

    elif query.data == "video_extract_audio":
        await query.edit_message_text("Извлекаю звук из видео...")
        try:
            output_path = await extract_audio(input_path)
        except Exception as e:
            logger.exception("Ошибка извлечения звука из видео")
            await query.edit_message_text(f"Не получилось извлечь звук: {e}")
            return
        await query.edit_message_text("Готово! Отправляю аудио...")
        with open(output_path, "rb") as f:
            await context.bot.send_audio(
                chat_id=update.effective_chat.id, audio=f, filename="audio.mp3"
            )

    context.user_data.pop("video_path", None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update, context)
    await update.message.reply_text(
        "Привет! Пришли мне аудиофайл (mp3, flac или m4a) — помогу поменять "
        "название, исполнителя и обложку.\n\n"
        "Или пришли видео/кружок — сделаю из видео кружок, либо вытащу звук "
        "из видео или кружка отдельным файлом."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update, context)
    await update.message.reply_text(
        "Как пользоваться:\n"
        "1. Пришли аудиофайл (mp3, flac или m4a)\n"
        "2. Кнопками выбери, что поменять: название, исполнителя, обложку\n"
        "3. Пришли новое значение (текст или фото)\n"
        "4. Нажми «Готово» — получишь файл с новыми тегами\n\n"
        "Пришли видео — предложу сделать кружок или вытащить из него звук "
        "(кружок — максимум 60 секунд, автоматически обрежется до квадрата).\n"
        "Пришли готовый кружок — сразу вытащу из него звук отдельным файлом.\n\n"
        "Чтобы можно было постить сразу на свой канал:\n"
        "/setchannel — привязать канал\n"
        "/mychannel — посмотреть, какой канал сейчас привязан\n\n"
        "/cancel — отменить текущую операцию"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    had_file = "file_path" in context.user_data
    for key in (
        "file_path", "ext", "new_title", "new_artist", "cover_path",
        "state", "last_file_id", "last_title", "last_artist",
        "original_title", "original_artist",
    ):
        context.user_data.pop(key, None)
    if had_file:
        await update.message.reply_text("Отменено. Можешь прислать новый файл.")
    else:
        await update.message.reply_text("Отменять нечего, но состояние на всякий случай сброшено.")


async def cmd_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = STATE_SET_CHANNEL
    await update.message.reply_text(
        "1. Добавь этого бота администратором в свой канал (с правом публикации постов)\n"
        "2. Перешли мне сюда любое сообщение из этого канала — я запомню его"
    )


async def cmd_mychannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_title = context.user_data.get("channel_title")
    if channel_title:
        await update.message.reply_text(f"Привязан канал: {channel_title}")
    else:
        await update.message.reply_text(
            "Канал ещё не привязан. Используй /setchannel, чтобы привязать."
        )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or str(update.effective_user.id) != str(ADMIN_ID):
        return  # не админ — тихо игнорируем, чтобы не палить наличие команды

    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Использование: /broadcast текст сообщения")
        return

    known_users = context.bot_data.get("known_users", set())
    if not known_users:
        await update.message.reply_text("Пока нет ни одного известного пользователя.")
        return

    sent, failed = 0, 0
    status = await update.message.reply_text(f"Рассылаю {len(known_users)} пользователям...")
    for user_id in list(known_users):
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # чтобы не упереться в лимиты Telegram

    await status.edit_text(f"Готово: доставлено {sent}, не доставлено {failed}.")


async def handle_forwarded_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") != STATE_SET_CHANNEL:
        return  # не в процессе привязки канала — игнорируем

    origin = update.message.forward_origin
    if origin is None or origin.type != "channel":
        await update.message.reply_text(
            "Это не похоже на пересланное сообщение из канала. Попробуй ещё раз."
        )
        return

    chat = origin.chat

    try:
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if member.status not in ("administrator", "creator"):
            raise ValueError("not admin")
    except Exception:
        await update.message.reply_text(
            "Не вижу бота среди администраторов этого канала. "
            "Добавь его туда с правом публикации и попробуй снова."
        )
        return

    context.user_data["channel_id"] = chat.id
    context.user_data["channel_title"] = chat.title
    context.user_data["state"] = STATE_NONE
    await update.message.reply_text(f"Готово! Канал «{chat.title}» привязан ✅")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update, context)
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

    # Запоминаем оригинальные название/исполнителя (Telegram хранит их отдельно
    # от самого файла), чтобы не терять их, если пользователь меняет только обложку
    orig_title = None
    orig_artist = None
    if update.message.audio:
        orig_title = update.message.audio.title
        orig_artist = update.message.audio.performer

    context.user_data["file_path"] = str(local_path)
    context.user_data["ext"] = ext
    context.user_data["state"] = STATE_NONE
    context.user_data["new_title"] = None
    context.user_data["new_artist"] = None
    context.user_data["cover_path"] = None
    context.user_data["original_title"] = orig_title
    context.user_data["original_artist"] = orig_artist

    await update.message.reply_text(
        "Файл получен! Что меняем?", reply_markup=main_menu_keyboard()
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    no_file_ok = query.data in ("send_channel", "video_to_circle", "video_extract_audio")
    if "file_path" not in context.user_data and not no_file_ok:
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
    elif query.data == "send_channel":
        await send_to_channel(update, context, query)
    elif query.data == "recognize":
        await handle_recognize(update, context, query)
    elif query.data in ("video_to_circle", "video_extract_audio"):
        await handle_video_choice(update, context, query)


async def handle_recognize(update: Update, context: ContextTypes.DEFAULT_TYPE, query):
    file_path = context.user_data.get("file_path")
    if not file_path or not os.path.exists(file_path):
        await query.edit_message_text("Файл не найден, пришли аудио заново.")
        return

    await query.edit_message_text("Слушаю трек, определяю...")
    try:
        result = await recognize_track(file_path)
    except Exception as e:
        logger.exception("Ошибка распознавания трека")
        await query.edit_message_text(
            f"Не получилось распознать трек: {e}", reply_markup=main_menu_keyboard()
        )
        return

    if not result:
        await query.edit_message_text(
            "Не удалось распознать этот трек. Можешь ввести название и исполнителя вручную.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if result.get("title"):
        context.user_data["new_title"] = result["title"]
    if result.get("artist"):
        context.user_data["new_artist"] = result["artist"]

    await query.edit_message_text(
        f"Похоже, это:\n«{result.get('title')}» — {result.get('artist')}\n\n"
        "Уже подставил в название и исполнителя. Можешь поправить или нажать «Готово».",
        reply_markup=main_menu_keyboard(),
    )


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


async def send_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE, query):
    file_id = context.user_data.get("last_file_id")
    channel_id = context.user_data.get("channel_id")

    if not file_id:
        await query.edit_message_text("Файл не найден, пришли аудио заново.")
        return
    if not channel_id:
        await query.edit_message_text(
            "У тебя ещё не привязан канал. Используй /setchannel, чтобы привязать его."
        )
        return

    try:
        await context.bot.send_audio(
            chat_id=channel_id,
            audio=file_id,
            title=context.user_data.get("last_title") or None,
            performer=context.user_data.get("last_artist") or None,
        )
        await query.edit_message_text("✅ Отправлено на твой канал!")
    except Exception as e:
        logger.exception("Ошибка при отправке в канал")
        await query.edit_message_text(
            f"Не получилось отправить в канал: {e}\n"
            "Проверь, что бот всё ещё администратор канала."
        )


async def apply_tags_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, query):
    file_path = context.user_data.get("file_path")
    ext = context.user_data.get("ext")
    cover_path = context.user_data.get("cover_path")

    # Если пользователь не менял название/исполнителя — сохраняем оригинальные
    # значения (Telegram хранит их отдельно от самого файла), а не теряем их
    title = context.user_data.get("new_title") or context.user_data.get("original_title")
    artist = context.user_data.get("new_artist") or context.user_data.get("original_artist")

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
            sent_message = await context.bot.send_audio(
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

    if context.user_data.get("channel_id"):
        # сохраняем file_id готового файла, чтобы отправить его в канал без
        # повторной загрузки, если пользователь нажмёт кнопку ниже
        context.user_data["last_file_id"] = sent_message.audio.file_id
        context.user_data["last_title"] = title
        context.user_data["last_artist"] = artist
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Запостить этот файл на твой канал?",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📢 Отправить на канал", callback_data="send_channel")]]
            ),
        )

    # очистка состояния пользователя (кроме last_* — они нужны для кнопки канала)
    for key in (
        "file_path", "ext", "new_title", "new_artist", "cover_path", "state",
        "original_title", "original_artist",
    ):
        context.user_data.pop(key, None)


async def setup_commands(application: Application):
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Начать / отправить новый файл"),
            BotCommand("help", "Как пользоваться ботом"),
            BotCommand("cancel", "Отменить текущую операцию"),
            BotCommand("setchannel", "Привязать свой канал"),
            BotCommand("mychannel", "Какой канал привязан сейчас"),
        ]
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Не найден BOT_TOKEN. Установи переменную окружения BOT_TOKEN "
            "с токеном от @BotFather перед запуском."
        )

    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(setup_commands)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("setchannel", cmd_setchannel))
    app.add_handler(CommandHandler("mychannel", cmd_mychannel))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_note))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded_channel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    webhook_url = os.environ.get("WEBHOOK_URL", "").rstrip("/")
    if webhook_url:
        port = int(os.environ.get("PORT", 8080))
        logger.info("Бот запущен через webhook: %s", webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=BOT_TOKEN,
            webhook_url=f"{webhook_url}/{BOT_TOKEN}",
            secret_token=os.environ.get("WEBHOOK_SECRET") or None,
        )
    else:
        logger.info("Бот запущен через polling (WEBHOOK_URL не задан)...")
        app.run_polling()


if __name__ == "__main__":
    main()
