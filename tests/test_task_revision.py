import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
from fastapi import HTTPException
from starlette.requests import Request

from database import init_db
from tasks import (
    ReviewReasonRequest,
    SubmitTaskRequest,
    remove_executor,
    request_revision,
    submit_task,
)


@unittest.skipUnless(os.getenv("DATABASE_URL"), "DATABASE_URL is required")
class TaskRevisionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dsn = os.environ["DATABASE_URL"]
        self.schema = "vip_task_revision_" + uuid.uuid4().hex[:12]
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

        self.customer_id = 2_000_000_000 + (uuid.uuid4().int % 100_000_000)
        self.executor_id = self.customer_id + 1
        self.admin_id = 5540327696
        self.new_screenshot_file_id = "new-file-" + uuid.uuid4().hex

        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO users (telegram_id, first_name)
                VALUES ($1, $2)
                """,
                [
                    (self.customer_id, "Revision test customer"),
                    (self.executor_id, "Revision test executor"),
                    (self.admin_id, "Revision test admin"),
                ],
            )
            self.task_id = await conn.fetchval(
                """
                INSERT INTO tasks (
                    customer_id, platform, action, title, description,
                    target_url, unit_price_kopecks, total_slots,
                    reserved_kopecks, initial_total_kopecks, status
                )
                VALUES (
                    $1, 'test', 'survey', 'Revision lifecycle',
                    'Integration test task description',
                    'https://example.com/task', 1000, 1, 1000, 1000, 'active'
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
                VALUES ($1, 1, $2, 'revision', NOW() - INTERVAL '30 minutes',
                        NOW() + INTERVAL '30 minutes')
                RETURNING id
                """,
                self.task_id,
                self.executor_id,
            )
            await conn.execute(
                """
                INSERT INTO task_slot_assignments (
                    task_id, slot_id, executor_id, assigned_at, deadline_at
                )
                VALUES ($1, $2, $3, NOW() - INTERVAL '30 minutes',
                        NOW() + INTERVAL '30 minutes')
                """,
                self.task_id,
                self.slot_id,
                self.executor_id,
            )
            await conn.execute(
                """
                INSERT INTO uploaded_media (file_id, telegram_id)
                VALUES ($1, $2)
                """,
                self.new_screenshot_file_id,
                self.executor_id,
            )
            await conn.execute(
                """
                INSERT INTO submissions (
                    slot_id, executor_id, proof_url, screenshot_file_id,
                    status, revision_count, revision_deadline, admin_reason,
                    reviewed_by, reviewed_at
                )
                VALUES (
                    $1, $2, 'https://example.com/old-result', 'old-file-id',
                    'revision_requested', 1, NOW() + INTERVAL '30 minutes',
                    'Исправь результат', $3, NOW()
                )
                """,
                self.slot_id,
                self.executor_id,
                self.admin_id,
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

    async def test_revision_resubmission_preserves_one_revision_and_clears_deadline(self):
        with patch(
            "tasks.get_user",
            new=AsyncMock(return_value={"id": self.executor_id}),
        ), patch("tasks.notify_admins", new=AsyncMock()):
            result = await submit_task(
                self.task_id,
                self.slot_id,
                SubmitTaskRequest(
                    proof_url="https://example.com/new-result",
                    screenshot_file_id=self.new_screenshot_file_id,
                ),
                self.request,
            )

        self.assertEqual(result["status"], "pending_review")

        async with self.pool.acquire() as conn:
            submission = await conn.fetchrow(
                """
                SELECT proof_url, screenshot_file_id, status, revision_count,
                       revision_deadline, created_at
                FROM submissions WHERE slot_id = $1
                """,
                self.slot_id,
            )
            slot = await conn.fetchrow(
                "SELECT status, deadline_at FROM task_slots WHERE id = $1",
                self.slot_id,
            )
            assignment_deadline = await conn.fetchval(
                """
                SELECT deadline_at FROM task_slot_assignments
                WHERE slot_id = $1 AND executor_id = $2 AND ended_at IS NULL
                """,
                self.slot_id,
                self.executor_id,
            )

        self.assertEqual(submission["proof_url"], "https://example.com/new-result")
        self.assertEqual(submission["screenshot_file_id"], self.new_screenshot_file_id)
        self.assertEqual(submission["status"], "pending")
        self.assertEqual(submission["revision_count"], 1)
        self.assertIsNone(submission["revision_deadline"])
        self.assertEqual(slot["status"], "pending_review")
        self.assertIsNone(slot["deadline_at"])
        self.assertIsNone(assignment_deadline)

        with patch(
            "tasks.require_admin",
            new=AsyncMock(return_value={"id": self.admin_id}),
        ):
            with self.assertRaises(HTTPException) as error:
                await request_revision(
                    self.task_id,
                    self.slot_id,
                    ReviewReasonRequest(reason="Ещё одна доработка"),
                    self.request,
                )
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("Повторная доработка запрещена", error.exception.detail)


if __name__ == "__main__":
    unittest.main()
