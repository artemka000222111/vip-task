
import os
from datetime import timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from api import get_user
from finance import reserve_funds_tx


router = APIRouter(prefix="/api/orders", tags=["orders"])


# Стоимость задаётся только на сервере, в копейках.
# Не передавать стоимость задания из Mini App.
ACTION_PRICE_ENV = {
    "survey": "TASK_PRICE_SURVEY_KOPECKS",
    "content_feedback": "TASK_PRICE_FEEDBACK_KOPECKS",
    "usability_test": "TASK_PRICE_USABILITY_KOPECKS",
}


def get_action_price(action: str) -> int:
    env_name = ACTION_PRICE_ENV.get(action)

    if not env_name:
        raise HTTPException(
            status_code=400,
            detail="Неизвестный тип задания",
        )

    value = os.getenv(env_name)

    if not value:
        raise HTTPException(
            status_code=503,
            detail=f"Не настроена стоимость задания: {action}",
        )

    try:
        price = int(value)
    except ValueError:
        raise HTTPException(
            status_code=500,
            detail="Некорректная настройка стоимости задания",
        )

    if price <= 0:
        raise HTTPException(
            status_code=500,
            detail="Стоимость задания должна быть положительной",
        )

    return price


def validate_target_url(value: str) -> str:
    parsed = urlparse(value)

    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or len(value) > 1000
    ):
        raise HTTPException(
            status_code=400,
            detail="Укажите корректную ссылку с https:// или http://",
        )

    return value


class CreateTaskRequest(BaseModel):
    action: str
    platform: str = Field(min_length=2, max_length=40)
    title: str = Field(min_length=3, max_length=100)
    description: str = Field(min_length=5, max_length=2000)
    target_url: str = Field(min_length=8, max_length=1000)
    quantity: int = Field(ge=1, le=10000)


@router.post("/create")
async def create_task(data: CreateTaskRequest, request: Request):
    user = await get_user(request)
    customer_id = int(user["id"])

    pool = request.app.state.db_pool

    unit_price = get_action_price(data.action)
    target_url = validate_target_url(data.target_url)

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
                    status_code=403,
                    detail="Создание заказов недоступно",
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

            # Резервирование и создание заказа находятся
            # в одной транзакции PostgreSQL.
            await reserve_funds_tx(
                conn,
                customer_id,
                total_amount,
                f"task:{task_id}:reserve",
                task_id,
            )

            await conn.execute(
                """
                INSERT INTO task_slots (task_id, slot_number, status)
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


@router.post("/{task_id}/take")
async def take_task(task_id: int, request: Request):
    user = await get_user(request)
    executor_id = int(user["id"])

    if task_id <= 0:
        raise HTTPException(
            status_code=400,
            detail="Некорректный номер заказа",
        )

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
                    status_code=403,
                    detail="Выполнение заданий недоступно",
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
                raise HTTPException(
                    status_code=404,
                    detail="Заказ больше недоступен",
                )

            if task["customer_id"] == executor_id:
                raise HTTPException(
                    status_code=403,
                    detail="Нельзя выполнять собственный заказ",
                )

            banned = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM task_bans
                    WHERE task_id = $1 AND executor_id = $2
                )
                """,
                task_id,
                executor_id,
            )

            if banned:
                raise HTTPException(
                    status_code=403,
                    detail="Вы не можете повторно взять этот заказ",
                )

            existing = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM task_slots
                    WHERE task_id = $1
                      AND executor_id = $2
                      AND status IN ('taken', 'pending_review')
                )
                """,
                task_id,
                executor_id,
            )

            if existing:
                raise HTTPException(
                    status_code=409,
                    detail="Вы уже выполняете этот заказ",
                )

            slot = await conn.fetchrow(
                """
                SELECT id
                FROM task_slots
                WHERE task_id = $1 AND status = 'free'
                ORDER BY slot_number
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                task_id,
            )

            if not slot:
                raise HTTPException(
                    status_code=409,
                    detail="Свободных мест больше нет",
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
