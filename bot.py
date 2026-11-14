import os
import asyncio
import asyncpg

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage


BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

db = None


class CreateTask(StatesGroup):
    title = State()
    description = State()
    price = State()


class SubmitTask(StatesGroup):
    proof = State()


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Задания"),
             KeyboardButton(text="➕ Создать")],
            [KeyboardButton(text="💰 Баланс"),
             KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="💳 Пополнить"),
             KeyboardButton(text="💸 Вывести")],
            [KeyboardButton(text="🔄 Режим")]
        ],
        resize_keyboard=True
    )


async def init_db():
    global db
    db = await asyncpg.create_pool(DATABASE_URL)

    async with db.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                username TEXT,
                role TEXT DEFAULT 'executor',
                balance NUMERIC(12,2) DEFAULT 0,
                reserved NUMERIC(12,2) DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                customer_id BIGINT REFERENCES users(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                price NUMERIC(12,2) NOT NULL,
                status TEXT DEFAULT 'open',
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id SERIAL PRIMARY KEY,
                task_id INTEGER REFERENCES tasks(id),
                executor_id BIGINT REFERENCES users(id),
                proof TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(task_id, executor_id)
            );

            CREATE TABLE IF NOT EXISTS money_requests (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(id),
                amount NUMERIC(12,2) NOT NULL,
                request_type TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(id),
                amount NUMERIC(12,2) NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)


async def register_user(message: Message):
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (id, username)
            VALUES ($1, $2)
            ON CONFLICT (id)
            DO UPDATE SET username = EXCLUDED.username
        """, message.from_user.id, message.from_user.username)


async def get_user(user_id):
    async with db.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE id=$1", user_id
        )


@router.message(CommandStart())
async def start(message: Message):
    await register_user(message)
    await message.answer(
        "👋 Добро пожаловать в VIP TASK!\n\n"
        "Здесь можно создавать задания и зарабатывать.",
        reply_markup=main_menu()
    )


@router.message(F.text == "👤 Профиль")
async def profile(message: Message):
    user = await get_user(message.from_user.id)

    await message.answer(
        f"👤 Твой профиль\n\n"
        f"ID: {user['id']}\n"
        f"Режим: {user['role']}\n"
        f"Баланс: {user['balance']} ₽\n"
        f"В резерве: {user['reserved']} ₽"
    )


@router.message(F.text == "💰 Баланс")
async def balance(message: Message):
    user = await get_user(message.from_user.id)

    await message.answer(
        f"💰 Баланс: {user['balance']} ₽\n"
        f"🔒 В резерве: {user['reserved']} ₽"
    )


@router.message(F.text == "🔄 Режим")
async def change_role(message: Message):
    async with db.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT role FROM users WHERE id=$1",
            message.from_user.id
        )

        new_role = (
            "customer" if user["role"] == "executor"
            else "executor"
        )

        await conn.execute(
            "UPDATE users SET role=$1 WHERE id=$2",
            new_role, message.from_user.id
        )

    name = "Заказчик" if new_role == "customer" else "Исполнитель"

    await message.answer(f"🔄 Теперь ты в режиме: {name}")


@router.message(F.text == "➕ Создать")
async def create_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)

    if user["role"] != "customer":
        await message.answer(
            "Сначала переключись в режим заказчика."
        )
        return

    await state.set_state(CreateTask.title)
    await message.answer("Напиши название задания:")


@router.message(CreateTask.title)
async def create_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(CreateTask.description)
    await message.answer("Опиши задание подробнее:")


@router.message(CreateTask.description)
async def create_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(CreateTask.price)
    await message.answer("Укажи оплату за выполнение в рублях:")


@router.message(CreateTask.price)
async def create_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", "."))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Укажи корректную сумму больше нуля.")
        return

    data = await state.get_data()
    await state.clear()

    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO tasks
            (customer_id, title, description, price)
            VALUES ($1, $2, $3, $4)
        """,
        message.from_user.id,
        data["title"],
        data["description"],
        price)

    await message.answer(
        "✅ Задание создано!\n"
        "Перед публикацией необходимо будет пополнить баланс "
        "и зарезервировать оплату."
    )


@router.message(F.text == "📋 Задания")
async def show_tasks(message: Message):
    async with db.acquire() as conn:
        tasks = await conn.fetch("""
            SELECT * FROM tasks
            WHERE status='open'
            ORDER BY id DESC
            LIMIT 10
        """)

    if not tasks:
        await message.answer("Пока открытых заданий нет.")
        return

    for task in tasks:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Выполнить",
                callback_data=f"task_{task['id']}"
            )]
        ])

        await message.answer(
            f"📌 {task['title']}\n\n"
            f"{task['description']}\n\n"
            f"💰 Оплата: {task['price']} ₽",
            reply_markup=keyboard
        )


@router.callback_query(F.data.startswith("task_"))
async def take_task(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[1])

    async with db.acquire() as conn:
        task = await conn.fetchrow(
            "SELECT * FROM tasks WHERE id=$1 AND status='open'",
            task_id
        )

        if not task:
            await callback.answer("Задание уже недоступно.", show_alert=True)
            return

        if task["customer_id"] == callback.from_user.id:
            await callback.answer("Нельзя выполнять своё задание.", show_alert=True)
            return

        existing = await conn.fetchrow("""
            SELECT id FROM submissions
            WHERE task_id=$1 AND executor_id=$2
        """, task_id, callback.from_user.id)

        if existing:
            await callback.answer("Ты уже отправлял отклик.", show_alert=True)
            return

    await state.update_data(task_id=task_id)
    await state.set_state(SubmitTask.proof)

    await callback.message.answer(
        "Отправь доказательство выполнения задания "
        "(текст или ссылку)."
    )
    await callback.answer()


@router.message(SubmitTask.proof)
async def submit_proof(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]

    async with db.acquire() as conn:
        task = await conn.fetchrow(
            "SELECT * FROM tasks WHERE id=$1 AND status='open'",
            task_id
        )

        if not task:
            await state.clear()
            await message.answer("Задание больше недоступно.")
            return

        try:
            await conn.execute("""
                INSERT INTO submissions
                (task_id, executor_id, proof)
                VALUES ($1, $2, $3)
            """, task_id, message.from_user.id, message.text or "[медиа]")
        except asyncpg.UniqueViolationError:
            await state.clear()
            await message.answer("Отклик уже отправлен.")
            return

    await state.clear()
    await message.answer(
        "✅ Отклик отправлен!\n"
        "После проверки заказчиком будет принято решение."
    )

    for admin_id in ADMIN_IDS:
        await bot.send_message(
            admin_id,
            f"Новый отклик на задание №{task_id}.\n"
            f"Исполнитель: {message.from_user.id}\n"
            f"Доказательство: {message.text or 'Медиа'}"
        )


@router.message(F.text == "💳 Пополнить")
async def topup(message: Message):
    await message.answer(
        "💳 Минимальное пополнение — 10 ₽.\n\n"
        "Пополнение проводится вручную через администратора.\n"
        "Напиши в поддержку для получения реквизитов."
    )


@router.message(F.text == "💸 Вывести")
async def withdraw(message: Message):
    await message.answer(
        "💸 Вывод средств проводится вручную через администратора.\n\n"
        "Комиссия платформы — 10% при выводе.\n"
        "Порядок оформления заявки будет добавлен после "
        "окончательного согласования минимальной суммы вывода."
    )


@router.message(Command("requests"))
async def admin_requests(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    async with db.acquire() as conn:
        requests = await conn.fetch("""
            SELECT * FROM submissions
            WHERE status='pending'
            ORDER BY id
        """)

    if not requests:
        await message.answer("Новых откликов нет.")
        return

    for item in requests:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Одобрить",
                    callback_data=f"approve_{item['id']}"
                ),
                InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=f"reject_{item['id']}"
                )
            ]
        ])

        await message.answer(
            f"Отклик №{item['id']}\n"
            f"Задание №{item['task_id']}\n"
            f"Исполнитель: {item['executor_id']}\n"
            f"Доказательство: {item['proof']}",
            reply_markup=keyboard
        )


@router.callback_query(F.data.startswith("approve_"))
async def approve_submission(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    submission_id = int(callback.data.split("_")[1])

    await callback.answer(
        "Проверка финансовой части ещё не подключена.",
        show_alert=True
    )


@router.callback_query(F.data.startswith("reject_"))
async def reject_submission(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    submission_id = int(callback.data.split("_")[1])

    async with db.acquire() as conn:
        await conn.execute("""
            UPDATE submissions
            SET status='rejected'
            WHERE id=$1 AND status='pending'
        """, submission_id)

    await callback.message.answer(
        f"Отклик №{submission_id} отклонён."
    )
    await callback.answer()


async def main():
    if not BOT_TOKEN or not DATABASE_URL:
        raise RuntimeError(
            "Не заданы BOT_TOKEN или DATABASE_URL в переменных окружения."
        )

    await init_db()
    await dp.start_polling(bot)
await bot.delete_webhook(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
