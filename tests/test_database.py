import os
import unittest
import uuid

import asyncpg

from database import init_db


class DatabaseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            self.skipTest("DATABASE_URL is not configured")

        self.schema = "vip_task_test_" + uuid.uuid4().hex[:12]
        self.admin_pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
        async with self.admin_pool.acquire() as conn:
            await conn.execute(f'CREATE SCHEMA "{self.schema}"')

        self.pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=2,
            server_settings={"search_path": self.schema},
        )

    async def asyncTearDown(self):
        if getattr(self, "pool", None):
            await self.pool.close()
        if getattr(self, "admin_pool", None):
            async with self.admin_pool.acquire() as conn:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
            await self.admin_pool.close()

    async def test_schema_initializes_and_is_idempotent(self):
        await init_db(self.pool)
        await init_db(self.pool)

        async with self.pool.acquire() as conn:
            tables = await conn.fetch(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                """
            )
            table_names = {row["table_name"] for row in tables}
            expected = {
                "users", "wallets", "ledger", "platform_revenue",
                "tasks", "task_slots", "task_slot_assignments",
                "submissions", "task_bans", "topup_requests",
                "withdrawal_requests", "sbp_settings",
                "notification_settings", "admin_actions",
                "schema_migrations",
            }
            self.assertTrue(expected.issubset(table_names))

            columns = await conn.fetch(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                """
            )
            column_map = {(row["table_name"], row["column_name"]) for row in columns}
            for required in (
                ("tasks", "initial_total_kopecks"),
                ("tasks", "penalty_kopecks"),
                ("task_slots", "slot_number"),
                ("submissions", "reviewed_by"),
                ("notification_settings", "system_notifications"),
                ("withdrawal_requests", "payout_details"),
            ):
                self.assertIn(required, column_map)

            migration_versions = await conn.fetchval(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
            )
            self.assertEqual(migration_versions, 1)

            unique_slot_index = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND indexname = 'idx_task_slots_number'
                      AND indexdef ILIKE '%UNIQUE%'
                )
                """
            )
            self.assertTrue(unique_slot_index)


if __name__ == "__main__":
    unittest.main()
