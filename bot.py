"""
Telegram-бот для приёма задач и постановки их в очередь Supabase.
Деплоится на Railway.

Переменные окружения (задать в Railway -> Variables):
    BOT_TOKEN=токен от BotFather
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY=service_role ключ (не anon!)

requirements.txt:
    python-telegram-bot==21.4
    supabase==2.7.4
"""

import os
import logging
import mimetypes
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from supabase import create_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
STORAGE_BUCKET = "task-images"

# сколько раз подряд опросили статус задачи -> когда бросить ждать
MAX_WAIT_CHECKS = 60  # при интервале 5 сек = 5 минут максимум ожидания


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Со мной можно и просто поговорить, и дать задачу для компьютера, например:\n"
        "\"открой папку Новая папка на рабочем столе, найди Rhino 7 и перемести на рабочий стол\"\n\n"
        "Можешь также прислать ФОТО (например референс украшения) с подписью, что с ним сделать —\n"
        "я передам картинку агенту, и он будет смотреть на неё напрямую.\n\n"
        "Пиши что угодно."
    )


async def handle_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_text = update.message.text
    chat_id = update.effective_chat.id

    insert_resp = (
        supabase.table("agent_tasks")
        .insert({"chat_id": chat_id, "task_text": task_text, "status": "pending"})
        .execute()
    )
    task_id = insert_resp.data[0]["id"]

    await update.message.reply_text("...")

    context.job_queue.run_repeating(
        check_task_status,
        interval=5,
        first=5,
        data={"task_id": task_id, "chat_id": chat_id, "checks": 0},
        name=f"task_{task_id}",
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    task_text = update.message.caption or "Посмотри на присланное фото и опиши, что на нём"

    photo = update.message.photo[-1]  # самое большое разрешение
    tg_file = await context.bot.get_file(photo.file_id)
    photo_bytes = await tg_file.download_as_bytearray()

    file_name = f"{chat_id}_{photo.file_id}.jpg"
    supabase.storage.from_(STORAGE_BUCKET).upload(
        file_name, bytes(photo_bytes), {"content-type": "image/jpeg"}
    )
    image_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(file_name)

    insert_resp = (
        supabase.table("agent_tasks")
        .insert({"chat_id": chat_id, "task_text": task_text, "image_url": image_url, "status": "pending"})
        .execute()
    )
    task_id = insert_resp.data[0]["id"]

    await update.message.reply_text("...")

    context.job_queue.run_repeating(
        check_task_status,
        interval=5,
        first=5,
        data={"task_id": task_id, "chat_id": chat_id, "checks": 0},
        name=f"task_{task_id}",
    )


async def check_task_status(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    task_id = job.data["task_id"]
    chat_id = job.data["chat_id"]
    job.data["checks"] += 1

    resp = supabase.table("agent_tasks").select("status,result").eq("id", task_id).execute()
    if not resp.data:
        job.schedule_removal()
        return

    row = resp.data[0]
    status = row["status"]

    if status == "done":
        await context.bot.send_message(chat_id, row["result"])
        supabase.table("agent_tasks").delete().eq("id", task_id).execute()
        job.schedule_removal()
    elif status == "error":
        await context.bot.send_message(chat_id, f"Ошибка при выполнении:\n{row['result']}")
        supabase.table("agent_tasks").delete().eq("id", task_id).execute()
        job.schedule_removal()
    elif job.data["checks"] >= MAX_WAIT_CHECKS:
        await context.bot.send_message(
            chat_id,
            "Агент на компьютере не отвечает слишком долго. Проверь, запущен ли local_agent.py.",
        )
        supabase.table("agent_tasks").delete().eq("id", task_id).execute()
        job.schedule_removal()


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task))
    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
