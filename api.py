
from fastapi import APIRouter, Request, HTTPException
from auth import verify_telegram_init_data

router = APIRouter(prefix="/api")


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
                    first_name = EXCLUDED.first_name
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

    return user


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

    return {
        "telegram_id": telegram_id,
        "username": user.get("username"),
        "first_name": user.get("first_name", ""),
        "available_balance": wallet["available_kopecks"] / 100,
        "reserved_balance": wallet["reserved_kopecks"] / 100,
    }


@router.get("/tasks")
async def get_tasks(request: Request):
    await get_user(request)
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
                COUNT(s.id) FILTER (WHERE s.status = 'free')
                    AS available_slots,
                COUNT(s.id) FILTER (WHERE s.status = 'taken')
                    AS taken_slots,
                COUNT(s.id) FILTER (WHERE s.status = 'pending_review')
                    AS pending_review_slots
            FROM tasks t
            LEFT JOIN task_slots s ON s.task_id = t.id
            WHERE t.status = 'active'
            GROUP BY t.id
            HAVING COUNT(s.id) FILTER (WHERE s.status = 'free') > 0
            ORDER BY t.unit_price_kopecks DESC, t.created_at DESC
            LIMIT 100
            """
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
            }
            for row in rows
        ]
    }
