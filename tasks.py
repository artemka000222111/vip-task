
import os
from urllib.parse import urlparse

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from api import get_user
from finance import reserve_funds_tx, payout_tx


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
            s.executor_id
        FROM task_slots s
        JOIN tasks t ON t.id = s.task_id
        WHERE s.status IN ('taken', 'revision')
          AND s.deadline_at IS NOT NULL
          AND s.deadline_at <= NOW()
          AND t.status = 'active'
        FOR UPDATE OF s SKIP LOCKED
        """
    )

    for slot in expired:
        if slot["executor_id"] is None:
            continue

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
            slot["task_id"],
            slot["executor_id"],
            "Истекло время выполнения или доработки",
        )

        await conn.execute(
            """
            UPDATE submissions
            SET status = 'expired',
                updated_at = NOW()
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

    return len(expired)


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

            await expire_overdue_slots(conn)

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

            await expire_overdue_slots(conn)

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

            await expire_overdue_slots(conn)

            slot = await conn.fetchrow(
                """
                SELECT
                    s.id,
                    s.task_id,
                    s.executor_id,
                    s.status,
                    s.deadline_at,
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

            if slot["deadline_at"] is None or slot["deadline_at"].timestamp() <= 0:
                raise HTTPException(
                    409,
                    "Срок выполнения истёк",
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
                await conn.execute(
                    """
                    INSERT INTO submissions (
                        slot_id,
                        executor_id,
                        proof_url,
                        screenshot_file_id,
                        status
                    )
                    VALUES ($1, $2, $3, $4, 'pending')
                    """,
                    slot_id,
                    executor_id,
                    proof_url,
                    screenshot_id,
                )

            await conn.execute(
                """
                UPDATE task_slots
                SET status = 'pending_review',
                    deadline_at = NULL
                WHERE id = $1
                """,
                slot_id,
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
        async with conn.transaction():
            await expire_overdue_slots(conn)

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

            slot = await conn.fetchrow(
                """
                SELECT
                    s.id,
                    s.executor_id,
                    s.status,
                    t.customer_id,
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

    return {
        "success": True,
        "message": "Задание одобрено. Исполнителю начислена полная сумма.",
        "paid": int(slot["unit_price_kopecks"]) / 100,
    }


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

            slot = await conn.fetchrow(
                """
                SELECT
                    s.id,
                    s.executor_id,
                    s.status,
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

            slot = await conn.fetchrow(
                """
                SELECT
                    s.id,
                    s.executor_id,
                    s.status,
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
                raise HTTPException(409, "Отправка уже обработана")

            executor_id = slot["executor_id"]

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

            slot = await conn.fetchrow(
                """
                SELECT
                    s.id,
                    s.executor_id,
                    s.status,
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

    return {
        "success": True,
        "message": "Исполнитель удалён с места. Средства остались в резерве.",
    }
