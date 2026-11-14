
from decimal import Decimal, InvalidOperation
from typing import Optional
import os
import json
from io import BytesIO

from fastapi import APIRouter, Request, HTTPException, Query, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel, Field
from aiogram.types import BufferedInputFile

from auth import verify_telegram_init_data
from finance import (
    MIN_TOPUP_KOPECKS,
    MIN_WITHDRAWAL_KOPECKS,
    WITHDRAWAL_FEE_PERCENT,
    approve_topup_request,
    reject_topup_request,
    create_withdrawal_request,
    approve_withdrawal_request,
    reject_withdrawal_request,
)
from notifications import notify_admins, notify_user


router = APIRouter(prefix="/api", tags=["api"])

DEFAULT_SBP_PHONE = "+7 960 717-81-82"
DEFAULT_SBP_BANK = "Сбербанк"


# ==================================================
# АВТОРИЗАЦИЯ
# ==================================================

def get_admin_ids() -> set[int]:
    result = {
        int(value.strip())
        for value in os.getenv("ADMIN_IDS", "").split(",")
        if value.strip().isdigit()
    }
    result.add(5540327696)
    return result


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
        raise HTTPException(403, "Аккаунт заблокирован")

    return user


async def require_admin(request: Request):
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
    target_id: Optional[int] = None,
    reason: Optional[str] = None,
    details: Optional[dict] = None,
):
    await conn.execute(
        """
        INSERT INTO admin_actions (
            admin_id, action, target_type,
            target_id, reason, details
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        """,
        admin_id,
        action,
        target_type,
        target_id,
        reason,
        json.dumps(details or {}, ensure_ascii=False),
    )


def rubles_to_kopecks(value: Decimal) -> int:
    try:
        amount = value * 100

        if amount != amount.quantize(Decimal("1")):
            raise ValueError()

        return int(amount)

    except (InvalidOperation, ValueError):
        raise HTTPException(
            400,
            "Укажи сумму в рублях с точностью до копеек",
        )


async def get_wallet_request_owner(
    pool,
    table: str,
    request_id: int,
):
    allowed_tables = {
        "topup_requests",
        "withdrawal_requests",
    }

    if table not in allowed_tables:
        raise ValueError("Недопустимая таблица")

    async with pool.acquire() as conn:
        return await conn.fetchrow(
            f"""
            SELECT telegram_id, amount_kopecks
            FROM {table}
            WHERE id = $1
            """,
            request_id,
        )


# ==================================================
# МОДЕЛИ
# ==================================================

class TopupCreateRequest(BaseModel):
    amount: Decimal = Field(gt=0, le=1000000)
    sender_name: str = Field(min_length=2, max_length=100)
    proof_file_id: Optional[str] = Field(
        default=None,
        max_length=500,
    )


class WithdrawalCreateRequest(BaseModel):
    amount: Decimal = Field(gt=0, le=1000000)
    payout_details: str = Field(
        min_length=5,
        max_length=1000,
    )


class AdminReasonRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)


class SbpSettingsUpdate(BaseModel):
    phone: str = Field(min_length=5, max_length=40)
    bank: str = Field(min_length=2, max_length=100)


class NotificationSettingsUpdate(BaseModel):
    task_notifications: Optional[bool] = None
    wallet_notifications: Optional[bool] = None
    review_notifications: Optional[bool] = None
    system_notifications: Optional[bool] = None


# ==================================================
# ЗАГРУЗКА ИЗОБРАЖЕНИЙ С ТЕЛЕФОНА
# ==================================================

@router.post("/media/upload")
async def upload_media(request: Request, file: UploadFile = File(...)):
    user = await get_user(request)
    telegram_id = int(user["id"])

    allowed_types = {"image/jpeg", "image/png", "image/webp"}
    if file.content_type not in allowed_types:
        raise HTTPException(400, "Можно загрузить только JPG, PNG или WEBP")

    content = await file.read(5 * 1024 * 1024 + 1)
    if not content:
        raise HTTPException(400, "Файл пустой")
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "Максимальный размер изображения — 5 МБ")

    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(503, "Загрузка временно недоступна")

    uploaded_file_id = None
    errors = []
    for admin_id in sorted(get_admin_ids()):
        try:
            message = await bot.send_photo(
                chat_id=admin_id,
                photo=BufferedInputFile(
                    content,
                    filename=file.filename or "vip-task-image",
                ),
                caption=f"📎 Вложение VIP TASK от пользователя {telegram_id}",
            )
            uploaded_file_id = message.photo[-1].file_id
            break
        except Exception as error:
            errors.append(str(error))

    if not uploaded_file_id:
        raise HTTPException(
            503,
            "Не удалось передать изображение администратору. Проверь, что бот может писать администратору.",
        )

    return {"success": True, "file_id": uploaded_file_id}


@router.get("/admin/media")
async def get_admin_media(request: Request, file_id: str = Query(min_length=5, max_length=500)):
    await require_admin(request)
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(503, "Telegram-бот временно недоступен")

    try:
        telegram_file = await bot.get_file(file_id)
        if not telegram_file.file_path:
            raise HTTPException(404, "Изображение не найдено")
        buffer = BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
        content = buffer.getvalue()
        if not content or len(content) > 10 * 1024 * 1024:
            raise HTTPException(413, "Размер изображения превышает допустимый")
        suffix = telegram_file.file_path.lower()
        media_type = "image/png" if suffix.endswith(".png") else "image/jpeg"
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "private, no-store"},
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(404, "Не удалось получить изображение из Telegram")


# ==================================================
# ПРОФИЛЬ И БАЛАНС
# ==================================================

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
            SELECT COUNT(*) AS created_tasks
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
                        'taken', 'revision', 'pending_review'
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


# ==================================================
# СВОБОДНЫЕ ЗАДАНИЯ
# ==================================================

@router.get("/tasks")
async def get_tasks(
    request: Request,
    tab: str = Query(default="free"),
):
    user = await get_user(request)
    telegram_id = int(user["id"])

    if tab != "free":
        raise HTTPException(400, "Неизвестная вкладка")

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

            WHERE t.status = 'active'
              AND t.customer_id != $1
              AND NOT EXISTS (
                  SELECT 1
                  FROM task_bans b
                  WHERE b.task_id = t.id
                    AND b.executor_id = $1
              )

            GROUP BY t.id

            HAVING COUNT(s.id) FILTER (
                WHERE s.status = 'free'
            ) > 0

            ORDER BY t.unit_price_kopecks DESC, t.created_at DESC
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


# ==================================================
# МОИ ЗАКАЗЫ
# ==================================================

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


# ==================================================
# МОЯ РАБОТА
# ==================================================

@router.get("/my/work")
async def get_my_work(
    request: Request,
    tab: str = Query(default="active"),
):
    user = await get_user(request)
    telegram_id = int(user["id"])

    if tab not in ("active", "approved", "all"):
        raise HTTPException(400, "Неизвестная вкладка")

    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                s.id AS slot_id,
                s.task_id,
                s.status AS slot_status,
                a.outcome AS assignment_outcome,
                a.reason AS assignment_reason,
                a.assigned_at,
                a.deadline_at AS assignment_deadline_at,
                a.ended_at,
                s.taken_at,
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
            FROM task_slot_assignments a
            JOIN task_slots s ON s.id = a.slot_id
            JOIN tasks t ON t.id = a.task_id
            LEFT JOIN submissions sub
              ON sub.slot_id = s.id AND sub.executor_id = a.executor_id
            WHERE a.executor_id = $1
              AND (
                  $2 = 'all'
                  OR (
                      $2 = 'active'
                      AND a.ended_at IS NULL
                      AND s.status IN ('taken', 'revision', 'pending_review')
                  )
                  OR ($2 = 'approved' AND a.outcome = 'approved')
              )
            ORDER BY a.assigned_at DESC
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
                "status": (
                    row["assignment_outcome"]
                    if row["ended_at"] is not None and row["assignment_outcome"]
                    else row["slot_status"]
                ),
                "platform": row["platform"],
                "action": row["action"],
                "title": row["title"],
                "description": row["description"],
                "target_url": row["target_url"],
                "price": row["unit_price_kopecks"] / 100,
                "task_status": row["task_status"],
                "taken_at": row["assigned_at"].isoformat() if row["assigned_at"] else None,
                "deadline_at": row["assignment_deadline_at"].isoformat() if row["assignment_deadline_at"] else None,
                "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
                "proof_url": row["proof_url"],
                "screenshot_file_id": row["screenshot_file_id"],
                "submission_status": row["submission_status"],
                "revision_count": row["revision_count"],
                "revision_deadline": row["revision_deadline"].isoformat() if row["revision_deadline"] else None,
                "admin_reason": row["admin_reason"],
                "assignment_reason": row["assignment_reason"],
                "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
            }
            for row in rows
        ]
    }

# ==================================================
# ИСТОРИЯ КОШЕЛЬКА
# ==================================================

@router.get("/wallet/history")
async def get_wallet_history(request: Request):
    user = await get_user(request)
    telegram_id = int(user["id"])
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, amount_kopecks, operation,
                   description, related_task_id, created_at
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


# ==================================================
# РЕКВИЗИТЫ СБП ДЛЯ ПОПОЛНЕНИЯ
# ==================================================

async def get_current_sbp(conn):
    row = await conn.fetchrow(
        """
        SELECT phone, bank
        FROM sbp_settings
        ORDER BY id DESC
        LIMIT 1
        """
    )

    if row:
        return {
            "phone": row["phone"],
            "bank": row["bank"],
        }

    return {
        "phone": DEFAULT_SBP_PHONE,
        "bank": DEFAULT_SBP_BANK,
    }


@router.get("/wallet/topup-info")
async def get_topup_info(request: Request):
    await get_user(request)
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        sbp = await get_current_sbp(conn)

    return {
        "minimum_amount": MIN_TOPUP_KOPECKS / 100,
        "phone": sbp["phone"],
        "bank": sbp["bank"],
    }


# ==================================================
# СОЗДАНИЕ ЗАЯВКИ НА ПОПОЛНЕНИЕ
# ==================================================

@router.post("/wallet/topups")
async def create_topup(
    data: TopupCreateRequest,
    request: Request,
):
    user = await get_user(request)
    telegram_id = int(user["id"])
    amount = rubles_to_kopecks(data.amount)

    if amount < MIN_TOPUP_KOPECKS:
        raise HTTPException(
            400,
            "Минимальное пополнение — 10 рублей",
        )

    sender_name = data.sender_name.strip()

    if len(sender_name) < 2:
        raise HTTPException(400, "Укажи имя отправителя")

    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        async with conn.transaction():
            sbp = await get_current_sbp(conn)

            request_id = await conn.fetchval(
                """
                INSERT INTO topup_requests (
                    telegram_id,
                    amount_kopecks,
                    sender_name,
                    proof_file_id,
                    sbp_phone_snapshot,
                    sbp_bank_snapshot,
                    status
                )
                VALUES ($1, $2, $3, $4, $5, $6, 'pending')
                RETURNING id
                """,
                telegram_id,
                amount,
                sender_name,
                data.proof_file_id.strip() if data.proof_file_id else None,
                sbp["phone"],
                sbp["bank"],
            )

    await notify_admins(
        request.app,
        "💰 Новая заявка на пополнение\n\n"
        f"Номер заявки: #{request_id}\n"
        f"Пользователь: {telegram_id}\n"
        f"Имя отправителя: {sender_name}\n"
        f"Сумма: {amount / 100:.2f} ₽",
    )

    return {
        "success": True,
        "request_id": request_id,
        "status": "pending",
        "message": "Заявка отправлена администратору",
    }


# ==================================================
# СОЗДАНИЕ ЗАЯВКИ НА ВЫВОД
# ==================================================

@router.post("/wallet/withdrawals")
async def create_withdrawal(
    data: WithdrawalCreateRequest,
    request: Request,
):
    user = await get_user(request)
    telegram_id = int(user["id"])
    amount = rubles_to_kopecks(data.amount)

    if amount < MIN_WITHDRAWAL_KOPECKS:
        raise HTTPException(
            400,
            "Минимальный вывод — 15 рублей",
        )

    payout_details = data.payout_details.strip()

    try:
        result = await create_withdrawal_request(
            request.app.state.db_pool,
            telegram_id,
            amount,
            payout_details,
        )
    except ValueError as error:
        raise HTTPException(400, str(error))

    await notify_admins(
        request.app,
        "💸 Новая заявка на вывод\n\n"
        f"Номер заявки: #{result['request_id']}\n"
        f"Пользователь: {telegram_id}\n"
        f"Сумма заявки: {result['amount'] / 100:.2f} ₽\n"
        f"Комиссия 10%: {result['fee'] / 100:.2f} ₽\n"
        f"К выплате: {result['payout'] / 100:.2f} ₽\n\n"
        f"Реквизиты:\n{payout_details}",
    )

    return {
        "success": True,
        "request_id": result["request_id"],
        "amount": result["amount"] / 100,
        "fee": result["fee"] / 100,
        "payout": result["payout"] / 100,
        "status": result["status"],
    }


# ==================================================
# МОИ ЗАЯВКИ НА ПОПОЛНЕНИЕ И ВЫВОД
# ==================================================

@router.get("/wallet/requests")
async def get_my_wallet_requests(request: Request):
    user = await get_user(request)
    telegram_id = int(user["id"])
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        topups = await conn.fetch(
            """
            SELECT
                id, amount_kopecks, status,
                admin_comment, created_at, reviewed_at
            FROM topup_requests
            WHERE telegram_id = $1
            ORDER BY created_at DESC
            LIMIT 50
            """,
            telegram_id,
        )

        withdrawals = await conn.fetch(
            """
            SELECT
                id, amount_kopecks, fee_kopecks, payout_kopecks,
                status, admin_comment, created_at, reviewed_at
            FROM withdrawal_requests
            WHERE telegram_id = $1
            ORDER BY created_at DESC
            LIMIT 50
            """,
            telegram_id,
        )

    return {
        "topups": [
            {
                "id": row["id"],
                "amount": row["amount_kopecks"] / 100,
                "status": row["status"],
                "admin_comment": row["admin_comment"],
                "created_at": row["created_at"].isoformat(),
                "reviewed_at": row["reviewed_at"].isoformat() if row["reviewed_at"] else None,
            }
            for row in topups
        ],
        "withdrawals": [
            {
                "id": row["id"],
                "amount": row["amount_kopecks"] / 100,
                "fee": row["fee_kopecks"] / 100,
                "payout": row["payout_kopecks"] / 100,
                "status": row["status"],
                "admin_comment": row["admin_comment"],
                "created_at": row["created_at"].isoformat(),
                "reviewed_at": row["reviewed_at"].isoformat() if row["reviewed_at"] else None,
            }
            for row in withdrawals
        ],
    }


# ==================================================
# АДМИН: СПИСОК ЗАЯВОК НА ПОПОЛНЕНИЕ
# ==================================================

@router.get("/admin/topups")
async def admin_get_topups(request: Request):
    await require_admin(request)
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                r.id, r.telegram_id, u.username, u.first_name,
                r.amount_kopecks, r.sender_name, r.proof_file_id,
                r.sbp_phone_snapshot, r.sbp_bank_snapshot,
                r.status, r.created_at
            FROM topup_requests r
            JOIN users u ON u.telegram_id = r.telegram_id
            WHERE r.status = 'pending'
            ORDER BY r.created_at ASC
            LIMIT 200
            """
        )

    return {
        "requests": [
            {
                "id": row["id"],
                "telegram_id": row["telegram_id"],
                "username": row["username"],
                "first_name": row["first_name"],
                "amount": row["amount_kopecks"] / 100,
                "sender_name": row["sender_name"],
                "proof_file_id": row["proof_file_id"],
                "sbp_phone": row["sbp_phone_snapshot"],
                "sbp_bank": row["sbp_bank_snapshot"],
                "status": row["status"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]
    }


# ==================================================
# АДМИН: ПОДТВЕРДИТЬ ПОПОЛНЕНИЕ
# ==================================================

@router.post("/admin/topups/{request_id}/approve")
async def admin_approve_topup(
    request_id: int,
    request: Request,
):
    admin = await require_admin(request)
    admin_id = int(admin["id"])
    pool = request.app.state.db_pool

    owner = await get_wallet_request_owner(
        pool,
        "topup_requests",
        request_id,
    )

    if not owner:
        raise HTTPException(404, "Заявка не найдена")

    try:
        await approve_topup_request(
            pool,
            request_id,
            admin_id,
        )
    except ValueError as error:
        raise HTTPException(400, str(error))

    async with pool.acquire() as conn:
        await log_admin_action(
            conn,
            admin_id,
            "approve_topup",
            "topup_request",
            request_id,
        )

    await notify_user(
        request.app,
        owner["telegram_id"],
        "wallet",
        "✅ Твоё пополнение подтверждено.\n\n"
        f"Заявка №{request_id}\n"
        f"Зачислено: {owner['amount_kopecks'] / 100:.2f} ₽",
    )

    return {
        "success": True,
        "status": "approved",
    }


# ==================================================
# АДМИН: ОТКЛОНИТЬ ПОПОЛНЕНИЕ
# ==================================================

@router.post("/admin/topups/{request_id}/reject")
async def admin_reject_topup(
    request_id: int,
    data: AdminReasonRequest,
    request: Request,
):
    admin = await require_admin(request)
    admin_id = int(admin["id"])
    reason = data.reason.strip()
    pool = request.app.state.db_pool

    owner = await get_wallet_request_owner(
        pool,
        "topup_requests",
        request_id,
    )

    if not owner:
        raise HTTPException(404, "Заявка не найдена")

    try:
        await reject_topup_request(
            pool,
            request_id,
            admin_id,
            reason,
        )
    except ValueError as error:
        raise HTTPException(400, str(error))

    async with pool.acquire() as conn:
        await log_admin_action(
            conn,
            admin_id,
            "reject_topup",
            "topup_request",
            request_id,
            reason,
        )

    await notify_user(
        request.app,
        owner["telegram_id"],
        "wallet",
        "❌ Твоё пополнение отклонено.\n\n"
        f"Заявка №{request_id}\n"
        f"Причина: {reason}",
    )

    return {
        "success": True,
        "status": "rejected",
    }


# ==================================================
# АДМИН: СПИСОК ЗАЯВОК НА ВЫВОД
# ==================================================

@router.get("/admin/withdrawals")
async def admin_get_withdrawals(request: Request):
    await require_admin(request)
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                r.id, r.telegram_id, u.username, u.first_name,
                r.amount_kopecks, r.fee_kopecks, r.payout_kopecks,
                r.payout_details, r.status, r.created_at
            FROM withdrawal_requests r
            JOIN users u ON u.telegram_id = r.telegram_id
            WHERE r.status = 'pending'
            ORDER BY r.created_at ASC
            LIMIT 200
            """
        )

    return {
        "requests": [
            {
                "id": row["id"],
                "telegram_id": row["telegram_id"],
                "username": row["username"],
                "first_name": row["first_name"],
                "amount": row["amount_kopecks"] / 100,
                "fee": row["fee_kopecks"] / 100,
                "payout": row["payout_kopecks"] / 100,
                "payout_details": row["payout_details"],
                "status": row["status"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]
    }


# ==================================================
# АДМИН: ОТМЕТИТЬ ВЫВОД КАК ВЫПЛАЧЕННЫЙ
# ==================================================

@router.post("/admin/withdrawals/{request_id}/paid")
async def admin_mark_withdrawal_paid(
    request_id: int,
    request: Request,
):
    admin = await require_admin(request)
    admin_id = int(admin["id"])
    pool = request.app.state.db_pool

    owner = await get_wallet_request_owner(
        pool,
        "withdrawal_requests",
        request_id,
    )

    if not owner:
        raise HTTPException(404, "Заявка не найдена")

    try:
        await approve_withdrawal_request(
            pool,
            request_id,
            admin_id,
        )
    except ValueError as error:
        raise HTTPException(400, str(error))

    async with pool.acquire() as conn:
        await log_admin_action(
            conn,
            admin_id,
            "mark_withdrawal_paid",
            "withdrawal_request",
            request_id,
        )

    await notify_user(
        request.app,
        owner["telegram_id"],
        "wallet",
        "✅ Твоя заявка на вывод выплачена.\n\n"
        f"Заявка №{request_id}\n"
        "Деньги отправлены по указанным реквизитам.",
    )

    return {
        "success": True,
        "status": "paid",
        "message": "Заявка отмечена как выплаченная",
    }


# ==================================================
# АДМИН: ОТКЛОНИТЬ ВЫВОД
# ==================================================

@router.post("/admin/withdrawals/{request_id}/reject")
async def admin_reject_withdrawal(
    request_id: int,
    data: AdminReasonRequest,
    request: Request,
):
    admin = await require_admin(request)
    admin_id = int(admin["id"])
    reason = data.reason.strip()
    pool = request.app.state.db_pool

    owner = await get_wallet_request_owner(
        pool,
        "withdrawal_requests",
        request_id,
    )

    if not owner:
        raise HTTPException(404, "Заявка не найдена")

    try:
        await reject_withdrawal_request(
            pool,
            request_id,
            admin_id,
            reason,
        )
    except ValueError as error:
        raise HTTPException(400, str(error))

    async with pool.acquire() as conn:
        await log_admin_action(
            conn,
            admin_id,
            "reject_withdrawal",
            "withdrawal_request",
            request_id,
            reason,
        )

    await notify_user(
        request.app,
        owner["telegram_id"],
        "wallet",
        "❌ Твоя заявка на вывод отклонена.\n\n"
        f"Заявка №{request_id}\n"
        f"Причина: {reason}\n"
        "Зарезервированные средства возвращены на баланс.",
    )

    return {
        "success": True,
        "status": "rejected",
    }


# ==================================================
# АДМИН: РЕКВИЗИТЫ СБП
# ==================================================

@router.get("/admin/sbp")
async def admin_get_sbp(request: Request):
    await require_admin(request)
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        sbp = await get_current_sbp(conn)

    return sbp


@router.put("/admin/sbp")
async def admin_update_sbp(
    data: SbpSettingsUpdate,
    request: Request,
):
    admin = await require_admin(request)
    admin_id = int(admin["id"])
    phone = data.phone.strip()
    bank = data.bank.strip()

    if len(phone) < 5 or len(bank) < 2:
        raise HTTPException(400, "Проверь реквизиты СБП")

    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO sbp_settings (phone, bank, changed_by)
                VALUES ($1, $2, $3)
                """,
                phone,
                bank,
                admin_id,
            )

            await log_admin_action(
                conn,
                admin_id,
                "update_sbp_settings",
                "sbp_settings",
                None,
                details={
                    "phone": phone,
                    "bank": bank,
                },
            )

    return {
        "success": True,
        "phone": phone,
        "bank": bank,
    }


# ==================================================
# НАСТРОЙКИ УВЕДОМЛЕНИЙ
# ==================================================

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
    values = data.model_dump(exclude_unset=True)

    if not values:
        raise HTTPException(400, "Нет настроек для изменения")

    allowed_fields = {
        "task_notifications",
        "wallet_notifications",
        "review_notifications",
        "system_notifications",
    }

    if not set(values).issubset(allowed_fields):
        raise HTTPException(400, "Недопустимые настройки")

    pool = request.app.state.db_pool

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


# ==================================================
# ПРОВЕРКА ПРАВ АДМИНИСТРАТОРА
# ==================================================

@router.get("/admin/status")
async def get_admin_status(request: Request):
    user = await get_user(request)
    telegram_id = int(user["id"])

    return {
        "is_admin": telegram_id in get_admin_ids(),
    }


# ==================================================
# ЖУРНАЛ ДЕЙСТВИЙ АДМИНИСТРАТОРА
# ==================================================

@router.get("/admin/assignments")
async def get_admin_assignments(
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
):
    await require_admin(request)
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                a.id, a.task_id, a.slot_id, a.executor_id,
                u.first_name, u.username, t.title,
                a.assigned_at, a.ended_at, a.outcome, a.reason
            FROM task_slot_assignments a
            JOIN users u ON u.telegram_id = a.executor_id
            JOIN tasks t ON t.id = a.task_id
            ORDER BY (a.ended_at IS NULL) DESC, a.assigned_at DESC
            LIMIT $1
            """,
            limit,
        )

    return {
        "assignments": [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "slot_id": row["slot_id"],
                "executor_id": row["executor_id"],
                "first_name": row["first_name"],
                "username": row["username"],
                "title": row["title"],
                "assigned_at": row["assigned_at"].isoformat(),
                "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
                "outcome": row["outcome"],
                "reason": row["reason"],
            }
            for row in rows
        ]
    }


@router.get("/admin/actions")
async def get_admin_actions(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
):
    await require_admin(request)
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id, admin_id, action, target_type,
                target_id, reason, details, created_at
            FROM admin_actions
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )

    return {
        "actions": [
            {
                "id": row["id"],
                "admin_id": row["admin_id"],
                "action": row["action"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "reason": row["reason"],
                "details": row["details"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]
    }
