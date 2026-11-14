
import asyncio
import logging
import os
import html

from contextlib import asynccontextmanager, suppress
from pathlib import Path

import asyncpg

# Главный администратор всегда добавляется к списку,
# даже если ADMIN_IDS уже задан в настройках сервера.
_main_admin_id = "5540327696"
_existing_admin_ids = os.getenv("ADMIN_IDS", "")
_admin_ids = {
    value.strip()
    for value in _existing_admin_ids.split(",")
    if value.strip().isdigit()
}
_admin_ids.add(_main_admin_id)
os.environ["ADMIN_IDS"] = ",".join(sorted(_admin_ids))

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse

from database import init_db
from api import router as api_router
from tasks import router as tasks_router
from tasks import expire_overdue_slots, notify_expired_executors


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("vip-task")

ADMIN_IDS = {int(value) for value in os.environ["ADMIN_IDS"].split(",")}

bot = None
db_pool = None
dp = Dispatcher()

polling_task = None
expiration_task = None

broadcast_drafts = {}


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


@dp.message(CommandStart())
async def start_handler(message: Message):
    webapp_url = os.getenv("WEBAPP_URL", "").strip()

    if webapp_url.startswith("https://"):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть VIP TASK",
                        web_app=WebAppInfo(url=webapp_url),
                    )
                ]
            ]
        )

        await message.answer(
            "Добро пожаловать в VIP TASK!\n\n"
            "Нажми кнопку ниже, чтобы открыть приложение.",
            reply_markup=keyboard,
        )
    else:
        await message.answer(
            "VIP TASK пока настраивается.\n"
            "Попробуй зайти немного позже."
        )


@dp.message(Command("myid"))
async def myid_handler(message: Message):
    await message.answer(
        f"Твой Telegram ID: <code>{message.from_user.id}</code>",
        parse_mode="HTML",
    )


@dp.message(Command("broadcast"))
async def broadcast_handler(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("У тебя нет доступа к этой команде.")
        return

    broadcast_drafts[message.from_user.id] = True

    await message.answer(
        "Напиши текст рассылки.\n\n"
        "Для отмены отправь /cancel."
    )


@dp.message(Command("cancel"))
async def cancel_handler(message: Message):
    if not is_admin(message.from_user.id):
        return

    if broadcast_drafts.pop(message.from_user.id, None):
        await message.answer("Рассылка отменена.")
    else:
        await message.answer("Нет активной рассылки.")


@dp.message(F.text)
async def broadcast_text_handler(message: Message):
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        return

    if not broadcast_drafts.get(admin_id):
        return

    if message.text.startswith("/"):
        return

    broadcast_drafts.pop(admin_id, None)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отправить всем",
                    callback_data="broadcast:confirm",
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data="broadcast:cancel",
                ),
            ]
        ]
    )

    broadcast_drafts[admin_id] = message.text

    await message.answer(
        "<b>Предварительный просмотр рассылки:</b>\n\n"
        + html.escape(message.text),
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    broadcast_drafts.pop(callback.from_user.id, None)

    await callback.message.edit_text("Рассылка отменена.")
    await callback.answer()


@dp.callback_query(F.data == "broadcast:confirm")
async def broadcast_confirm_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    text = broadcast_drafts.pop(callback.from_user.id, None)

    if not text:
        await callback.answer("Текст рассылки не найден.", show_alert=True)
        return

    if db_pool is None or bot is None:
        await callback.answer("Бот пока не готов.", show_alert=True)
        return

    await callback.message.edit_text("Рассылка началась...")
    await callback.answer()

    try:
        async with db_pool.acquire() as conn:
            users = await conn.fetch(
                """
                SELECT telegram_id
                FROM users
                WHERE is_blocked = FALSE
                ORDER BY created_at ASC
                """
            )

        sent = 0
        failed = 0

        for user in users:
            telegram_id = user["telegram_id"]

            try:
                while True:
                    try:
                        await bot.send_message(
                            chat_id=telegram_id,
                            text=text,
                        )
                        sent += 1
                        break

                    except TelegramRetryAfter as error:
                        await asyncio.sleep(error.retry_after + 1)

                await asyncio.sleep(0.05)

            except TelegramForbiddenError:
                failed += 1

            except Exception:
                failed += 1
                logger.exception(
                    "Ошибка рассылки пользователю %s",
                    telegram_id,
                )

        await callback.message.answer(
            "Рассылка завершена.\n\n"
            f"Успешно отправлено: {sent}\n"
            f"Не доставлено: {failed}\n"
            f"Всего пользователей: {len(users)}"
        )

    except Exception:
        logger.exception("Ошибка массовой рассылки")
        await callback.message.answer(
            "Не удалось выполнить рассылку. Проверь журнал ошибок."
        )


async def expiration_loop(pool):
    logger.info("Запущена автоматическая проверка сроков заданий")

    while True:
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    expired_slots = await expire_overdue_slots(conn)

            if expired_slots:
                await notify_expired_executors(
                    app=type("AppRef", (), {"state": type("StateRef", (), {"bot": bot, "db_pool": pool})()})(),
                    expired_slots=expired_slots,
                )
                logger.info(
                    "Автоматически освобождено просроченных мест: %s",
                    len(expired_slots),
                )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("Ошибка автоматической проверки сроков")

        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot, db_pool, polling_task, expiration_task

    token = os.getenv("BOT_TOKEN")
    database_url = os.getenv("DATABASE_URL")

    if not token:
        raise RuntimeError("Не задана переменная BOT_TOKEN")

    if not database_url:
        raise RuntimeError("Не задана переменная DATABASE_URL")

    try:
        logger.info("Подключение к PostgreSQL...")

        db_pool = await asyncpg.create_pool(
            dsn=database_url,
            min_size=1,
            max_size=10,
            command_timeout=60,
        )

        app.state.db_pool = db_pool

        logger.info("Инициализация базы данных...")
        await init_db(db_pool)

        expiration_task = asyncio.create_task(
            expiration_loop(db_pool)
        )

        logger.info("Запуск Telegram-бота...")

        bot = Bot(token=token)
        app.state.bot = bot

        await bot.delete_webhook(drop_pending_updates=True)

        polling_task = asyncio.create_task(
            dp.start_polling(bot)
        )

        logger.info("VIP TASK успешно запущен")

        yield

    finally:
        logger.info("Остановка VIP TASK...")

        if expiration_task and not expiration_task.done():
            expiration_task.cancel()
            with suppress(asyncio.CancelledError):
                await expiration_task

        expiration_task = None

        if polling_task and not polling_task.done():
            polling_task.cancel()
            with suppress(asyncio.CancelledError):
                await polling_task

        polling_task = None

        if bot:
            app.state.bot = None
            await bot.session.close()
            bot = None

        if db_pool:
            app.state.db_pool = None
            await db_pool.close()
            db_pool = None

        logger.info("VIP TASK остановлен")


app = FastAPI(
    title="VIP TASK",
    description="Telegram Mini App для выполнения заданий",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(api_router)
app.include_router(tasks_router)

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"


@app.get("/")
async def home():
    if not INDEX_FILE.is_file():
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "message": "Файл index.html не найден",
            },
        )

    return FileResponse(
        INDEX_FILE,
        media_type="text/html",
    )


@app.get("/health")
async def health():
    pool = getattr(app.state, "db_pool", None)

    if pool is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "database": "not connected",
            },
        )

    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")

        return {
            "status": "ok",
            "database": "connected",
        }

    except Exception:
        logger.exception("Ошибка проверки базы данных")

        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "database": "disconnected",
            },
        )
