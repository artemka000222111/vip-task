
async def credit_wallet(pool, telegram_id: int, amount: int, key: str):
    """Зачисление денег. amount — копейки."""
    if amount <= 0:
        raise ValueError("Сумма должна быть положительной")

    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval(
                "SELECT 1 FROM ledger WHERE idempotency_key = $1",
                key,
            )
            if exists:
                return False

            wallet = await conn.fetchrow(
                """
                SELECT telegram_id FROM wallets
                WHERE telegram_id = $1 FOR UPDATE
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

            await conn.execute(
                """
                INSERT INTO ledger
                    (telegram_id, amount_kopecks, operation,
                     idempotency_key, description)
                VALUES ($1, $2, 'credit', $3, 'Зачисление средств')
                """,
                telegram_id,
                amount,
                key,
            )

    return True


async def reserve_task_funds(pool, telegram_id: int, amount: int, key: str):
    """Перевод денег с доступного баланса в резерв."""
    if amount <= 0:
        raise ValueError("Сумма должна быть положительной")

    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval(
                "SELECT 1 FROM ledger WHERE idempotency_key = $1",
                key,
            )
            if exists:
                return False

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

            await conn.execute(
                """
                UPDATE wallets
                SET available_kopecks = available_kopecks - $1,
                    reserved_kopecks = reserved_kopecks + $1,
                    updated_at = NOW()
                WHERE telegram_id = $2
                """,
                amount,
                telegram_id,
            )

            await conn.execute(
                """
                INSERT INTO ledger
                    (telegram_id, amount_kopecks, operation,
                     idempotency_key, description)
                VALUES ($1, $2, 'reserve', $3, 'Резервирование заказа')
                """,
                telegram_id,
                -amount,
                key,
            )

    return True


async def release_reserved_funds(
    pool,
    telegram_id: int,
    amount: int,
    key: str,
):
    """Возврат зарезервированных средств на доступный баланс."""
    if amount <= 0:
        raise ValueError("Сумма должна быть положительной")

    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval(
                "SELECT 1 FROM ledger WHERE idempotency_key = $1",
                key,
            )
            if exists:
                return False

            wallet = await conn.fetchrow(
                """
                SELECT reserved_kopecks
                FROM wallets
                WHERE telegram_id = $1
                FOR UPDATE
                """,
                telegram_id,
            )

            if not wallet or wallet["reserved_kopecks"] < amount:
                raise ValueError("Недостаточно зарезервированных средств")

            await conn.execute(
                """
                UPDATE wallets
                SET reserved_kopecks = reserved_kopecks - $1,
                    available_kopecks = available_kopecks + $1,
                    updated_at = NOW()
                WHERE telegram_id = $2
                """,
                amount,
                telegram_id,
            )

            await conn.execute(
                """
                INSERT INTO ledger
                    (telegram_id, amount_kopecks, operation,
                     idempotency_key, description)
                VALUES ($1, $2, 'release', $3, 'Возврат резерва')
                """,
                telegram_id,
                amount,
                key,
            )

    return True


async def pay_executor(
    pool,
    customer_id: int,
    executor_id: int,
    amount: int,
    key: str,
):
    """Выплата исполнителю из резерва заказчика без комиссии."""
    if amount <= 0:
        raise ValueError("Сумма должна быть положительной")

    if customer_id == executor_id:
        raise ValueError("Нельзя выплатить деньги самому себе")

    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval(
                """
                SELECT 1 FROM ledger
                WHERE idempotency_key IN ($1, $2)
                """,
                key + ":customer",
                key + ":executor",
            )
            if exists:
                return False

            for user_id in sorted([customer_id, executor_id]):
                wallet = await conn.fetchrow(
                    """
                    SELECT telegram_id, reserved_kopecks
                    FROM wallets
                    WHERE telegram_id = $1
                    FOR UPDATE
                    """,
                    user_id,
                )
                if not wallet:
                    raise ValueError("Кошелёк пользователя не найден")

            customer_wallet = await conn.fetchrow(
                """
                SELECT reserved_kopecks
                FROM wallets
                WHERE telegram_id = $1
                """,
                customer_id,
            )

            if customer_wallet["reserved_kopecks"] < amount:
                raise ValueError("Недостаточно средств в резерве заказа")

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

            await conn.execute(
                """
                INSERT INTO ledger
                    (telegram_id, amount_kopecks, operation,
                     idempotency_key, description)
                VALUES
                    ($1, $2, 'task_payout', $3, 'Выплата исполнителю'),
                    ($4, $5, 'task_income', $6, 'Доход за выполнение задания')
                """,
                customer_id,
                -amount,
                key + ":customer",
                executor_id,
                amount,
                key + ":executor",
            )

    return True
