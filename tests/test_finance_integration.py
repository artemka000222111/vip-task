import os
import unittest

import asyncpg

from database import init_db
from finance import (
    approve_withdrawal_request,
    create_withdrawal_request,
    payout_tx,
    reject_withdrawal_request,
)


@unittest.skipUnless(os.getenv("DATABASE_URL"), "DATABASE_URL is required")
class FinanceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        await init_db(self.pool)

    async def asyncTearDown(self):
        await self.pool.close()

    async def _user(self, telegram_id: int, available: int = 0, reserved: int = 0):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO users (telegram_id, first_name)
                    VALUES ($1, 'Finance integration test')
                    ON CONFLICT (telegram_id) DO NOTHING
                    """,
                    telegram_id,
                )
                await conn.execute(
                    """
                    INSERT INTO wallets (telegram_id, available_kopecks, reserved_kopecks)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (telegram_id) DO UPDATE
                    SET available_kopecks = EXCLUDED.available_kopecks,
                        reserved_kopecks = EXCLUDED.reserved_kopecks
                    """,
                    telegram_id, available, reserved,
                )

    async def test_withdrawal_approval_records_fee_once(self):
        user_id = 900000011
        await self._user(user_id, available=5000)

        request = await create_withdrawal_request(
            self.pool, user_id, 1500, "Карта 0000"
        )
        self.assertEqual(request["fee"], 150)
        self.assertEqual(request["payout"], 1350)

        async with self.pool.acquire() as conn:
            balance = await conn.fetchval(
                "SELECT available_kopecks FROM wallets WHERE telegram_id = $1",
                user_id,
            )
        self.assertEqual(balance, 3500)

        await approve_withdrawal_request(self.pool, request["request_id"], 5540327696)

        with self.assertRaises(ValueError):
            await approve_withdrawal_request(self.pool, request["request_id"], 5540327696)

        async with self.pool.acquire() as conn:
            revenue = await conn.fetchval(
                """
                SELECT COUNT(*) FROM platform_revenue
                WHERE idempotency_key = $1 AND amount_kopecks = 150
                """,
                f"withdrawal:{request['request_id']}:platform_fee",
            )
            status = await conn.fetchval(
                "SELECT status FROM withdrawal_requests WHERE id = $1",
                request["request_id"],
            )
        self.assertEqual(revenue, 1)
        self.assertEqual(status, "paid")

    async def test_withdrawal_rejection_refunds_full_amount_without_fee(self):
        user_id = 900000012
        await self._user(user_id, available=5000)

        request = await create_withdrawal_request(
            self.pool, user_id, 2500, "Карта 1111"
        )
        await reject_withdrawal_request(
            self.pool, request["request_id"], 5540327696, "Проверка"
        )

        with self.assertRaises(ValueError):
            await reject_withdrawal_request(
                self.pool, request["request_id"], 5540327696, "Повтор"
            )

        async with self.pool.acquire() as conn:
            balance = await conn.fetchval(
                "SELECT available_kopecks FROM wallets WHERE telegram_id = $1",
                user_id,
            )
            revenue = await conn.fetchval(
                "SELECT COUNT(*) FROM platform_revenue WHERE source_id = $1",
                request["request_id"],
            )
        self.assertEqual(balance, 5000)
        self.assertEqual(revenue, 0)

    async def test_task_payout_moves_full_amount_once(self):
        customer_id = 900000013
        executor_id = 900000014
        await self._user(customer_id, reserved=3000)
        await self._user(executor_id)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await payout_tx(
                    conn, customer_id, executor_id, 1200,
                    "integration:task:slot:1", None,
                )

        with self.assertRaises(ValueError):
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await payout_tx(
                        conn, customer_id, executor_id, 1200,
                        "integration:task:slot:1", None,
                    )

        async with self.pool.acquire() as conn:
            customer = await conn.fetchrow(
                "SELECT available_kopecks, reserved_kopecks FROM wallets WHERE telegram_id = $1",
                customer_id,
            )
            executor = await conn.fetchrow(
                "SELECT available_kopecks, reserved_kopecks FROM wallets WHERE telegram_id = $1",
                executor_id,
            )
        self.assertEqual(customer["reserved_kopecks"], 1800)
        self.assertEqual(executor["available_kopecks"], 1200)


if __name__ == "__main__":
    unittest.main()
