
import asyncpg


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
            telegram_id,
            amount_kopecks,
            operation,
            idempotency_key,
            description,
            related_task_id
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


async def reserve_funds_tx(
    conn,
    customer_id: int,
    amount: int,
    key: str,
    task_id: int | None = None,
):
    """Резервирование средств внутри внешней транзакции."""
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
    """Возвращает деньги из резерва на доступный баланс."""
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


async def payout_tx(
    conn,
    customer_id: int,
    executor_id: int,
    amount: int,
    key: str,
    task_id: int | None = None,
):
    """Переводит полную сумму исполнителю без комиссии."""
    if amount <= 0:
        raise ValueError("Сумма должна быть положительной")

    if customer_id == executor_id:
        raise ValueError("Нельзя выполнять собственный заказ")

    customer_key = f"{key}:customer"
    executor_key = f"{key}:executor"

    if await _operation_exists(conn, customer_key) or \
       await _operation_exists(conn, executor_key):
        raise ValueError("Выплата уже выполнялась")

    # Блокируем кошельки в одинаковом порядке, чтобы снизить
    # вероятность взаимной блокировки транзакций.
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
        conn,
        customer_id,
        -amount,
        "task_payout",
        customer_key,
        "Оплата выполненного задания",
        task_id,
    )

    await _write_ledger(
        conn,
        executor_id,
        amount,
        "task_income",
        executor_key,
        "Доход за выполнение задания",
        task_id,
    )


async def credit_wallet(
    pool: asyncpg.Pool,
    telegram_id: int,
    amount: int,
    key: str,
):
    """Зачисление подтверждённого пополнения."""
    if amount < 1000:
        raise ValueError("Минимальное пополнение — 10 рублей")

    async with pool.acquire() as conn:
        async with conn.transaction():
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


async def reserve_task_funds(
    pool: asyncpg.Pool,
    telegram_id: int,
    amount: int,
    key: str,
):
    """Самостоятельное резервирование средств."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await reserve_funds_tx(
                conn,
                telegram_id,
                amount,
                key,
            )

    return True


async def release_reserved_funds(
    pool: asyncpg.Pool,
    telegram_id: int,
    amount: int,
    key: str,
):
    """Самостоятельный возврат средств из резерва."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await release_funds_tx(
                conn,
                telegram_id,
                amount,
                key,
            )

    return True


async def pay_executor(
    pool: asyncpg.Pool,
    customer_id: int,
    executor_id: int,
    amount: int,
    key: str,
):
    """Самостоятельная выплата исполнителю."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await payout_tx(
                conn,
                customer_id,
                executor_id,
                amount,
                key,
            )

    return True
