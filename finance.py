
import asyncpg


MIN_TOPUP_KOPECKS = 1000
MIN_WITHDRAWAL_KOPECKS = 1500
WITHDRAWAL_FEE_PERCENT = 10


def calculate_withdrawal(amount_kopecks: int) -> tuple[int, int]:
    if amount_kopecks < MIN_WITHDRAWAL_KOPECKS:
        raise ValueError("Минимальный вывод — 15 рублей")
    fee = amount_kopecks * WITHDRAWAL_FEE_PERCENT // 100
    payout = amount_kopecks - fee
    if payout <= 0:
        raise ValueError("Сумма выплаты некорректна")
    return fee, payout


async def _operation_exists(conn, key: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM ledger WHERE idempotency_key = $1)",
        key,
    )


async def _write_ledger(
    conn,
    telegram_id: int,
    amount: int,
    operation: str,
    key: str,
    description: str = "",
    task_id: int | None = None,
):
    await conn.execute(
        """
        INSERT INTO ledger (
            telegram_id, amount_kopecks, operation,
            idempotency_key, description, related_task_id
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        telegram_id,
        amount,
        operation,
        key,
        description,
        task_id,
    )


# ==================================================
# ЗАЧИСЛЕНИЕ СРЕДСТВ ВНУТРИ ТРАНЗАКЦИИ
# ==================================================

async def credit_wallet_tx(conn, telegram_id: int, amount: int, key: str):
    if amount < MIN_TOPUP_KOPECKS:
        raise ValueError("Минимальное пополнение — 10 рублей")

    if await _operation_exists(conn, key):
        return False

    wallet = await conn.fetchrow(
        """
        SELECT telegram_id
        FROM wallets
        WHERE telegram_id = $1
        FOR UPDATE
        """,
        telegram_id,
    )

    if not wallet:
        raise ValueError("Кошелёк пользователя не найден")

    await conn.execute(
        """
        UPDATE wallets
        SET available_kopecks = available_kopecks + $1,
            updated_at = NOW()
        WHERE telegram_id = $2
        """,
        amount,
        telegram_id,
    )

    await _write_ledger(
        conn,
        telegram_id,
        amount,
        "topup",
        key,
        "Подтверждённое пополнение баланса",
    )

    return True


# ==================================================
# РЕЗЕРВИРОВАНИЕ СРЕДСТВ ЗАКАЗЧИКА
# ==================================================

async def reserve_funds_tx(
    conn,
    customer_id: int,
    amount: int,
    key: str,
    task_id: int | None = None,
):
    if amount <= 0:
        raise ValueError("Сумма должна быть положительной")

    if await _operation_exists(conn, key):
        raise ValueError("Операция уже выполнена")

    wallet = await conn.fetchrow(
        """
        SELECT available_kopecks
        FROM wallets
        WHERE telegram_id = $1
        FOR UPDATE
        """,
        customer_id,
    )

    if not wallet:
        raise ValueError("Кошелёк не найден")

    if wallet["available_kopecks"] < amount:
        raise ValueError("Недостаточно средств")

    await conn.execute(
        """
        UPDATE wallets
        SET available_kopecks = available_kopecks - $1,
            reserved_kopecks = reserved_kopecks + $1,
            updated_at = NOW()
        WHERE telegram_id = $2
        """,
        amount,
        customer_id,
    )

    await _write_ledger(
        conn,
        customer_id,
        -amount,
        "reserve",
        key,
        "Резервирование средств заказа",
        task_id,
    )


async def release_funds_tx(
    conn,
    customer_id: int,
    amount: int,
    key: str,
    task_id: int | None = None,
):
    if amount <= 0:
        raise ValueError("Сумма должна быть положительной")

    if await _operation_exists(conn, key):
        raise ValueError("Операция уже выполнена")

    wallet = await conn.fetchrow(
        """
        SELECT reserved_kopecks
        FROM wallets
        WHERE telegram_id = $1
        FOR UPDATE
        """,
        customer_id,
    )

    if not wallet or wallet["reserved_kopecks"] < amount:
        raise ValueError("Недостаточно средств в резерве")

    await conn.execute(
        """
        UPDATE wallets
        SET reserved_kopecks = reserved_kopecks - $1,
            available_kopecks = available_kopecks + $1,
            updated_at = NOW()
        WHERE telegram_id = $2
        """,
        amount,
        customer_id,
    )

    await _write_ledger(
        conn,
        customer_id,
        amount,
        "release",
        key,
        "Возврат зарезервированных средств",
        task_id,
    )


# ==================================================
# ВЫПЛАТА ИСПОЛНИТЕЛЮ ЗА ЗАДАНИЕ
# ==================================================

async def payout_tx(
    conn,
    customer_id: int,
    executor_id: int,
    amount: int,
    key: str,
    task_id: int | None = None,
):
    if amount <= 0:
        raise ValueError("Сумма должна быть положительной")

    if customer_id == executor_id:
        raise ValueError("Нельзя выполнять собственный заказ")

    customer_key = f"{key}:customer"
    executor_key = f"{key}:executor"

    if (
        await _operation_exists(conn, customer_key)
        or await _operation_exists(conn, executor_key)
    ):
        raise ValueError("Выплата уже выполнялась")

    ids = sorted([customer_id, executor_id])

    wallets = await conn.fetch(
        """
        SELECT telegram_id, reserved_kopecks
        FROM wallets
        WHERE telegram_id = ANY($1::bigint[])
        ORDER BY telegram_id
        FOR UPDATE
        """,
        ids,
    )

    if len(wallets) != 2:
        raise ValueError("Не найден кошелёк одного из пользователей")

    customer_wallet = next(
        item for item in wallets
        if item["telegram_id"] == customer_id
    )

    if customer_wallet["reserved_kopecks"] < amount:
        raise ValueError("Недостаточно денег в резерве заказа")

    await conn.execute(
        """
        UPDATE wallets
        SET reserved_kopecks = reserved_kopecks - $1,
            updated_at = NOW()
        WHERE telegram_id = $2
        """,
        amount,
        customer_id,
    )

    await conn.execute(
        """
        UPDATE wallets
        SET available_kopecks = available_kopecks + $1,
            updated_at = NOW()
        WHERE telegram_id = $2
        """,
        amount,
        executor_id,
    )

    await _write_ledger(
        conn, customer_id, -amount, "task_payout",
        customer_key, "Оплата выполненного задания", task_id
    )

    await _write_ledger(
        conn, executor_id, amount, "task_income",
        executor_key, "Доход за выполнение задания", task_id
    )


# ==================================================
# ПОДТВЕРЖДЕНИЕ ПОПОЛНЕНИЯ АДМИНИСТРАТОРОМ
# ==================================================

async def approve_topup_request(
    pool: asyncpg.Pool,
    request_id: int,
    admin_id: int,
):
    async with pool.acquire() as conn:
        async with conn.transaction():
            request = await conn.fetchrow(
                """
                SELECT *
                FROM topup_requests
                WHERE id = $1
                FOR UPDATE
                """,
                request_id,
            )

            if not request:
                raise ValueError("Заявка на пополнение не найдена")

            if request["status"] != "pending":
                raise ValueError("Заявка уже обработана")

            await credit_wallet_tx(
                conn,
                request["telegram_id"],
                request["amount_kopecks"],
                f"topup_request:{request_id}",
            )

            await conn.execute(
                """
                UPDATE topup_requests
                SET status = 'approved',
                    admin_id = $1,
                    reviewed_at = NOW()
                WHERE id = $2
                """,
                admin_id,
                request_id,
            )

    return True


# ==================================================
# ОТКЛОНЕНИЕ ПОПОЛНЕНИЯ
# ==================================================

async def reject_topup_request(
    pool: asyncpg.Pool,
    request_id: int,
    admin_id: int,
    reason: str,
):
    async with pool.acquire() as conn:
        async with conn.transaction():
            request = await conn.fetchrow(
                """
                SELECT id, status
                FROM topup_requests
                WHERE id = $1
                FOR UPDATE
                """,
                request_id,
            )

            if not request:
                raise ValueError("Заявка не найдена")

            if request["status"] != "pending":
                raise ValueError("Заявка уже обработана")

            await conn.execute(
                """
                UPDATE topup_requests
                SET status = 'rejected',
                    admin_id = $1,
                    admin_comment = $2,
                    reviewed_at = NOW()
                WHERE id = $3
                """,
                admin_id,
                reason,
                request_id,
            )

    return True


# ==================================================
# СОЗДАНИЕ ЗАЯВКИ НА ВЫВОД
# ==================================================

async def create_withdrawal_request(
    pool: asyncpg.Pool,
    telegram_id: int,
    amount: int,
    payout_details: str,
):
    fee, payout = calculate_withdrawal(amount)

    if not payout_details or len(payout_details.strip()) < 5:
        raise ValueError("Укажи корректные платёжные реквизиты")

    if len(payout_details) > 1000:
        raise ValueError("Слишком длинные реквизиты")

    async with pool.acquire() as conn:
        async with conn.transaction():
            wallet = await conn.fetchrow(
                """
                SELECT available_kopecks
                FROM wallets
                WHERE telegram_id = $1
                FOR UPDATE
                """,
                telegram_id,
            )

            if not wallet:
                raise ValueError("Кошелёк не найден")

            if wallet["available_kopecks"] < amount:
                raise ValueError("Недостаточно средств")

            request_id = await conn.fetchval(
                """
                INSERT INTO withdrawal_requests (
                    telegram_id,
                    amount_kopecks,
                    payout_details,
                    fee_kopecks,
                    payout_kopecks,
                    status
                )
                VALUES ($1, $2, $3, $4, $5, 'pending')
                RETURNING id
                """,
                telegram_id,
                amount,
                payout_details.strip(),
                fee,
                payout,
            )

            await conn.execute(
                """
                UPDATE wallets
                SET available_kopecks = available_kopecks - $1,
                    updated_at = NOW()
                WHERE telegram_id = $2
                """,
                amount,
                telegram_id,
            )

            await _write_ledger(
                conn,
                telegram_id,
                -amount,
                "withdrawal_request",
                f"withdrawal:{request_id}",
                "Резервирование средств для вывода",
            )

    return {
        "request_id": request_id,
        "amount": amount,
        "fee": fee,
        "payout": payout,
        "status": "pending",
    }


# ==================================================
# ОДОБРЕНИЕ ВЫВОДА АДМИНИСТРАТОРОМ
# ==================================================

async def approve_withdrawal_request(
    pool: asyncpg.Pool,
    request_id: int,
    admin_id: int,
):
    async with pool.acquire() as conn:
        async with conn.transaction():
            request = await conn.fetchrow(
                """
                SELECT id, status, fee_kopecks
                FROM withdrawal_requests
                WHERE id = $1
                FOR UPDATE
                """,
                request_id,
            )

            if not request:
                raise ValueError("Заявка на вывод не найдена")

            if request["status"] != "pending":
                raise ValueError("Заявка уже обработана")

            await conn.execute(
                """
                UPDATE withdrawal_requests
                SET status = 'paid',
                    admin_id = $1,
                    reviewed_at = NOW()
                WHERE id = $2
                """,
                admin_id,
                request_id,
            )

            if request["fee_kopecks"] > 0:
                await conn.execute(
                    """
                    INSERT INTO platform_revenue (
                        idempotency_key, event_type, source_id, amount_kopecks
                    )
                    VALUES ($1, 'withdrawal_fee', $2, $3)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    f"withdrawal:{request_id}:platform_fee",
                    request_id,
                    request["fee_kopecks"],
                )

    return True


# ==================================================
# ОТКЛОНЕНИЕ ВЫВОДА И ВОЗВРАТ ДЕНЕГ
# ==================================================

async def reject_withdrawal_request(
    pool: asyncpg.Pool,
    request_id: int,
    admin_id: int,
    reason: str,
):
    async with pool.acquire() as conn:
        async with conn.transaction():
            request = await conn.fetchrow(
                """
                SELECT *
                FROM withdrawal_requests
                WHERE id = $1
                FOR UPDATE
                """,
                request_id,
            )

            if not request:
                raise ValueError("Заявка на вывод не найдена")

            if request["status"] != "pending":
                raise ValueError("Заявка уже обработана")

            telegram_id = request["telegram_id"]
            amount = request["amount_kopecks"]

            wallet = await conn.fetchrow(
                """
                SELECT telegram_id
                FROM wallets
                WHERE telegram_id = $1
                FOR UPDATE
                """,
                telegram_id,
            )

            if not wallet:
                raise ValueError("Кошелёк пользователя не найден")

            await conn.execute(
                """
                UPDATE wallets
                SET available_kopecks = available_kopecks + $1,
                    updated_at = NOW()
                WHERE telegram_id = $2
                """,
                amount,
                telegram_id,
            )

            await _write_ledger(
                conn,
                telegram_id,
                amount,
                "withdrawal_refund",
                f"withdrawal_refund:{request_id}",
                "Возврат средств по отклонённой заявке на вывод",
            )

            await conn.execute(
                """
                UPDATE withdrawal_requests
                SET status = 'rejected',
                    admin_id = $1,
                    admin_comment = $2,
                    reviewed_at = NOW()
                WHERE id = $3
                """,
                admin_id,
                reason,
                request_id,
            )

    return True


# ==================================================
# СТАРЫЕ ПУБЛИЧНЫЕ ФУНКЦИИ
# ==================================================

async def credit_wallet(
    pool: asyncpg.Pool,
    telegram_id: int,
    amount: int,
    key: str,
):
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await credit_wallet_tx(
                conn, telegram_id, amount, key
            )


async def reserve_task_funds(
    pool: asyncpg.Pool,
    telegram_id: int,
    amount: int,
    key: str,
):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await reserve_funds_tx(conn, telegram_id, amount, key)

    return True


async def release_reserved_funds(
    pool: asyncpg.Pool,
    telegram_id: int,
    amount: int,
    key: str,
):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await release_funds_tx(conn, telegram_id, amount, key)

    return True


async def pay_executor(
    pool: asyncpg.Pool,
    customer_id: int,
    executor_id: int,
    amount: int,
    key: str,
):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await payout_tx(
                conn, customer_id, executor_id, amount, key
            )

    return True
