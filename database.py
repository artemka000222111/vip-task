
async def init_db(pool):
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            is_blocked BOOLEAN NOT NULL DEFAULT FALSE
        );

        CREATE TABLE IF NOT EXISTS wallets (
            telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
            available_kopecks BIGINT NOT NULL DEFAULT 0
                CHECK (available_kopecks >= 0),
            reserved_kopecks BIGINT NOT NULL DEFAULT 0
                CHECK (reserved_kopecks >= 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS ledger (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
            amount_kopecks BIGINT NOT NULL,
            operation TEXT NOT NULL,
            idempotency_key TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id BIGSERIAL PRIMARY KEY,
            customer_id BIGINT NOT NULL REFERENCES users(telegram_id),
            platform TEXT NOT NULL,
            action TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            target_url TEXT NOT NULL,
            unit_price_kopecks BIGINT NOT NULL CHECK (unit_price_kopecks > 0),
            total_slots INTEGER NOT NULL CHECK (total_slots > 0),
            reserved_kopecks BIGINT NOT NULL CHECK (reserved_kopecks >= 0),
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            removed_at TIMESTAMPTZ,
            removal_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS task_slots (
            id BIGSERIAL PRIMARY KEY,
            task_id BIGINT NOT NULL REFERENCES tasks(id),
            executor_id BIGINT REFERENCES users(telegram_id),
            status TEXT NOT NULL DEFAULT 'free',
            taken_at TIMESTAMPTZ,
            deadline_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS submissions (
            id BIGSERIAL PRIMARY KEY,
            slot_id BIGINT NOT NULL REFERENCES task_slots(id),
            executor_id BIGINT NOT NULL REFERENCES users(telegram_id),
            proof_url TEXT NOT NULL,
            screenshot_file_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            revision_count INTEGER NOT NULL DEFAULT 0,
            revision_deadline TIMESTAMPTZ,
            admin_reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reviewed_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS task_bans (
            task_id BIGINT NOT NULL REFERENCES tasks(id),
            executor_id BIGINT NOT NULL REFERENCES users(telegram_id),
            reason TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (task_id, executor_id)
        );

        CREATE TABLE IF NOT EXISTS topup_requests (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
            amount_kopecks BIGINT NOT NULL CHECK (amount_kopecks >= 1000),
            sender_name TEXT NOT NULL,
            proof_file_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reviewed_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS withdrawal_requests (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
            amount_kopecks BIGINT NOT NULL CHECK (amount_kopecks >= 1500),
            payout_details TEXT NOT NULL,
            fee_kopecks BIGINT NOT NULL,
            payout_kopecks BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reviewed_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS sbp_settings (
            id BIGSERIAL PRIMARY KEY,
            phone TEXT NOT NULL,
            bank TEXT NOT NULL,
            changed_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS notification_settings (
            telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
            task_notifications BOOLEAN NOT NULL DEFAULT TRUE,
            wallet_notifications BOOLEAN NOT NULL DEFAULT TRUE,
            review_notifications BOOLEAN NOT NULL DEFAULT TRUE
        );

        CREATE TABLE IF NOT EXISTS admin_actions (
            id BIGSERIAL PRIMARY KEY,
            admin_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id BIGINT,
            reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status
            ON tasks(status);

        CREATE INDEX IF NOT EXISTS idx_slots_task_status
            ON task_slots(task_id, status);

        CREATE INDEX IF NOT EXISTS idx_submissions_status
            ON submissions(status);

        CREATE INDEX IF NOT EXISTS idx_topups_status
            ON topup_requests(status);

        CREATE INDEX IF NOT EXISTS idx_withdrawals_status
            ON withdrawal_requests(status);
        """)
