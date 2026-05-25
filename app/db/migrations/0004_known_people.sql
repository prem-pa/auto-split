-- Cache of each bot user's Splitwise "known people" (their Splitwise friends).
--
-- Splitwise auto-friends anyone you share an expense with, so a user's friend
-- list is effectively "everyone they've split with." Caching it lets the
-- orchestrator resolve a name like "split with Cody" against someone who has a
-- Splitwise account but never connected to the bot — they have a
-- splitwise_user_id, which is all create_expense needs.
--
-- Synced from getFriends() on OAuth connect / re-/start (best-effort).
-- Keyed per owner; (owner, splitwise_user_id) is unique so re-syncs upsert.

CREATE TABLE IF NOT EXISTS known_people (
    owner_telegram_user_id BIGINT NOT NULL,
    splitwise_user_id BIGINT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    email TEXT,
    source TEXT NOT NULL DEFAULT 'friend',
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner_telegram_user_id, splitwise_user_id)
);

CREATE INDEX IF NOT EXISTS known_people_owner_idx
    ON known_people (owner_telegram_user_id);
