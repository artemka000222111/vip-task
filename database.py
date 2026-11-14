
import asyncpg


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        telegram_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        is_blocked BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS wallets (
        telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
        available_kopecks BIGINT NOT NULL DEFAULT 0
            CHECK (available_kopecks >= 0),
        reserved_kopecks BIGINT NOT NULL DEFAULT 0
            CHECK (reserved_kopecks >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS ledger (
        id BIGSERIAL PRIMARY KEY,
        telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
        amount_kopecks BIGINT NOT NULL,
        operation TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        related_task_id BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS platform_revenue (
        id BIGSERIAL PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        source_id BIGINT,
        amount_kopecks BIGINT NOT NULL CHECK (amount_kopecks > 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,

    """
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
        reserved_kopecks BIGINT NOT NULL DEFAULT 0
            CHECK (reserved_kopecks >= 0),
        initial_total_kopecks BIGINT NOT NULL DEFAULT 0,
        penalty_kopecks BIGINT NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        removed_at TIMESTAMPTZ,
        removal_reason TEXT
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS task_slots (
        id BIGSERIAL PRIMARY KEY,
        task_id BIGINT NOT NULL REFERENCES tasks(id),
        slot_number INTEGER,
        executor_id BIGINT REFERENCES users(telegram_id),
        status TEXT NOT NULL DEFAULT 'free',
        taken_at TIMESTAMPTZ,
        deadline_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS task_slot_assignments (
        id BIGSERIAL PRIMARY KEY,
        task_id BIGINT NOT NULL REFERENCES tasks(id),
        slot_id BIGINT NOT NULL REFERENCES task_slots(id),
        executor_id BIGINT NOT NULL REFERENCES users(telegram_id),
        assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        deadline_at TIMESTAMPTZ,
        ended_at TIMESTAMPTZ,
        outcome TEXT,
        reason TEXT
    )
    """,

    """
    ALTER TABLE task_slot_assignments
    ADD COLUMN IF NOT EXISTS deadline_at TIMESTAMPTZ
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_slot_assignments_executor
    ON task_slot_assignments(executor_id, assigned_at DESC)
    """,

    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_slot_assignments_one_active_per_slot
    ON task_slot_assignments(slot_id)
    WHERE ended_at IS NULL
    """,
    """
    INSERT INTO task_slot_assignments (
        task_id, slot_id, executor_id, assigned_at,
        deadline_at, ended_at, outcome
    )
    SELECT
        s.task_id,
        s.id,
        s.executor_id,
        COALESCE(s.taken_at, s.created_at),
        s.deadline_at,
        CASE
            WHEN s.status = 'approved'
            THEN COALESCE(s.completed_at, s.created_at, NOW())
            ELSE NULL
        END,
        CASE WHEN s.status = 'approved' THEN 'approved' ELSE NULL END
    FROM task_slots s
    WHERE s.executor_id IS NOT NULL
      AND s.status IN ('taken', 'revision', 'pending_review', 'approved')
      AND NOT EXISTS (
          SELECT 1 FROM task_slot_assignments a
          WHERE a.slot_id = s.id AND a.executor_id = s.executor_id
      )
    """,

    """
    CREATE TABLE IF NOT EXISTS submissions (
        id BIGSERIAL PRIMARY KEY,
        slot_id BIGINT NOT NULL UNIQUE REFERENCES task_slots(id),
        executor_id BIGINT NOT NULL REFERENCES users(telegram_id),
        proof_url TEXT NOT NULL,
        screenshot_file_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        revision_count INTEGER NOT NULL DEFAULT 0,
        revision_deadline TIMESTAMPTZ,
        admin_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        reviewed_at TIMESTAMPTZ,
        reviewed_by BIGINT
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS task_bans (
        task_id BIGINT NOT NULL REFERENCES tasks(id),
        executor_id BIGINT NOT NULL REFERENCES users(telegram_id),
        reason TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (task_id, executor_id)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS topup_requests (
        id BIGSERIAL PRIMARY KEY,
        telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
        amount_kopecks BIGINT NOT NULL CHECK (amount_kopecks >= 1000),
        sender_name TEXT NOT NULL,
        proof_file_id TEXT,
        sbp_phone_snapshot TEXT,
        sbp_bank_snapshot TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        admin_id BIGINT,
        admin_comment TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        reviewed_at TIMESTAMPTZ
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS withdrawal_requests (
        id BIGSERIAL PRIMARY KEY,
        telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
        amount_kopecks BIGINT NOT NULL CHECK (amount_kopecks >= 1500),
        payout_details TEXT NOT NULL,
        fee_kopecks BIGINT NOT NULL,
        payout_kopecks BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        admin_id BIGINT,
        admin_comment TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        reviewed_at TIMESTAMPTZ
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS sbp_settings (
        id BIGSERIAL PRIMARY KEY,
        phone TEXT NOT NULL,
        bank TEXT NOT NULL,
        changed_by BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS notification_settings (
        telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
        task_notifications BOOLEAN NOT NULL DEFAULT TRUE,
        wallet_notifications BOOLEAN NOT NULL DEFAULT TRUE,
        review_notifications BOOLEAN NOT NULL DEFAULT TRUE,
        system_notifications BOOLEAN NOT NULL DEFAULT TRUE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS admin_actions (
        id BIGSERIAL PRIMARY KEY,
        admin_id BIGINT NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id BIGINT,
        reason TEXT,
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,

    # Дополнительные поля для обновления уже созданной базы.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS initial_total_kopecks BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS penalty_kopecks BIGINT NOT NULL DEFAULT 0",
    """
    UPDATE tasks
    SET initial_total_kopecks = unit_price_kopecks * total_slots
    WHERE initial_total_kopecks = 0
    """,
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE task_slots ADD COLUMN IF NOT EXISTS slot_number INTEGER",
    "ALTER TABLE task_slots ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
    "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS reviewed_by BIGINT",
    "ALTER TABLE topup_requests ADD COLUMN IF NOT EXISTS sbp_phone_snapshot TEXT",
    "ALTER TABLE topup_requests ADD COLUMN IF NOT EXISTS sbp_bank_snapshot TEXT",
    "ALTER TABLE topup_requests ADD COLUMN IF NOT EXISTS admin_id BIGINT",
    "ALTER TABLE topup_requests ADD COLUMN IF NOT EXISTS admin_comment TEXT",
    "ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS admin_id BIGINT",
    "ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS admin_comment TEXT",
    "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS system_notifications BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",

    # Нумеруем существующие места, если база уже запускалась.
    """
    WITH numbered AS (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY task_id ORDER BY id
               ) AS number
        FROM task_slots
        WHERE slot_number IS NULL
    )
    UPDATE task_slots AS slots
    SET slot_number = numbered.number
    FROM numbered
    WHERE slots.id = numbered.id
    """,

    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_task_slots_number
    ON task_slots(task_id, slot_number)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_tasks_status_created
    ON tasks(status, created_at DESC)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_tasks_customer
    ON tasks(customer_id, created_at DESC)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_slots_task_status
    ON task_slots(task_id, status)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_slots_executor_status
    ON task_slots(executor_id, status)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_submissions_status
    ON submissions(status, created_at)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_topups_status
    ON topup_requests(status, created_at)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_withdrawals_status
    ON withdrawal_requests(status, created_at)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_ledger_user_created
    ON ledger(telegram_id, created_at DESC)
    """,

    """
    CREATE INDEX IF NOT EXISTS idx_admin_actions_created
    ON admin_actions(created_at DESC)
    """,
]


async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            for statement in SCHEMA:
                await conn.execute(statement)

            await conn.execute(
                """
                INSERT INTO schema_migrations(version)
                VALUES (1)
                ON CONFLICT (version) DO NOTHING
                """
            )
