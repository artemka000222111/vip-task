import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)

from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse

from database import init_db
from api import router as api_router
from tasks import router as tasks_router
from tasks import expire_overdue_slots


# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("vip-task")


# ==========================================
# BOT
# ==========================================

bot = None
dp = Dispatcher()

polling_task = None
expiration_task = None

# Черновики рассылок администраторов.
broadcast_drafts = {}


def get_admin_ids() -> set[int]:
    return {
        int(value.strip())
        for value in os.getenv("ADMIN_IDS", "").split(",")
        if value.strip().isdigit()
    }


def is_admin(telegram_id: int) -> bool:
    return telegram_id in get_admin_ids()


# ==========================================
# START COMMAND
# ==========================================

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


# ==========================================
# BROADCAST: START
# ==========================================

@dp.message(Command("broadcast"))
async def broadcast_start(message: Message):
    telegram_id = message.from_user.id

    if not is_admin(telegram_id):
        await message.answer("У тебя нет доступа к этой команде.")
        return

    broadcast_drafts.pop(telegram_id, None)

    await message.answer(
        "📢 <b>Создание рассылки VIP TASK</b>\n\n"
        "Отправь следующим сообщением текст, "
        "который нужно разослать пользователям.\n\n"
        "Для отмены напиши /cancel.",
        parse_mode="HTML",
    )


# ==========================================
# BROADCAST: CANCEL
# ==========================================

@dp.message(Command("cancel"))
async def broadcast_cancel(message: Message):
    telegram_id = message.from_user.id

    if telegram_id in broadcast_drafts:
        broadcast_drafts.pop(telegram_id, None)
        await message.answer("Рассылка отменена.")
    else:
        await message.answer("Нет активного черновика рассылки.")


# ==========================================
# BROADCAST: RECEIVE TEXT
# ==========================================

@dp.message(F.text)
async def broadcast_receive_text(message: Message):
    telegram_id = message.from_user.id

    if telegram_id not in broadcast_drafts and not is_admin(telegram_id):
        return

    if not is_admin(telegram_id):
        return

    if telegram_id not in broadcast_drafts:
        return

    text = message.text.strip()

    if text.startswith("/"):
        return

    if not text:
        await message.answer("Текст сообщения пустой. Отправь текст ещё раз.")
        return

    if len(text) > 4000:
        await message.answer(
            "Сообщение слишком длинное. Максимум — 4000 символов."
        )
        return

    broadcast_drafts[telegram_id] = text

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить рассылку",
                    callback_data="broadcast:confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data="broadcast:cancel",
                )
            ],
        ]
    )

    await message.answer(
        "📢 <b>Предварительный просмотр</b>\n\n"
        f"{text}\n\n"
        "Если всё правильно, подтверди рассылку.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


# ==========================================
# BROADCAST: SEND
# ==========================================

async def send_broadcast(
    telegram_bot: Bot,
    pool: asyncpg.Pool,
    text: str,
    admin_id: int,
) -> tuple[int, int]:

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT telegram_id
            FROM users
            WHERE is_blocked = FALSE
            ORDER BY created_at ASC
            """
        )

    successful = 0
    failed = 0

    total = len(rows)

    logger.info(
        "Начало рассылки: admin=%s, recipients=%s",
        admin_id,
        total,
    )

    for row in rows:
        user_id = int(row["telegram_id"])

        try:
            await telegram_bot.send_message(
                chat_id=user_id,
                text=text,
            )

            successful += 1

        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after + 1)

            try:
                await telegram_bot.send_message(
                    chat_id=user_id,
                    text=text,
                )
                successful += 1

            except Exception:
                failed += 1
                logger.warning(
                    "Не удалось отправить сообщение пользователю %s",
                    user_id,
                )

        except TelegramForbiddenError:
            failed += 1

        except Exception:
            failed += 1
            logger.exception(
                "Ошибка отправки рассылки пользователю %s",
                user_id,
            )

        # Не превышаем лимиты Telegram.
        await asyncio.sleep(0.05)

    logger.info(
        "Рассылка завершена: admin=%s, total=%s, success=%s, failed=%s",
        admin_id,
        total,
        successful,
        failed,
    )

    return successful, failed


# ==========================================
# BROADCAST: CONFIRM / CANCEL BUTTONS
# ==========================================

@dp.callback_query(F.data.startswith("broadcast:"))
async def broadcast_callback(callback: CallbackQuery):
    telegram_id = callback.from_user.id

    if not is_admin(telegram_id):
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    action = callback.data.split(":", 1)[1]

    if action == "cancel":
        broadcast_drafts.pop(telegram_id, None)

        await callback.answer("Рассылка отменена.")

        if callback.message:
            await callback.message.edit_text(
                "❌ Рассылка отменена."
            )

        return

    if action != "confirm":
        await callback.answer()
        return

    text = broadcast_drafts.get(telegram_id)

    if not text:
        await callback.answer(
            "Черновик не найден. Создай рассылку заново.",
            show_alert=True,
        )
        return

    pool = getattr(callback.bot, "db_pool", None)

    if pool is None:
        await callback.answer(
            "База данных недоступна.",
            show_alert=True,
        )
        return

    # Удаляем черновик до начала отправки,
    # чтобы исключить повторное нажатие.
    broadcast_drafts.pop(telegram_id, None)

    await callback.answer("Рассылка началась.")

    if callback.message:
        await callback.message.edit_text(
            "⏳ Рассылка началась.\n"
            "Дождись завершения отправки."
        )

    try:
        successful, failed = await send_broadcast(
            callback.bot,
            pool,
            text,
            telegram_id,
        )

        report = (
            "✅ <b>Рассылка завершена!</b>\n\n"
            f"Всего получателей: {successful + failed}\n"
            f"Успешно отправлено: {successful}\n"
            f"Не удалось отправить: {failed}"
        )

        await callback.bot.send_message(
            chat_id=telegram_id,
            text=report,
            parse_mode="HTML",
        )

    except Exception:
        logger.exception("Критическая ошибка рассылки")

        await callback.bot.send_message(
            chat_id=telegram_id,
            text=(
                "❌ Во время рассылки произошла ошибка. "
                "Подробности записаны в журнал сервера."
            ),
        )


# ==========================================
# AUTOMATIC TASK EXPIRATION
# ==========================================

async def expiration_loop(pool):
    logger.info(
        "Запущена автоматическая проверка сроков заданий"
    )

    while True:
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    expired_count = await expire_overdue_slots(conn)

            if expired_count:
                logger.info(
                    "Автоматически освобождено просроченных мест: %s",
                    expired_count,
                )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Ошибка автоматической проверки сроков"
            )

        await asyncio.sleep(30)


# ==========================================
# APPLICATION LIFESPAN
# ==========================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot, polling_task, expiration_task

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

        logger.info("Запуск автоматической проверки сроков...")

        expiration_task = asyncio.create_task(
            expiration_loop(pool)
        )

        logger.info("Запуск Telegram-бота...")

        bot = Bot(token=token)

        # Передаём пул базе данных обработчикам бота.
        bot.db_pool = pool

        await bot.delete_webhook(drop_pending_updates=True)

        polling_task = asyncio.create_task(
            dp.start_polling(bot)
        )

        logger.info("VIP TASK успешно запущен")

        yield

    finally:
        logger.info("Остановка VIP TASK...")

        broadcast_drafts.clear()

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
            await bot.session.close()
            bot = None

        if pool:
            await pool.close()

        logger.info("VIP TASK остановлен")


# ==========================================
# FASTAPI
# ==========================================

app = FastAPI(
    title="VIP TASK",
    description="Telegram Mini App для выполнения заданий",
    version="1.0.0",
    lifespan=lifespan,
)


# ==========================================
# ROUTERS
# ==========================================

app.include_router(api_router)
app.include_router(tasks_router)


# ==========================================
# MINI APP
# ==========================================

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


# ==========================================
# HEALTH CHECK
# ==========================================

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
        logger.exception(
            "Ошибка проверки базы данных"
        )

        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "database": "disconnected",
            },
        )
