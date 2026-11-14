
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from auth import verify_telegram_init_data


router = APIRouter(prefix="/api", tags=["api"])


# --------------------------------------------------
# АВТОРИЗАЦИЯ И ПОЛЬЗОВАТЕЛЬ
# --------------------------------------------------

async def get_user(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data")

    try:
        user = verify_telegram_init_data(init_data)
    except ValueError as error:
        raise HTTPException(status_code=401, detail=str(error))

    telegram_id = int(user["id"])
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO users (telegram_id, username, first_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (telegram_id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    updated_at = NOW()
                """,
                telegram_id,
                user.get("username"),
                user.get("first_name", ""),
            )

            await conn.execute(
                """
                INSERT INTO wallets (telegram_id)
                VALUES ($1)
                ON CONFLICT (telegram_id) DO NOTHING
                """,
                telegram_id,
            )

            await conn.execute(
                """
                INSERT INTO notification_settings (telegram_id)
                VALUES ($1)
                ON CONFLICT (telegram_id) DO NOTHING
                """,
                telegram_id,
            )

            blocked = await conn.fetchval(
                "SELECT is_blocked FROM users WHERE telegram_id = $1",
                telegram_id,
            )

    if blocked:
        raise HTTPException(
            status_code=403,
            detail="Аккаунт заблокирован",
        )

    return user


# --------------------------------------------------
# ПРОФИЛЬ И БАЛАНС
# --------------------------------------------------

@router.get("/me")
async def get_profile(request: Request):
    user = await get_user(request)
    telegram_id = int(user["id"])
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        wallet = await conn.fetchrow(
            """
            SELECT available_kopecks, reserved_kopecks
            FROM wallets
            WHERE telegram_id = $1
            """,
            telegram_id,
        )

        user_row = await conn.fetchrow(
            """
            SELECT created_at
            FROM users
            WHERE telegram_id = $1
            """,
            telegram_id,
        )

        task_stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE customer_id = $1
                ) AS created_tasks
            FROM tasks
            WHERE customer_id = $1
            """,
            telegram_id,
        )

        executor_stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE status = 'approved'
                ) AS approved_tasks,
                COUNT(*) FILTER (
                    WHERE status IN (
                        'taken',
                        'revision',
                        'pending_review'
                    )
                ) AS active_tasks
            FROM task_slots
            WHERE executor_id = $1
            """,
            telegram_id,
        )

    return {
        "telegram_id": telegram_id,
        "username": user.get("username"),
        "first_name": user.get("first_name", ""),
        "available_balance": wallet["available_kopecks"] / 100,
        "reserved_balance": wallet["reserved_kopecks"] / 100,
        "created_at": user_row["created_at"].isoformat(),
        "created_tasks": task_stats["created_tasks"],
        "approved_tasks": executor_stats["approved_tasks"],
        "active_tasks": executor_stats["active_tasks"],
    }


# --------------------------------------------------
# СПИСОК СВОБОДНЫХ ЗАДАНИЙ
# --------------------------------------------------

@router.get("/tasks")
async def get_tasks(
    request: Request,
    tab: str = Query(default="free"),
):
    user = await get_user(request)
    telegram_id = int(user["id"])
    pool = request.app.state.db_pool

    if tab != "free":
        raise HTTPException(
            status_code=400,
            detail="Неизвестная вкладка",
        )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                t.id,
                t.customer_id,
                t.platform,
                t.action,
                t.title,
                t.description,
                t.target_url,
                t.unit_price_kopecks,
                t.total_slots,
                t.created_at,

                COUNT(s.id) FILTER (
                    WHERE s.status = 'free'
                ) AS available_slots,

                COUNT(s.id) FILTER (
                    WHERE s.status IN (
                        'taken',
                        'revision'
                    )
                ) AS taken_slots,

                COUNT(s.id) FILTER (
                    WHERE s.status = 'pending_review'
                ) AS pending_review_slots,

                COUNT(s.id) FILTER (
                    WHERE s.status = 'approved'
                ) AS approved_slots

            FROM tasks t
            LEFT JOIN task_slots s ON s.task_id = t.id

            WHERE
                t.status = 'active'
                AND t.customer_id != $1
                AND NOT EXISTS (
                    SELECT 1
                    FROM task_bans b
                    WHERE
                        b.task_id = t.id
                        AND b.executor_id = $1
                )

            GROUP BY t.id

            HAVING COUNT(s.id) FILTER (
                WHERE s.status = 'free'
            ) > 0

            ORDER BY
                t.unit_price_kopecks DESC,
                t.created_at DESC

            LIMIT 100
            """,
            telegram_id,
        )

    return {
        "tasks": [
            {
                "id": row["id"],
                "platform": row["platform"],
                "action": row["action"],
                "title": row["title"],
                "description": row["description"],
                "target_url": row["target_url"],
                "price": row["unit_price_kopecks"] / 100,
                "total_slots": row["total_slots"],
                "available_slots": row["available_slots"],
                "taken_slots": row["taken_slots"],
                "pending_review_slots": row["pending_review_slots"],
                "approved_slots": row["approved_slots"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]
    }


# --------------------------------------------------
# МОИ ЗАКАЗЫ — ДЛЯ ЗАКАЗЧИКА
# --------------------------------------------------

@router.get("/my/tasks")
async def get_my_tasks(request: Request):
    user = await get_user(request)
    telegram_id = int(user["id"])
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                t.id,
                t.platform,
                t.action,
                t.title,
                t.description,
                t.target_url,
                t.unit_price_kopecks,
                t.total_slots,
                t.reserved_kopecks,
                t.initial_total_kopecks,
                t.status,
                t.created_at,

                COUNT(s.id) FILTER (
                    WHERE s.status = 'free'
                ) AS available_slots,

                COUNT(s.id) FILTER (
                    WHERE s.status IN ('taken', 'revision')
                ) AS taken_slots,

                COUNT(s.id) FILTER (
                    WHERE s.status = 'pending_review'
                ) AS pending_review_slots,

                COUNT(s.id) FILTER (
                    WHERE s.status = 'approved'
                ) AS approved_slots

            FROM tasks t
            LEFT JOIN task_slots s ON s.task_id = t.id

            WHERE t.customer_id = $1

            GROUP BY t.id
            ORDER BY t.created_at DESC

            LIMIT 100
            """,
            telegram_id,
        )

    return {
        "tasks": [
            {
                "id": row["id"],
                "platform": row["platform"],
                "action": row["action"],
                "title": row["title"],
                "description": row["description"],
                "target_url": row["target_url"],
                "price": row["unit_price_kopecks"] / 100,
                "total_slots": row["total_slots"],
                "available_slots": row["available_slots"],
                "taken_slots": row["taken_slots"],
                "pending_review_slots": row["pending_review_slots"],
                "approved_slots": row["approved_slots"],
                "reserved_balance": row["reserved_kopecks"] / 100,
                "initial_total": row["initial_total_kopecks"] / 100,
                "status": row["status"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]
    }


# --------------------------------------------------
# МОЯ РАБОТА — ДЛЯ ИСПОЛНИТЕЛЯ
# --------------------------------------------------

@router.get("/my/work")
async def get_my_work(
    request: Request,
    tab: str = Query(default="active"),
):
    user = await get_user(request)
    telegram_id = int(user["id"])
    pool = request.app.state.db_pool

    if tab not in ("active", "approved", "all"):
        raise HTTPException(
            status_code=400,
            detail="Неизвестная вкладка",
        )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                s.id AS slot_id,
                s.task_id,
                s.status AS slot_status,
                s.taken_at,
                s.deadline_at,
                s.completed_at,

                t.platform,
                t.action,
                t.title,
                t.description,
                t.target_url,
                t.unit_price_kopecks,
                t.status AS task_status,

                sub.proof_url,
                sub.screenshot_file_id,
                sub.status AS submission_status,
                sub.revision_count,
                sub.revision_deadline,
                sub.admin_reason

            FROM task_slots s

            JOIN tasks t ON t.id = s.task_id

            LEFT JOIN submissions sub
                ON sub.slot_id = s.id

            WHERE
                s.executor_id = $1
                AND (
                    $2 = 'all'
                    OR ($2 = 'active' AND s.status IN (
                        'taken',
                        'revision',
                        'pending_review'
                    ))
                    OR ($2 = 'approved' AND s.status = 'approved')
                )

            ORDER BY s.taken_at DESC NULLS LAST

            LIMIT 100
            """,
            telegram_id,
            tab,
        )

    return {
        "work": [
            {
                "slot_id": row["slot_id"],
                "task_id": row["task_id"],
                "status": row["slot_status"],
                "platform": row["platform"],
                "action": row["action"],
                "title": row["title"],
                "description": row["description"],
                "target_url": row["target_url"],
                "price": row["unit_price_kopecks"] / 100,
                "task_status": row["task_status"],
                "taken_at": (
                    row["taken_at"].isoformat()
                    if row["taken_at"] else None
                ),
                "deadline_at": (
                    row["deadline_at"].isoformat()
                    if row["deadline_at"] else None
                ),
                "completed_at": (
                    row["completed_at"].isoformat()
                    if row["completed_at"] else None
                ),
                "proof_url": row["proof_url"],
                "screenshot_file_id": row["screenshot_file_id"],
                "submission_status": row["submission_status"],
                "revision_count": row["revision_count"],
                "revision_deadline": (
                    row["revision_deadline"].isoformat()
                    if row["revision_deadline"] else None
                ),
                "admin_reason": row["admin_reason"],
            }
            for row in rows
        ]
    }


# --------------------------------------------------
# ИСТОРИЯ ОПЕРАЦИЙ КОШЕЛЬКА
# --------------------------------------------------

@router.get("/wallet/history")
async def get_wallet_history(request: Request):
    user = await get_user(request)
    telegram_id = int(user["id"])
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                amount_kopecks,
                operation,
                description,
                related_task_id,
                created_at
            FROM ledger
            WHERE telegram_id = $1
            ORDER BY created_at DESC
            LIMIT 50
            """,
            telegram_id,
        )

    return {
        "operations": [
            {
                "id": row["id"],
                "amount": row["amount_kopecks"] / 100,
                "operation": row["operation"],
                "description": row["description"],
                "task_id": row["related_task_id"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]
    }


# --------------------------------------------------
# НАСТРОЙКИ УВЕДОМЛЕНИЙ
# --------------------------------------------------

class NotificationSettingsUpdate(BaseModel):
    task_notifications: Optional[bool] = None
    wallet_notifications: Optional[bool] = None
    review_notifications: Optional[bool] = None
    system_notifications: Optional[bool] = None


@router.get("/notifications")
async def get_notifications(request: Request):
    user = await get_user(request)
    telegram_id = int(user["id"])
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                task_notifications,
                wallet_notifications,
                review_notifications,
                system_notifications
            FROM notification_settings
            WHERE telegram_id = $1
            """,
            telegram_id,
        )

    return dict(row)


@router.patch("/notifications")
async def update_notifications(
    request: Request,
    data: NotificationSettingsUpdate,
):
    user = await get_user(request)
    telegram_id = int(user["id"])
    pool = request.app.state.db_pool

    values = data.model_dump(exclude_unset=True)

    if not values:
        raise HTTPException(
            status_code=400,
            detail="Нет настроек для изменения",
        )

    allowed_fields = {
        "task_notifications",
        "wallet_notifications",
        "review_notifications",
        "system_notifications",
    }

    if not set(values).issubset(allowed_fields):
        raise HTTPException(
            status_code=400,
            detail="Недопустимые настройки",
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            for field, value in values.items():
                await conn.execute(
                    f"""
                    UPDATE notification_settings
                    SET {field} = $1, updated_at = NOW()
                    WHERE telegram_id = $2
                    """,
                    value,
                    telegram_id,
                )

            row = await conn.fetchrow(
                """
                SELECT
                    task_notifications,
                    wallet_notifications,
                    review_notifications,
                    system_notifications
                FROM notification_settings
                WHERE telegram_id = $1
                """,
                telegram_id,
            )

    return {
        "success": True,
        "settings": dict(row),
    }


# --------------------------------------------------
# ПРОВЕРКА ПРАВ АДМИНИСТРАТОРА
# --------------------------------------------------

@router.get("/admin/status")
async def get_admin_status(request: Request):
    user = await get_user(request)
    telegram_id = int(user["id"])

    admin_ids = {
        int(value.strip())
        for value in __import__("os").getenv(
            "ADMIN_IDS", ""
        ).split(",")
        if value.strip().isdigit()
    }

    return {
        "is_admin": telegram_id in admin_ids,
    }
