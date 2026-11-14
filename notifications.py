
import logging
import os

logger = logging.getLogger("vip-task.notifications")

MAIN_ADMIN_ID = 5540327696

SETTING_BY_CATEGORY = {
    "task": "task_notifications",
    "wallet": "wallet_notifications",
    "review": "review_notifications",
    "system": "system_notifications",
}


def get_admin_ids() -> set[int]:
    result = {MAIN_ADMIN_ID}

    for value in os.getenv("ADMIN_IDS", "").split(","):
        value = value.strip()
        if value.isdigit():
            result.add(int(value))

    return result


async def notify_user(app, telegram_id: int, category: str, message: str):
    bot = getattr(app.state, "bot", None)
    pool = getattr(app.state, "db_pool", None)

    if bot is None or pool is None:
        logger.warning("Бот или база данных недоступны для уведомления")
        return False

    setting = SETTING_BY_CATEGORY.get(category)

    if setting is None:
        logger.warning("Неизвестная категория уведомления: %s", category)
        return False

    try:
        async with pool.acquire() as conn:
            enabled = await conn.fetchval(
                f"""
                SELECT {setting}
                FROM notification_settings
                WHERE telegram_id = $1
                """,
                telegram_id,
            )

        if enabled is False:
            return False

        await bot.send_message(
            chat_id=telegram_id,
            text=message,
        )

        return True

    except Exception:
        logger.exception(
            "Не удалось отправить уведомление пользователю %s",
            telegram_id,
        )
        return False


async def notify_admins(app, message: str):
    bot = getattr(app.state, "bot", None)

    if bot is None:
        logger.warning("Бот недоступен для уведомления администраторов")
        return 0

    sent = 0

    for admin_id in get_admin_ids():
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=message,
            )
            sent += 1

        except Exception:
            logger.exception(
                "Не удалось уведомить администратора %s",
                admin_id,
            )

    return sent
