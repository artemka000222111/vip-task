
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from uuid import uuid4

from api import get_user
from finance import reserve_task_funds


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


ALLOWED_ACTIONS = {
    "content_feedback",
    "usability_test",
    "survey",
}

PRICE_KOPECKS = {
    "content_feedback": 1000,
    "usability_test": 1000,
    "survey": 1000,
}


class CreateTaskRequest(BaseModel):
    action: str
    platform: str = Field(min_length=2, max_length=40)
    title: str = Field(min_length=3, max_length=100)
    description: str = Field(min_length=5, max_length=2000)
    target_url: str = Field(min_length=5, max_length=1000)
    quantity: int = Field(ge=1, le=10000)


@router.post("/create")
async def create_task(data: CreateTaskRequest, request: Request):
    user = await get_user(request)
    customer_id = int(user["id"])

    if data.action not in ALLOWED_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail="Такой тип задания пока не поддерживается",
        )

    unit_price = PRICE_KOPECKS[data.action]
    total_amount = unit_price * data.quantity
    pool = request.app.state.db_pool

    task_key = f"task-reserve-{uuid4()}"

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await reserve_task_funds(
                    pool,
                    customer_id,
                    total_amount,
                    task_key,
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
                        status
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'active')
                    RETURNING id
                    """,
                    customer_id,
                    data.platform,
                    data.action,
                    data.title,
                    data.description,
                    data.target_url,
                    unit_price,
                    data.quantity,
                    total_amount,
                )

                await conn.executemany(
                    """
                    INSERT INTO task_slots (task_id, status)
                    VALUES ($1, 'free')
                    """,
                    [(task_id,) for _ in range(data.quantity)],
                )

        return {
            "success": True,
            "task_id": task_id,
            "quantity": data.quantity,
            "unit_price": unit_price / 100,
            "total_price": total_amount / 100,
        }

    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
