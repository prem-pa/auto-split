-- 0001_init.sql -- initial schema for auto-split.
--
-- Defines the 5 domain tables documented in CLAUDE.md plus a
-- ``schema_migrations`` tracking table used by ``scripts/migrate.py``
-- to make migrations idempotent.
--
-- Conventions:
--  * Telegram IDs are BIGINT (can exceed 32-bit).
--  * Splitwise IDs are INTEGER (Splitwise API ids fit comfortably).
--  * Timestamps are TIMESTAMPTZ, server-default to NOW().
--  * ``users.splitwise_access_token`` holds ciphertext only — Brick C
--    encrypts before persisting. This brick stores opaque strings.

CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    telegram_user_id BIGINT PRIMARY KEY,
    telegram_username TEXT,
    splitwise_user_id INTEGER,
    splitwise_access_token TEXT,
    default_currency TEXT NOT NULL DEFAULT 'USD',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS groups (
    telegram_group_id BIGINT PRIMARY KEY,
    telegram_group_name TEXT,
    splitwise_group_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS group_memberships (
    telegram_user_id BIGINT NOT NULL,
    telegram_group_id BIGINT NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (telegram_user_id, telegram_group_id)
);

CREATE INDEX IF NOT EXISTS group_memberships_group_idx
    ON group_memberships (telegram_group_id);

CREATE TABLE IF NOT EXISTS expenses_pending (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_message_id BIGINT,
    telegram_group_id BIGINT,
    payer_telegram_user_id BIGINT,
    parsed_data JSONB,
    receipt_image_url TEXT,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '10 minutes')
);

CREATE INDEX IF NOT EXISTS expenses_pending_expires_at_idx
    ON expenses_pending (expires_at);

CREATE TABLE IF NOT EXISTS expenses_completed (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    splitwise_expense_id INTEGER,
    telegram_message_id BIGINT,
    telegram_group_id BIGINT,
    payer_telegram_user_id BIGINT,
    amount_cents INTEGER,
    currency TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS expenses_completed_group_idx
    ON expenses_completed (telegram_group_id);
