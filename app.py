
import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from database import init_db
from api import router as api_router
from tasks import router as tasks_router


# -----------------------------
# LOGGING
# -----------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("vip-task")


# -----------------------------
# BOT
# -----------------------------

bot = None
dp = Dispatcher()
polling_task = None


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


# -----------------------------
# APPLICATION LIFESPAN
# -----------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot, polling_task

    token = os.getenv("BOT_TOKEN")
    database_url = os.getenv("DATABASE_URL")

    if not token:
        raise RuntimeError("Не задана переменная BOT_TOKEN")

    if not database_url:
        raise RuntimeError("Не задана переменная DATABASE_URL")

    pool = None

    try:
        logger.info("Подключение к PostgreSQL...")

        pool = await asyncpg.create_pool(
            dsn=database_url,
            min_size=1,
            max_size=10,
            command_timeout=60,
        )

        app.state.db_pool = pool

        logger.info("Инициализация базы данных...")

        await init_db(pool)

        logger.info("Запуск Telegram-бота...")

        bot = Bot(token=token)

        await bot.delete_webhook(drop_pending_updates=True)

        polling_task = asyncio.create_task(
            dp.start_polling(bot)
        )

        logger.info("VIP TASK успешно запущен")

        yield

    finally:
        logger.info("Остановка VIP TASK...")

        if polling_task and not polling_task.done():
            polling_task.cancel()

            with suppress(asyncio.CancelledError):
                await polling_task

        polling_task = None

        if bot:
            await bot.session.close()
            bot = None

        if pool:
            await pool.close()

        logger.info("VIP TASK остановлен")


# -----------------------------
# FASTAPI
# -----------------------------

app = FastAPI(
    title="VIP TASK",
    description="Telegram Mini App для выполнения заданий",
    version="1.0.0",
    lifespan=lifespan,
)


# -----------------------------
# ROUTERS
# -----------------------------

app.include_router(api_router)
app.include_router(tasks_router)


# -----------------------------
# MAIN PAGE
# -----------------------------

@app.get("/")
async def home():
    return {
        "app": "VIP TASK",
        "status": "online",
        "message": "VIP TASK API работает",
    }


# -----------------------------
# HEALTH CHECK
# -----------------------------

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
