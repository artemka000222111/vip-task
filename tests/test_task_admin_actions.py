import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
from starlette.requests import Request

from database import init_db
from tasks import ReviewReasonRequest, remove_executor


@unittest.skipUnless(os.getenv("DATABASE_URL"), "DATABASE_URL is required")
class RemoveExecutorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dsn = os.environ["DATABASE_URL"]
        self.schema = "vip_task_remove_" + uuid.uuid4().hex[:12]
        self.admin_pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
        async with self.admin_pool.acquire() as conn:
            await conn.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=2,
            server_settings={"search_path": self.schema},
        )
        await init_db(self.pool)

        self.customer_id = 2_100_000_000 + (uuid.uuid4().int % 100_000_000)
        self.executor_id = self.customer_id + 1
        self.admin_id = 5540327696

        async with self.pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO users (telegram_id, first_name) VALUES ($1, $2)",
                [
                    (self.customer_id, "Removal test customer"),
                    (self.executor_id, "Removal test executor"),
                    (self.admin_id, "Removal test admin"),
                ],
            )
            await conn.execute(
                """
                INSERT INTO wallets (telegram_id, available_kopecks, reserved_kopecks)
                VALUES ($1, 0, 1000), ($2, 0, 0)
                """,
                self.customer_id,
                self.executor_id,
            )
            self.task_id = await conn.fetchval(
                """
                INSERT INTO tasks (
                    customer_id, platform, action, title, description,
                    target_url, unit_price_kopecks, total_slots,
                    reserved_kopecks, initial_total_kopecks, status
                )
                VALUES (
                    $1, 'test', 'survey', 'Remove executor test',
                    'Integration test description', 'https://example.com',
                    1000, 1, 1000, 1000, 'active'
                )
                RETURNING id
                """,
                self.customer_id,
            )
            self.slot_id = await conn.fetchval(
                """
                INSERT INTO task_slots (
                    task_id, slot_number, executor_id, status,
                    taken_at, deadline_at
                )
                VALUES ($1, 1, $2, 'taken', NOW(), NOW() + INTERVAL '1 hour')
                RETURNING id
                """,
                self.task_id,
                self.executor_id,
            )
            await conn.execute(
                """
                INSERT INTO task_slot_assignments (
                    task_id, slot_id, executor_id, deadline_at
                )
                VALUES ($1, $2, $3, NOW() + INTERVAL '1 hour')
                """,
                self.task_id,
                self.slot_id,
                self.executor_id,
            )

        self.app = SimpleNamespace(state=SimpleNamespace(db_pool=self.pool, bot=None))
        self.request = Request({
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "app": self.app,
        })

    async def asyncTearDown(self):
        if getattr(self, "pool", None):
            await self.pool.close()
        if getattr(self, "admin_pool", None):
            async with self.admin_pool.acquire() as conn:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
            await self.admin_pool.close()

    async def test_removal_frees_slot_keeps_reserve_and_bans_executor(self):
        with patch(
            "tasks.require_admin",
            new=AsyncMock(return_value={"id": self.admin_id}),
        ), patch("tasks.notify_user", new=AsyncMock()):
            result = await remove_executor(
                self.task_id,
                self.slot_id,
                ReviewReasonRequest(reason="Проверка администратором"),
                self.request,
            )

        self.assertTrue(result["success"])
        async with self.pool.acquire() as conn:
            slot = await conn.fetchrow(
                "SELECT status, executor_id FROM task_slots WHERE id = $1",
                self.slot_id,
            )
            assignment = await conn.fetchrow(
                """
                SELECT ended_at, outcome, reason
                FROM task_slot_assignments
                WHERE slot_id = $1 AND executor_id = $2
                """,
                self.slot_id,
                self.executor_id,
            )
            ban_count = await conn.fetchval(
                "SELECT COUNT(*) FROM task_bans WHERE task_id = $1 AND executor_id = $2",
                self.task_id,
                self.executor_id,
            )
            reserve = await conn.fetchval(
                "SELECT reserved_kopecks FROM wallets WHERE telegram_id = $1",
                self.customer_id,
            )

        self.assertEqual(slot["status"], "free")
        self.assertIsNone(slot["executor_id"])
        self.assertIsNotNone(assignment["ended_at"])
        self.assertEqual(assignment["outcome"], "removed")
        self.assertEqual(assignment["reason"], "Проверка администратором")
        self.assertEqual(ban_count, 1)
        self.assertEqual(reserve, 1000)


if __name__ == "__main__":
    unittest.main()
