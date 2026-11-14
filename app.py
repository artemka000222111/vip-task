
import asyncio
import logging
import os
from contextlib import asynccontextmanager

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

from database import init_db
from api import router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vip-task")

bot = None
dp = Dispatcher()
db_pool = None
polling_task = None


@dp.message(CommandStart())
async def start_handler(message: Message):
    webapp_url = os.getenv("WEBAPP_URL")

    if webapp_url:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="Открыть VIP TASK",
                    web_app=WebAppInfo(url=webapp_url),
                )
            ]]
        )
        await message.answer(
            "Добро пожаловать в VIP TASK!",
            reply_markup=keyboard,
        )
    else:
        await message.answer(
            "VIP TASK настраивается. Скоро приложение будет доступно."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot, db_pool, polling_task

    token = os.getenv("BOT_TOKEN")
    database_url = os.getenv("DATABASE_URL")

    if not token:
        raise RuntimeError("Не задан BOT_TOKEN")

    if not database_url:
        raise RuntimeError("Не задан DATABASE_URL")

    db_pool = await asyncpg.create_pool(
        dsn=database_url,
        min_size=1,
        max_size=10,
    )

    await init_db(db_pool)

    app.state.db_pool = db_pool

    bot = Bot(token=token)
    await bot.delete_webhook(drop_pending_updates=True)

    polling_task = asyncio.create_task(dp.start_polling(bot))

    logger.info("VIP TASK запущен")

    try:
        yield
    finally:
        if polling_task:
            polling_task.cancel()
            try:
                await polling_task
            except asyncio.CancelledError:
                pass

        if bot:
            await bot.session.close()

        if db_pool:
            await db_pool.close()

        logger.info("VIP TASK остановлен")


app = FastAPI(
    title="VIP TASK",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/")
async def home():
    return {
        "service": "VIP TASK",
        "status": "running",
    }


@app.get("/health")
async def health():
    if db_pool is None:
        return {
            "status": "error",
            "database": "not initialized",
        }

    async with db_pool.acquire() as connection:
        await connection.fetchval("SELECT 1")

    return {
        "status": "ok",
        "database": "connected",
    }

