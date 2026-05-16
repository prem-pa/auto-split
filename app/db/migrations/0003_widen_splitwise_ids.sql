-- Widen INTEGER columns that hold Splitwise IDs to BIGINT.
--
-- Splitwise's auto-incrementing IDs have already crossed Postgres int4's
-- ~2.15B limit (we hit "value '4463412812' is out of range for type
-- integer" inserting a real expense). int4 → int8 is a metadata-only
-- change in Postgres (no table rewrite), so this is fast even on large
-- tables.
--
-- Three columns affected:
--   * users.splitwise_user_id
--   * groups.splitwise_group_id
--   * expenses_completed.splitwise_expense_id

ALTER TABLE users
    ALTER COLUMN splitwise_user_id TYPE BIGINT;

ALTER TABLE groups
    ALTER COLUMN splitwise_group_id TYPE BIGINT;

ALTER TABLE expenses_completed
    ALTER COLUMN splitwise_expense_id TYPE BIGINT;
