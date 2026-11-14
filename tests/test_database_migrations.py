import os
import unittest

import asyncpg

from database import init_db


@unittest.skipUnless(os.getenv("DATABASE_URL"), "DATABASE_URL is required")
class DatabaseMigrationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
        await init_db(self.pool)

    async def asyncTearDown(self):
        await self.pool.close()

    async def test_init_is_idempotent_and_backfills_legacy_rows(self):
        customer_id = 900000001
        executor_id = 900000002

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (telegram_id, first_name)
                VALUES ($1, 'Migration customer'), ($2, 'Migration executor')
                ON CONFLICT (telegram_id) DO NOTHING
                """,
                customer_id,
                executor_id,
            )

            task_id = await conn.fetchval(
                """
                INSERT INTO tasks (
                    customer_id, platform, action, title, description,
                    target_url, unit_price_kopecks, total_slots,
                    reserved_kopecks, initial_total_kopecks, status
                )
                VALUES ($1, 'test', 'survey', 'Migration test',
                        'Legacy row used by migration test',
                        'https://example.com', 2500, 2, 5000, 0, 'active')
                RETURNING id
                """,
                customer_id,
            )

            slot_id = await conn.fetchval(
                """
                INSERT INTO task_slots (
                    task_id, slot_number, executor_id, status, deadline_at
                )
                VALUES ($1, 1, $2, 'taken', NOW() + INTERVAL '1 hour')
                RETURNING id
                """,
                task_id,
                executor_id,
            )

            approved_slot_id = await conn.fetchval(
                """
                INSERT INTO task_slots (
                    task_id, slot_number, executor_id, status, completed_at
                )
                VALUES ($1, 2, $2, 'approved', NOW())
                RETURNING id
                """,
                task_id,
                executor_id,
            )

            await conn.execute(
                """
                INSERT INTO task_slot_assignments (
                    task_id, slot_id, executor_id, assigned_at,
                    ended_at, outcome, reason
                )
                VALUES (
                    $1, $2, $3, NOW() - INTERVAL '2 hours',
                    NOW() - INTERVAL '1 hour', 'removed', 'Старое назначение'
                )
                """,
                task_id,
                approved_slot_id,
                executor_id,
            )

        # Повторный запуск должен быть безопасным и заполнить новые данные
        # для записей, созданных до появления соответствующих миграций.
        await init_db(self.pool)

        async with self.pool.acquire() as conn:
            initial_total = await conn.fetchval(
                "SELECT initial_total_kopecks FROM tasks WHERE id = $1",
                task_id,
            )
            assignment_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM task_slot_assignments
                WHERE slot_id = $1
                  AND executor_id = $2
                  AND ended_at IS NULL
                """,
                slot_id,
                executor_id,
            )
            assignment_deadline = await conn.fetchval(
                "SELECT deadline_at FROM task_slot_assignments WHERE slot_id = $1 AND executor_id = $2 AND ended_at IS NULL",
                slot_id,
                executor_id,
            )
            approved_history = await conn.fetchrow(
                """
                SELECT outcome, ended_at
                FROM task_slot_assignments
                WHERE slot_id = $1 AND executor_id = $2 AND outcome = 'approved'
                """,
                approved_slot_id,
                executor_id,
            )
            migration_count = await conn.fetchval(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
            )

        self.assertEqual(initial_total, 5000)
        self.assertEqual(assignment_count, 1)
        self.assertIsNotNone(assignment_deadline)
        self.assertEqual(approved_history['outcome'], 'approved')
        self.assertIsNotNone(approved_history['ended_at'])
        self.assertEqual(migration_count, 1)


if __name__ == "__main__":
    unittest.main()
