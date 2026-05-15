-- Add first_name + last_name to the users table.
--
-- Telegram updates always include first_name (required) and optionally
-- last_name on every from_user. We were ignoring them; the orchestrator's
-- name resolver had only telegram_username to work with, which is null
-- for OAuth-only users and fails when the username doesn't resemble the
-- spoken name (e.g. "Shreya" → "shreyab03").
--
-- Both columns are nullable so existing rows backfill to NULL; the
-- orchestrator's upsert_user_identity call fills them in on the next
-- message from each user.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS first_name TEXT,
    ADD COLUMN IF NOT EXISTS last_name TEXT;
