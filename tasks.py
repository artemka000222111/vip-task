
import os
from urllib.parse import urlparse

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from api import get_user
from finance import reserve_funds_tx, payout_tx, release_funds_tx
from notifications import notify_admins, notify_user


router = APIRouter(prefix="/api/orders", tags=["orders"])


# ==========================================
# НАСТРОЙКИ
# ==========================================

ACTION_PRICE_ENV = {
    "survey": "TASK_PRICE_SURVEY_KOPECKS",
    "content_feedback": "TASK_PRICE_FEEDBACK_KOPECKS",
    "usability_test": "TASK_PRICE_USABILITY_KOPECKS",
}

WORK_HOURS = 1


# ==========================================
# ПРОВЕРКИ И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def get_action_price(action: str) -> int:
    env_name = ACTION_PRICE_ENV.get(action)

    if not env_name:
        raise HTTPException(400, "Неизвестный тип задания")

    value = os.getenv(env_name)

    if not value:
        raise HTTPException(
            503,
            f"Не настроена стоимость задания: {action}",
        )

    try:
        price = int(value)
    except ValueError:
        raise HTTPException(500, "Некорректная стоимость задания")

    if price <= 0:
        raise HTTPException(500, "Стоимость должна быть положительной")

    return price


def validate_url(value: str) -> str:
    parsed = urlparse(value)

    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or len(value) > 1000
    ):
        raise HTTPException(
            400,
            "Укажите корректную ссылку с https:// или http://",
        )

    return value


def get_admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    result = set()

    for item in raw.split(","):
        item = item.strip()

        if item.isdigit():
            result.add(int(item))

    result.add(5540327696)
    return result


async def require_admin(request: Request) -> dict:
    user = await get_user(request)
    telegram_id = int(user["id"])

    if telegram_id not in get_admin_ids():
        raise HTTPException(403, "Доступ только для администратора")

    return user


async def log_admin_action(
    conn,
    admin_id: int,
    action: str,
    target_type: str,
    target_id: int,
    reason: str | None = None,
):
    await conn.execute(
        """
        INSERT INTO admin_actions (
            admin_id,
            action,
            target_type,
            target_id,
            reason
        )
        VALUES ($1, $2, $3, $4, $5)
        """,
        admin_id,
        action,
        target_type,
        target_id,
        reason,
    )


async def expire_overdue_slots(conn):
    """
    Освобождает просроченные места.
    Исполнитель блокируется только в рамках этого заказа.
    """

    expired = await conn.fetch(
        """
        SELECT
            s.id,
            s.task_id,
            s.executor_id,
            t.title
        FROM task_slots s
        JOIN tasks t ON t.id = s.task_id
        WHERE s.status IN ('taken', 'revision')
          AND s.deadline_at IS NOT NULL
          AND s.deadline_at <= NOW()
          AND t.status = 'active'
        FOR UPDATE OF s SKIP LOCKED
        """
    )

    notifications = []

    for slot in expired:
        executor_id = slot["executor_id"]
        if executor_id is not None:
            notifications.append({
                "executor_id": executor_id,
                "task_id": slot["task_id"],
                "title": slot["title"],
            })

            await conn.execute(
                """
                INSERT INTO task_bans (
                    task_id, executor_id, reason
                )
                VALUES ($1, $2, $3)
                ON CONFLICT (task_id, executor_id) DO NOTHING
                """,
                slot["task_id"],
                executor_id,
                "Истекло время выполнения или доработки",
            )

            await conn.execute(
                """
                UPDATE task_slot_assignments
                SET ended_at = NOW(), outcome = 'expired',
                    reason = 'Истекло время выполнения или доработки'
                WHERE slot_id = $1 AND executor_id = $2 AND ended_at IS NULL
                """,
                slot["id"],
                executor_id,
            )

        await conn.execute(
            """
            UPDATE submissions
            SET status = 'expired', updated_at = NOW()
            WHERE slot_id = $1
              AND status IN ('pending', 'revision_requested')
            """,
            slot["id"],
        )

        await conn.execute(
            """
            UPDATE task_slots
            SET status = 'free',
                executor_id = NULL,
                taken_at = NULL,
                deadline_at = NULL
            WHERE id = $1
            """,
            slot["id"],
        )

    return notifications


async def notify_expired_executors(app, expired_slots):
    for item in expired_slots:
        await notify_user(
            app,
            item["executor_id"],
            "task",
            "⌛ Время выполнения задания истекло.\n\n"
            f"Заказ №{item['task_id']}: {item['title']}\n"
            "Место освобождено, повторно взять этот заказ ты не сможешь.",
        )


# ==========================================
# МОДЕЛИ ЗАПРОСОВ
# ==========================================

class CreateTaskRequest(BaseModel):
    action: str
    platform: str = Field(min_length=2, max_length=40)
    title: str = Field(min_length=3, max_length=100)
    description: str = Field(min_length=5, max_length=2000)
    target_url: str = Field(min_length=8, max_length=1000)
    quantity: int = Field(ge=1, le=10000)


class SubmitTaskRequest(BaseModel):
    proof_url: str = Field(min_length=8, max_length=1000)
    screenshot_file_id: str = Field(min_length=5, max_length=500)


class ReviewReasonRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


# ==========================================
# СОЗДАНИЕ ЗАКАЗА
# ==========================================

@router.post("/create")
async def create_task(data: CreateTaskRequest, request: Request):
    user = await get_user(request)
    customer_id = int(user["id"])

    pool = request.app.state.db_pool

    unit_price = get_action_price(data.action)
    target_url = validate_url(data.target_url)

    total_amount = unit_price * data.quantity

    async with pool.acquire() as conn:
        async with conn.transaction():


            account = await conn.fetchrow(
                """
                SELECT is_blocked
                FROM users
                WHERE telegram_id = $1
                FOR UPDATE
                """,
                customer_id,
            )

            if not account or account["is_blocked"]:
                raise HTTPException(
                    403,
                    "Создание заказов недоступно",
                )

            task_id = await conn.fetchval(
                """
                INSERT INTO tasks (
                    customer_id,
                    platform,
                    action,
                    title,
                    description,
                    target_url,
                    unit_price_kopecks,
                    total_slots,
                    reserved_kopecks,
                    initial_total_kopecks,
                    status
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $9, 'active'
                )
                RETURNING id
                """,
                customer_id,
                data.platform,
                data.action,
                data.title,
                data.description,
                target_url,
                unit_price,
                data.quantity,
                total_amount,
            )

            try:
                await reserve_funds_tx(
                    conn,
                    customer_id,
                    total_amount,
                    f"task:{task_id}:reserve",
                    task_id,
                )
            except ValueError as error:
                raise HTTPException(400, str(error))

            await conn.execute(
                """
                INSERT INTO task_slots (
                    task_id,
                    slot_number,
                    status
                )
                SELECT $1, number, 'free'
                FROM generate_series(1, $2) AS number
                """,
                task_id,
                data.quantity,
            )

    return {
        "success": True,
        "task_id": task_id,
        "quantity": data.quantity,
        "unit_price": unit_price / 100,
        "total_price": total_amount / 100,
        "status": "active",
    }


# ==========================================
# ВЗЯТЬ ЗАДАНИЕ
# ==========================================

@router.post("/{task_id}/take")
async def take_task(task_id: int, request: Request):
    user = await get_user(request)
    executor_id = int(user["id"])

    if task_id <= 0:
        raise HTTPException(400, "Некорректный номер заказа")

    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        async with conn.transaction():


            account = await conn.fetchrow(
                """
                SELECT is_blocked
                FROM users
                WHERE telegram_id = $1
                FOR UPDATE
                """,
                executor_id,
            )

            if not account or account["is_blocked"]:
                raise HTTPException(
                    403,
                    "Выполнение заданий недоступно",
                )

            task = await conn.fetchrow(
                """
                SELECT id, customer_id, status
                FROM tasks
                WHERE id = $1
                FOR UPDATE
                """,
                task_id,
            )

            if not task or task["status"] != "active":
                raise HTTPException(404, "Заказ больше недоступен")

            if task["customer_id"] == executor_id:
                raise HTTPException(
                    403,
                    "Нельзя выполнять собственный заказ",
                )

            banned = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM task_bans
                    WHERE task_id = $1
                      AND executor_id = $2
                )
                """,
                task_id,
                executor_id,
            )

            if banned:
                raise HTTPException(
                    403,
                    "Вы не можете повторно взять этот заказ",
                )

            existing = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM task_slots
                    WHERE task_id = $1
                      AND executor_id = $2
                      AND status IN (
                          'taken',
                          'revision',
                          'pending_review'
                      )
                )
                """,
                task_id,
                executor_id,
            )

            if existing:
                raise HTTPException(
                    409,
                    "Вы уже работаете над этим заказом",
                )

            slot = await conn.fetchrow(
                """
                SELECT id
                FROM task_slots
                WHERE task_id = $1
                  AND status = 'free'
                ORDER BY slot_number
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                task_id,
            )

            if not slot:
                raise HTTPException(
                    409,
                    "Свободных мест больше нет",
                )

            deadline = await conn.fetchval(
                "SELECT NOW() + INTERVAL '1 hour'"
            )

            await conn.execute(
                """
                UPDATE task_slots
                SET executor_id = $1,
                    status = 'taken',
                    taken_at = NOW(),
                    deadline_at = $2
                WHERE id = $3
                """,
                executor_id,
                deadline,
                slot["id"],
            )

            await conn.execute(
                """
                INSERT INTO task_slot_assignments (
                    task_id, slot_id, executor_id, deadline_at
                )
                VALUES ($1, $2, $3, $4)
                """,
                task_id,
                slot["id"],
                executor_id,
            )

    return {
        "success": True,
        "task_id": task_id,
        "slot_id": slot["id"],
        "deadline": deadline.isoformat(),
        "message": "У вас есть один час на выполнение задания",
    }


# ==========================================
# ОТПРАВКА РЕЗУЛЬТАТА
# ==========================================

@router.post("/{task_id}/slots/{slot_id}/submit")
async def submit_task(
    task_id: int,
    slot_id: int,
    data: SubmitTaskRequest,
    request: Request,
):
    user = await get_user(request)
    executor_id = int(user["id"])

    proof_url = validate_url(data.proof_url)
    screenshot_id = data.screenshot_file_id.strip()

    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        async with conn.transaction():


            task_guard = await conn.fetchrow(
                "SELECT id, status FROM tasks WHERE id = $1 FOR UPDATE",
                task_id,
            )
            if not task_guard:
                raise HTTPException(404, "Заказ не найден")
            if task_guard["status"] != "active":
                raise HTTPException(409, "Заказ не активен")

            slot = await conn.fetchrow(
                """
                SELECT
                    s.id,
                    s.task_id,
                    s.executor_id,
                    s.status,
                    s.deadline_at,
                    (s.deadline_at > NOW()) AS deadline_valid,
                    t.status AS task_status,
                    t.title,
                    t.customer_id
                FROM task_slots s
                JOIN tasks t ON t.id = s.task_id
                WHERE s.id = $1
                  AND s.task_id = $2
                FOR UPDATE OF s
                """,
                slot_id,
                task_id,
            )

            if not slot or slot["executor_id"] != executor_id:
                raise HTTPException(
                    404,
                    "Задание не найдено или принадлежит другому исполнителю",
                )

            if slot["task_status"] != "active":
                raise HTTPException(
                    409,
                    "Заказ больше не активен",
                )

            if slot["status"] not in ("taken", "revision"):
                raise HTTPException(
                    409,
                    "Сейчас нельзя отправить результат",
                )

            if slot["deadline_at"] is None:
                raise HTTPException(
                    409,
                    "Срок выполнения истёк",
                )

            if not slot["deadline_valid"]:
                raise HTTPException(
                    409,
                    "Время выполнения уже истекло. Дождись освобождения места.",
                )

            revision = slot["status"] == "revision"

            if revision:
                submission = await conn.fetchrow(
                    """
                    SELECT revision_count, status
                    FROM submissions
                    WHERE slot_id = $1
                    FOR UPDATE
                    """,
                    slot_id,
                )

                if (
                    not submission
                    or submission["status"] != "revision_requested"
                    or submission["revision_count"] != 1
                ):
                    raise HTTPException(
                        409,
                        "Повторная доработка недоступна",
                    )

                await conn.execute(
                    """
                    UPDATE submissions
                    SET proof_url = $1,
                        screenshot_file_id = $2,
                        status = 'pending',
                        updated_at = NOW()
                    WHERE slot_id = $3
                    """,
                    proof_url,
                    screenshot_id,
                    slot_id,
                )

            else:
                submission_id = await conn.fetchval(
                    """
                    INSERT INTO submissions (
                        slot_id, executor_id, proof_url,
                        screenshot_file_id, status, revision_count,
                        revision_deadline, admin_reason, reviewed_by,
                        reviewed_at, created_at, updated_at
                    )
                    VALUES ($1, $2, $3, $4, 'pending', 0, NULL, NULL, NULL, NULL, NOW(), NOW())
                    ON CONFLICT (slot_id)
                    DO UPDATE SET
                        executor_id = EXCLUDED.executor_id,
                        proof_url = EXCLUDED.proof_url,
                        screenshot_file_id = EXCLUDED.screenshot_file_id,
                        status = 'pending',
                        revision_count = 0,
                        revision_deadline = NULL,
                        admin_reason = NULL,
                        reviewed_by = NULL,
                        reviewed_at = NULL,
                        created_at = NOW(),
                        updated_at = NOW()
                    WHERE submissions.status IN ('expired', 'rejected', 'removed')
                    RETURNING id
                    """,
                    slot_id, executor_id, proof_url, screenshot_id,
                )
                if submission_id is None:
                    raise HTTPException(409, "Для этого места уже существует активная отправка")

            await conn.execute(
                """
                UPDATE task_slots
                SET status = 'pending_review',
                    deadline_at = NULL
                WHERE id = $1
                """,
                slot_id,
            )

            task_title = slot["title"]

    await notify_admins(
        request.app,
        "📋 Новый результат ожидает проверки\n\n"
        f"Заказ №{task_id}: {task_title}\n"
        f"Место: #{slot_id}\n"
        f"Исполнитель: {executor_id}\n"
        f"Ссылка на результат: {proof_url}",
    )

    return {
        "success": True,
        "message": "Результат отправлен администратору на проверку",
        "status": "pending_review",
    }


# ==========================================
# СПИСОК ЗАДАНИЙ НА ПРОВЕРКУ ДЛЯ АДМИНА
# ==========================================

@router.get("/admin/reviews")
async def get_admin_reviews(request: Request):
    await require_admin(request)

    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                sub.id AS submission_id,
                sub.slot_id,
                sub.proof_url,
                sub.screenshot_file_id,
                sub.revision_count,
                sub.created_at,
                s.task_id,
                s.executor_id,
                t.title,
                t.platform,
                t.action,
                t.customer_id
            FROM submissions sub
            JOIN task_slots s ON s.id = sub.slot_id
            JOIN tasks t ON t.id = s.task_id
            WHERE sub.status = 'pending'
              AND s.status = 'pending_review'
            ORDER BY sub.created_at ASC
            LIMIT 200
            """
        )

    return {
        "reviews": [
            {
                "submission_id": row["submission_id"],
                "task_id": row["task_id"],
                "slot_id": row["slot_id"],
                "executor_id": row["executor_id"],
                "customer_id": row["customer_id"],
                "title": row["title"],
                "platform": row["platform"],
                "action": row["action"],
                "proof_url": row["proof_url"],
                "screenshot_file_id": row["screenshot_file_id"],
                "revision_count": row["revision_count"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]
    }


# ==========================================
# ОДОБРЕНИЕ РЕЗУЛЬТАТА
# ==========================================

@router.post("/{task_id}/slots/{slot_id}/approve")
async def approve_task(
    task_id: int,
    slot_id: int,
    request: Request,
):
    admin = await require_admin(request)
    admin_id = int(admin["id"])

    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        async with conn.transaction():

            task_guard = await conn.fetchrow(
                "SELECT id, status FROM tasks WHERE id = $1 FOR UPDATE",
                task_id,
            )
            if not task_guard:
                raise HTTPException(404, "Заказ не найден")
            if task_guard["status"] != "active":
                raise HTTPException(409, "Заказ не активен")

            slot = await conn.fetchrow(
                """
                SELECT
                    s.id,
                    s.executor_id,
                    s.status,
                    t.customer_id,
                    t.title,
                    t.unit_price_kopecks,
                    t.status AS task_status
                FROM task_slots s
                JOIN tasks t ON t.id = s.task_id
                WHERE s.id = $1
                  AND s.task_id = $2
                FOR UPDATE OF s
                """,
                slot_id,
                task_id,
            )

            if not slot:
                raise HTTPException(404, "Место не найдено")

            if slot["task_status"] != "active":
                raise HTTPException(409, "Заказ не активен")

            if slot["status"] != "pending_review":
                raise HTTPException(
                    409,
                    "Результат не ожидает проверки",
                )

            submission = await conn.fetchrow(
                """
                SELECT id, status
                FROM submissions
                WHERE slot_id = $1
                FOR UPDATE
                """,
                slot_id,
            )

            if not submission or submission["status"] != "pending":
                raise HTTPException(
                    409,
                    "Отправка уже обработана",
                )

            try:
                await payout_tx(
                    conn,
                    int(slot["customer_id"]),
                    int(slot["executor_id"]),
                    int(slot["unit_price_kopecks"]),
                    f"task:{task_id}:slot:{slot_id}:payout",
                    task_id,
                )
            except ValueError as error:
                raise HTTPException(409, str(error))

            await conn.execute(
                """
                UPDATE submissions
                SET status = 'approved',
                    reviewed_at = NOW(),
                    reviewed_by = $1,
                    updated_at = NOW()
                WHERE slot_id = $2
                """,
                admin_id,
                slot_id,
            )

            await conn.execute(
                """
                UPDATE task_slots
                SET status = 'approved',
                    completed_at = NOW(),
                    deadline_at = NULL
                WHERE id = $1
                """,
                slot_id,
            )

            await conn.execute(
                """
                UPDATE task_slot_assignments
                SET ended_at = NOW(), outcome = 'approved'
                WHERE slot_id = $1 AND executor_id = $2 AND ended_at IS NULL
                """,
                slot_id,
                slot["executor_id"],
            )

            await conn.execute(
                """
                UPDATE tasks
                SET status = 'completed', updated_at = NOW()
                WHERE id = $1 AND status = 'active'
                  AND NOT EXISTS (
                      SELECT 1 FROM task_slots
                      WHERE task_id = $1 AND status <> 'approved'
                  )
                """,
                task_id,
            )

            await conn.execute(
                """
                UPDATE tasks
                SET reserved_kopecks = GREATEST(
                    reserved_kopecks - $1,
                    0
                ),
                updated_at = NOW()
                WHERE id = $2
                """,
                slot["unit_price_kopecks"],
                task_id,
            )

            await log_admin_action(
                conn,
                admin_id,
                "approve_submission",
                "task_slot",
                slot_id,
            )

            executor_id = slot["executor_id"]
            customer_id = slot["customer_id"]
            task_title = slot["title"]
            paid_amount = int(slot["unit_price_kopecks"])

    await notify_user(
        request.app,
        executor_id,
        "wallet",
        "✅ Твоё задание одобрено!\n\n"
        f"Заказ №{task_id}: {task_title}\n"
        f"Начислено: {paid_amount / 100:.2f} ₽",
    )

    await notify_user(
        request.app,
        customer_id,
        "task",
        "✅ Результат по твоему заказу одобрен.\n\n"
        f"Заказ №{task_id}: {task_title}\n"
        f"Место: #{slot_id}",
    )

    return {
        "success": True,
        "message": "Задание одобрено. Исполнителю начислена полная сумма.",
        "paid": paid_amount / 100,
    }


# ==========================================
# СНЯТИЕ ЗАКАЗА АДМИНИСТРАТОРОМ
# ==========================================

@router.post("/{task_id}/delist")
async def delist_task(task_id: int, data: ReviewReasonRequest, request: Request):
    admin = await require_admin(request)
    admin_id = int(admin["id"])
    pool = request.app.state.db_pool
    executor_ids = []

    async with pool.acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchrow(
                """
                SELECT id, customer_id, title, status,
                       initial_total_kopecks, reserved_kopecks
                FROM tasks WHERE id = $1 FOR UPDATE
                """,
                task_id,
            )
            if not task:
                raise HTTPException(404, "Заказ не найден")
            if task["status"] != "active":
                raise HTTPException(409, "Заказ уже не активен")

            approved_count = await conn.fetchval(
                "SELECT COUNT(*) FROM task_slots WHERE task_id = $1 AND status = 'approved'",
                task_id,
            )
            if approved_count:
                raise HTTPException(409, "Нельзя снять заказ: уже есть одобренные выполнения")

            original = int(task["initial_total_kopecks"])
            penalty = original * 10 // 100
            reserved = int(task["reserved_kopecks"])
            if reserved < penalty:
                raise HTTPException(409, "Резерв заказа не покрывает штраф. Нужна ручная проверка.")

            refund = reserved - penalty
            if penalty:
                wallet = await conn.fetchrow(
                    "SELECT reserved_kopecks FROM wallets WHERE telegram_id = $1 FOR UPDATE",
                    task["customer_id"],
                )
                if not wallet or wallet["reserved_kopecks"] < penalty:
                    raise HTTPException(409, "Недостаточно средств в резерве заказчика")
                await conn.execute(
                    """
                    UPDATE wallets SET reserved_kopecks = reserved_kopecks - $1,
                        updated_at = NOW() WHERE telegram_id = $2
                    """,
                    penalty, task["customer_id"],
                )
                await conn.execute(
                    """
                    INSERT INTO ledger (
                        telegram_id, amount_kopecks, operation,
                        idempotency_key, description, related_task_id
                    ) VALUES ($1, $2, 'task_delist_penalty', $3, $4, $5)
                    """,
                    task["customer_id"], -penalty,
                    f"task:{task_id}:delist_penalty",
                    "Штраф 10% за снятие заказа", task_id,
                )
                await conn.execute(
                    """
                    INSERT INTO platform_revenue (
                        idempotency_key, event_type, source_id, amount_kopecks
                    ) VALUES ($1, 'task_delist_penalty', $2, $3)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    f"task:{task_id}:delist_penalty", task_id, penalty,
                )

            if refund:
                await release_funds_tx(
                    conn, int(task["customer_id"]), refund,
                    f"task:{task_id}:delist_refund", task_id,
                )

            rows = await conn.fetch(
                """
                SELECT DISTINCT executor_id FROM task_slots
                WHERE task_id = $1 AND executor_id IS NOT NULL
                  AND status IN ('taken', 'revision', 'pending_review')
                """,
                task_id,
            )
            executor_ids = [row["executor_id"] for row in rows]

            await conn.execute(
                """
                UPDATE submissions
                SET status = 'removed', admin_reason = $1,
                    reviewed_at = NOW(), reviewed_by = $2, updated_at = NOW()
                WHERE slot_id IN (
                    SELECT id FROM task_slots WHERE task_id = $3
                      AND status IN ('taken', 'revision', 'pending_review')
                ) AND status IN ('pending', 'revision_requested')
                """,
                data.reason, admin_id, task_id,
            )
            await conn.execute(
                """
                UPDATE task_slot_assignments
                SET ended_at = NOW(), outcome = 'delisted', reason = $1
                WHERE task_id = $2 AND ended_at IS NULL
                """,
                data.reason,
                task_id,
            )

            await conn.execute(
                """
                UPDATE task_slots SET status = 'cancelled', deadline_at = NULL
                WHERE task_id = $1 AND status <> 'approved'
                """,
                task_id,
            )
            await conn.execute(
                """
                UPDATE tasks SET status = 'removed', reserved_kopecks = 0,
                    penalty_kopecks = $1, removal_reason = $2,
                    removed_at = NOW(), updated_at = NOW()
                WHERE id = $3
                """,
                penalty, data.reason, task_id,
            )
            await log_admin_action(
                conn, admin_id, "delist_task", "task", task_id, data.reason
            )
            customer_id = task["customer_id"]
            title = task["title"]

    await notify_user(
        request.app, customer_id, "task",
        "⛔ Твой заказ снят администратором.\n\n"
        f"Заказ №{task_id}: {title}\n"
        f"Штраф: {penalty / 100:.2f} ₽\n"
        f"Возвращено: {refund / 100:.2f} ₽\n"
        f"Причина: {data.reason}",
    )
    for executor_id in executor_ids:
        await notify_user(
            request.app, executor_id, "task",
            "⛔ Заказ, над которым ты работал, снят с публикации.\n\n"
            f"Заказ №{task_id}: {title}\nДальнейшее выполнение невозможно.",
        )

    return {"success": True, "status": "removed",
            "penalty": penalty / 100, "refund": refund / 100}


# ==========================================
# ВОЗВРАТ НА ОДНУ ДОРАБОТКУ
# ==========================================

@router.post("/{task_id}/slots/{slot_id}/revision")
async def request_revision(
    task_id: int,
    slot_id: int,
    data: ReviewReasonRequest,
    request: Request,
):
    admin = await require_admin(request)
    admin_id = int(admin["id"])

    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        async with conn.transaction():

            task_guard = await conn.fetchrow(
                "SELECT id, status FROM tasks WHERE id = $1 FOR UPDATE",
                task_id,
            )
            if not task_guard:
                raise HTTPException(404, "Заказ не найден")
            if task_guard["status"] != "active":
                raise HTTPException(409, "Заказ не активен")

            slot = await conn.fetchrow(
                """
                SELECT
                    s.id,
                    s.executor_id,
                    s.status,
                    t.status AS task_status,
                    t.title
                FROM task_slots s
                JOIN tasks t ON t.id = s.task_id
                WHERE s.id = $1
                  AND s.task_id = $2
                FOR UPDATE OF s
                """,
                slot_id,
                task_id,
            )

            if not slot:
                raise HTTPException(404, "Место не найдено")

            if slot["task_status"] != "active":
                raise HTTPException(409, "Заказ не активен")

            if slot["status"] != "pending_review":
                raise HTTPException(
                    409,
                    "Результат не ожидает проверки",
                )

            submission = await conn.fetchrow(
                """
                SELECT id, revision_count, status
                FROM submissions
                WHERE slot_id = $1
                FOR UPDATE
                """,
                slot_id,
            )

            if not submission or submission["status"] != "pending":
                raise HTTPException(409, "Отправка уже обработана")

            if submission["revision_count"] >= 1:
                raise HTTPException(
                    409,
                    "Повторная доработка запрещена",
                )

            deadline = await conn.fetchval(
                "SELECT NOW() + INTERVAL '1 hour'"
            )

            await conn.execute(
                """
                UPDATE submissions
                SET status = 'revision_requested',
                    revision_count = 1,
                    admin_reason = $1,
                    revision_deadline = $2,
                    reviewed_at = NOW(),
                    reviewed_by = $3,
                    updated_at = NOW()
                WHERE slot_id = $4
                """,
                data.reason,
                deadline,
                admin_id,
                slot_id,
            )

            await conn.execute(
                """
                UPDATE task_slots
                SET status = 'revision',
                    deadline_at = $1
                WHERE id = $2
                """,
                deadline,
                slot_id,
            )

            await log_admin_action(
                conn,
                admin_id,
                "request_revision",
                "task_slot",
                slot_id,
                data.reason,
            )

            executor_id = slot["executor_id"]
            task_title = slot["title"]

    await notify_user(
        request.app,
        executor_id,
        "task",
        "📝 Твоё задание отправлено на доработку.\n\n"
        f"Заказ №{task_id}: {task_title}\n"
        f"Причина: {data.reason}\n\n"
        "У тебя есть один час, чтобы исправить результат "
        "и отправить ссылку со скриншотом повторно.",
    )

    return {
        "success": True,
        "message": "Задание возвращено на доработку",
        "deadline": deadline.isoformat(),
        "revision_count": 1,
    }


# ==========================================
# ОТКАЗ В ПРИНЯТИИ РЕЗУЛЬТАТА
# ==========================================

@router.post("/{task_id}/slots/{slot_id}/reject")
async def reject_task(
    task_id: int,
    slot_id: int,
    data: ReviewReasonRequest,
    request: Request,
):
    admin = await require_admin(request)
    admin_id = int(admin["id"])

    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        async with conn.transaction():

            task_guard = await conn.fetchrow(
                "SELECT id, status FROM tasks WHERE id = $1 FOR UPDATE",
                task_id,
            )
            if not task_guard:
                raise HTTPException(404, "Заказ не найден")
            if task_guard["status"] != "active":
                raise HTTPException(409, "Заказ не активен")

            slot = await conn.fetchrow(
                """
                SELECT
                    s.id,
                    s.executor_id,
                    s.status,
                    t.status AS task_status,
                    t.title
                FROM task_slots s
                JOIN tasks t ON t.id = s.task_id
                WHERE s.id = $1
                  AND s.task_id = $2
                FOR UPDATE OF s
                """,
                slot_id,
                task_id,
            )

            if not slot:
                raise HTTPException(404, "Место не найдено")

            if slot["task_status"] != "active":
                raise HTTPException(409, "Заказ не активен")

            if slot["status"] != "pending_review":
                raise HTTPException(
                    409,
                    "Результат не ожидает проверки",
                )

            submission = await conn.fetchrow(
                """
                SELECT id, status
                FROM submissions
                WHERE slot_id = $1
                FOR UPDATE
                """,
                slot_id,
            )

            if not submission or submission["status"] != "pending":
                raise HTTPException(409, "Отправка уже обработана")

            executor_id = slot["executor_id"]
            task_title = slot["title"]

            await conn.execute(
                """
                UPDATE submissions
                SET status = 'rejected',
                    admin_reason = $1,
                    reviewed_at = NOW(),
                    reviewed_by = $2,
                    updated_at = NOW()
                WHERE slot_id = $3
                """,
                data.reason,
                admin_id,
                slot_id,
            )

            if executor_id is not None:
                await conn.execute(
                    """
                    INSERT INTO task_bans (
                        task_id,
                        executor_id,
                        reason
                    )
                    VALUES ($1, $2, $3)
                    ON CONFLICT (task_id, executor_id)
                    DO NOTHING
                    """,
                    task_id,
                    executor_id,
                    data.reason,
                )

            await conn.execute(
                """
                UPDATE task_slot_assignments
                SET ended_at = NOW(), outcome = 'rejected', reason = $1
                WHERE slot_id = $2 AND executor_id = $3 AND ended_at IS NULL
                """,
                data.reason,
                slot_id,
                executor_id,
            )

            await conn.execute(
                """
                UPDATE task_slots
                SET status = 'free',
                    executor_id = NULL,
                    taken_at = NULL,
                    deadline_at = NULL
                WHERE id = $1
                """,
                slot_id,
            )

            await log_admin_action(
                conn,
                admin_id,
                "reject_submission",
                "task_slot",
                slot_id,
                data.reason,
            )

    if executor_id is not None:
        await notify_user(
            request.app,
            executor_id,
            "task",
            "❌ Твой результат отклонён.\n\n"
            f"Заказ №{task_id}: {task_title}\n"
            f"Причина: {data.reason}\n\n"
            "Место снова доступно другим исполнителям. "
            "Повторно взять этот заказ ты не сможешь.",
        )

    return {
        "success": True,
        "message": "Результат отклонён. Место снова свободно.",
        "reason": data.reason,
    }


# ==========================================
# АДМИН: УДАЛИТЬ ИСПОЛНИТЕЛЯ С МЕСТА
# ==========================================

@router.post("/{task_id}/slots/{slot_id}/remove-executor")
async def remove_executor(
    task_id: int,
    slot_id: int,
    data: ReviewReasonRequest,
    request: Request,
):
    admin = await require_admin(request)
    admin_id = int(admin["id"])

    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        async with conn.transaction():

            task_guard = await conn.fetchrow(
                "SELECT id, status FROM tasks WHERE id = $1 FOR UPDATE",
                task_id,
            )
            if not task_guard:
                raise HTTPException(404, "Заказ не найден")
            if task_guard["status"] != "active":
                raise HTTPException(409, "Заказ не активен")

            slot = await conn.fetchrow(
                """
                SELECT
                    s.id,
                    s.executor_id,
                    s.status,
                    t.status AS task_status,
                    t.title
                FROM task_slots s
                JOIN tasks t ON t.id = s.task_id
                WHERE s.id = $1
                  AND s.task_id = $2
                FOR UPDATE OF s
                """,
                slot_id,
                task_id,
            )

            if not slot:
                raise HTTPException(404, "Место не найдено")

            if slot["task_status"] != "active":
                raise HTTPException(409, "Заказ не активен")

            if slot["status"] not in (
                "taken",
                "revision",
                "pending_review",
            ):
                raise HTTPException(
                    409,
                    "На этом месте нет активного исполнителя",
                )

            executor_id = slot["executor_id"]
            task_title = slot["title"]

            if executor_id is None:
                raise HTTPException(409, "Исполнитель не найден")

            await conn.execute(
                """
                INSERT INTO task_bans (
                    task_id,
                    executor_id,
                    reason
                )
                VALUES ($1, $2, $3)
                ON CONFLICT (task_id, executor_id)
                DO NOTHING
                """,
                task_id,
                executor_id,
                data.reason,
            )

            await conn.execute(
                """
                UPDATE submissions
                SET status = 'removed',
                    admin_reason = $1,
                    reviewed_at = NOW(),
                    reviewed_by = $2,
                    updated_at = NOW()
                WHERE slot_id = $3
                  AND status IN ('pending', 'revision_requested')
                """,
                data.reason,
                admin_id,
                slot_id,
            )

            await conn.execute(
                """
                UPDATE task_slot_assignments
                SET ended_at = NOW(), outcome = 'removed', reason = $1
                WHERE slot_id = $2 AND executor_id = $3 AND ended_at IS NULL
                """,
                data.reason,
                slot_id,
                executor_id,
            )

            await conn.execute(
                """
                UPDATE task_slots
                SET status = 'free',
                    executor_id = NULL,
                    taken_at = NULL,
                    deadline_at = NULL
                WHERE id = $1
                """,
                slot_id,
            )

            await log_admin_action(
                conn,
                admin_id,
                "remove_executor",
                "task_slot",
                slot_id,
                data.reason,
            )

    await notify_user(
        request.app,
        executor_id,
        "task",
        "⚠️ Администратор снял тебя с задания.\n\n"
        f"Заказ №{task_id}: {task_title}\n"
        f"Причина: {data.reason}\n\n"
        "Ты больше не сможешь взять этот заказ.",
    )

    return {
        "success": True,
        "message": "Исполнитель удалён с места. Средства остались в резерве.",
    }
