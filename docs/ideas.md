# Idea log

Future product ideas, jotted as they come up. Newest first.
Format per entry: short title, date, what + why, optional notes.

---

## Split with people who aren't on the bot (but have Splitwise)
**2026-05-25**

> **v1 SHIPPED** (branch `fix/api-retries-and-observability`): friends-matching.
> A `known_people` table caches each user's Splitwise friends (synced from
> `getFriends()` on OAuth connect + `/start`); the resolver falls back to it
> when a split name isn't a connected bot member, producing an `external`
> ResolvedSplit with the friend's `splitwise_user_id`. Email-invite of brand
> new strangers was deferred (see below).

Let a user split an expense with someone who hasn't connected to the
bot — as long as that person already has a Splitwise account. Today the
bot only splits among connected group members (it maps
`telegram_user_id → splitwise_user_id`), so anyone who hasn't OAuth'd is
invisible to the splitter.

**Follow-ups (not yet built):**
- **`/people` command** — list who the user can split with (connected members
  + cached Splitwise friends). Makes the feature discoverable and answers
  "who does the bot know about?"
- **Onboarding a brand-new person without an email-invite flow:** tell the
  user to add *one* expense with them in the Splitwise app. Splitwise
  auto-friends on a shared expense, so the next sync (a `/start` away) picks
  them up. This is a zero-build alternative to the deferred email-invite UX —
  the `/people` reply should explain it.

**Feasibility: yes, at the API level.** Splitwise's `create_expense`
accepts participants by `user_id` *or* by `email` + `first_name`/
`last_name`. Adding someone by email who isn't your friend yet triggers a
Splitwise invite — so the payer can split with anyone, bot or not.

**The gap is identity mapping.** From a Telegram mention ("split with
Cody") the bot has a *name*, not a Splitwise identity. For a non-bot
person it can't resolve name → `splitwise_user_id`. Options to explore:
- Pull the payer's Splitwise **friends list** (we already have their
  token) and match the mentioned name against it — covers people who are
  Splitwise friends but never used the bot. This is probably the 80% case.
- Fall back to asking for an **email** when the name isn't a connected
  member or a known friend, then split by email (Splitwise invites them).
- Cache resolved external people per group so you don't re-ask.

**Can we profile a user's split history via the API? Yes.**
Splitwise auto-friends anyone you share an expense with, so the friends
list *is* effectively "everyone you've split with." Sources, easiest first:
- `getFriends()` → each friend's `id`, `first_name`, `last_name`, `email`.
  **We already wrap this** (`SplitwiseClient.get_friends`). One call = the
  known-people set.
- `getGroups()` → co-members of the user's groups (also already wrapped).
- `getExpenses()` (paginated) → historical participants, including people
  who are no longer friends. More work; only needed for exhaustive history.

**Data model: a normal table, not a graph DB.** It's a per-user adjacency
list, which Postgres handles fine at our scale:
```sql
known_people (
  owner_telegram_user_id BIGINT,
  splitwise_user_id INTEGER,
  first_name TEXT, last_name TEXT, email TEXT,
  source TEXT,            -- 'friend' | 'group' | 'expense'
  last_synced_at TIMESTAMPTZ,
  PRIMARY KEY (owner_telegram_user_id, splitwise_user_id)
)
```
Sync on OAuth connect + lazily refresh (e.g. on a cache miss during name
resolution, or a periodic job). Then the name resolver matches "split with
Cody" against connected members first, then this known-people set.

**Scope note:** this is its own feature (schema + resolver + expense-build
by `splitwise_user_id`/email + sync + ambiguity UX) — kept separate from
the resilience/observability release so that can ship and fix the live bug.

**Open questions**
- Ambiguity/UX when a mentioned name matches a friend but not a member.
- Privacy: surfacing the payer's Splitwise friend list names in a group.
- Whether to persist these external participants for reuse.

---

## Item-by-item bill splitting ("help me split this")
**2026-05-25**

Instead of one split for the whole receipt, let the user split a bill
**line by line**. Flow:
1. User sends a photo and asks the bot to help split it.
2. Bot first assembles a **bank of people** for this bill — asks who should
   be included (connected members + known Splitwise people, see the idea
   above).
3. Bot goes **item by item** (using the receipt line items the parser
   already extracts) and asks who shares each one:
   - `"all"` → split across everyone in the bank.
   - a list of names → just those people.
   - per-item the user can choose **equal / shares / percentages**.
4. Bot tallies each person's total (items + proportional tax/tip) and
   creates one Splitwise expense with the computed per-person owed shares.

**Why:** this is the "Sarah had the salad, Mike had the steak" case — the
v2 item-level split called out in CLAUDE.md as explicitly *not* in v1.

**Notes / open questions**
- Leans on line-item extraction (`ParsedExpense.items`) already in the
  parser, plus the known-people bank from the idea above — build that first.
- Multi-turn, stateful conversation: needs to hold partial split state
  across several messages (the conversation-session work pairs well here).
- How to apportion tax/tip/fees across items — proportional to item cost is
  the sane default.
- UX for correcting a mis-assigned item without restarting the whole flow.

---

## Deploy on Raspberry Pi Zero 2 W
**2026-05-16**

Move the bot off the dev laptop onto an existing Pi Zero 2 W
(quad-core A53, **512MB RAM**, WiFi only, microSD).

**Open questions**
- Real memory footprint under load. Rough estimate was 350-450MB
  resident but that's a guess based on the dep list, not measurement.
  **Decision-blocker:** measure actual RSS of `uvicorn app.main:app`
  (no `--reload`, single worker) at idle and during a real receipt
  capture before committing to the Pi. Use the one-liner from chat:
    ```bash
    PID=$(pgrep -fn "uvicorn app.main")
    while sleep 1; do ps -o rss= -p "$PID" | awk '{printf "%6.1f MB\n", $1/1024}'; done
    ```
- macOS RSS vs ARM Linux RSS isn't 1:1. Mac measurement is a floor;
  the Pi could be 20-30% higher.
- Python 3.12 (Pi OS default) vs 3.14 (dev laptop) differ by ~5-10MB
  at baseline.

**Sketch (if feasible)**
- Pi OS Lite 64-bit, Python 3.12, `uv` for env management.
- `uvicorn app.main:app --workers 1` as a systemd service.
- Named `cloudflared` tunnel as systemd service for a stable URL.
- Systemd memory limits: `MemoryHigh=350M`, `MemoryMax=450M`,
  `Restart=on-failure`. OOM kill → auto-restart.
- 1GB swap file (not zram — Python's resident set can't be compressed
  meaningfully). High-endurance microSD (Samsung PRO Endurance or
  SanDisk Max Endurance).
- Langfuse stays on cloud — the Pi can't run a ClickHouse stack.

**Fallback paths**
- Pi 4 4GB (~$45) if Zero 2 W OOMs too often. Same software, way more headroom.
- Oracle Always Free as the cloud option (real reclaim risk).

---

## Receipt deduplication
**2026-05-16**

If a user tries to record the same receipt twice, the bot should notice
and ask if they really mean to add it again — rather than silently
creating a duplicate on Splitwise.

**Sketch**
- On photo capture, compute a hash of the image. Use a **perceptual hash**
  (e.g. pHash) so re-photographing the same bill at a slightly different
  angle / crop still matches.
- Persist the hash with the completed expense (new column on
  `expenses_completed`).
- On the next capture, look up the new hash against recent completed
  expenses. If a match exists within a TTL window (~60 days?), the
  confirmation reads:
  > *"Looks like you already added this on 2026-05-12 ($47.32, split with
  > Shreya). Add it again?"*
  with Yes / Cancel buttons.
- Yes → proceed as normal. Cancel → drop the pending row.

**Open questions**
- Match scope: per-user, per-group, or global? Per-group is most useful for
  catching "Hardik and Shreya both tried to capture last night's dinner."
- pHash threshold tuning — too lax matches different receipts at the same
  merchant; too strict misses real dupes (rotation, glare, partial reshot).
- Hash before or after parsing? Hashing first saves a Gemini call on
  obvious dupes, but you still need to display the matched expense's
  details, which means a DB lookup either way.
