# Splitwise Voice + Photo Bot

## What this is

A Telegram bot that lets users add expenses to Splitwise by sending a receipt photo and an optional voice note (or text) to a group chat. The bot parses the receipt with vision AI, transcribes the voice note, figures out who to split with from the group members, and creates the Splitwise expense after the user confirms.

The goal is friction-free shared expense capture. No opening the Splitwise app, no typing in amounts and names. Snap a photo, say "split this with Priya and Cody," tap confirm.

## Core design decisions

### Telegram, not WhatsApp or PWA

We chose Telegram because:
- WhatsApp Business API blocks bots from joining group chats. Group visibility was a hard requirement.
- Telegram is permanently free with no per-message costs.
- Bot setup is trivial (BotFather, get token, point webhook).
- Native voice + photo support, no UI to design.
- Webhooks for real-time message handling.

We rejected:
- **WhatsApp via Twilio**: $1-2/month phone number plus per-conversation cost after Meta's 1000 free conversations/month, and crucially, no group chat support.
- **PWA**: too much install friction on iOS, and Web Speech API for voice is unreliable on Safari.
- **Plain website**: worst convenience, fails the "lives where the conversation already is" test.

### Group chat first, not DM

Group visibility ("who added what") is the core UX differentiator vs the Splitwise app. Members see expenses being added in real time, and the group chat history becomes a natural audit log. DMs are still supported for solo capture but the primary mode is group.

### Each user OAuths individually with Splitwise

Mapping: `telegram_user_id` to `splitwise_user_id + token`. Users DM the bot once to OAuth, then they can add expenses in any group. The bot creates expenses from the sender's Splitwise account, splitting with other connected group members.

### Confirm before posting to Splitwise

The bot proposes the parsed expense with an inline keyboard (Confirm / Edit / Cancel). Nothing hits Splitwise until the user confirms. This prevents AI parsing errors from creating wrong expenses, which are annoying to unwind.

## Tech stack

All free tier within expected usage. Total expected cost: $0.

| Layer | Choice | Why |
|---|---|---|
| Interface | Telegram bot | Group support, free, easy setup |
| Backend | FastAPI on Oracle Cloud Always Free | Free permanent VM (4 ARM cores, 24GB RAM) |
| Database | Supabase Postgres (free tier) | 500MB enough for token storage and expense logs |
| File storage | Supabase Storage | Receipt images, same dashboard as DB |
| Transcription | Groq Whisper-large-v3-turbo | Free, fastest available |
| Vision + reasoning | Gemini 2.0 Flash | Free, good at structured extraction |
| Bill splitting | Splitwise API (public, OAuth) | Free for personal use |

### Why not Claude for the LLM

Gemini Flash is free and good enough for receipt parsing. Once the project is validated and we're willing to spend a few dollars per month, swap to Claude Sonnet for better quality on ambiguous voice notes. Build the LLM call behind a thin interface so swapping providers is a 20-line change.

## Splitwise API notes

- The public API does NOT enforce the 3-5 daily expense cap that the app shows free users. Confirmed.
- "Conservative rate limits" exist but are not a concern at personal scale.
- Auth options: OAuth 1.0a, OAuth 2.0, or per-user API key.
- We use OAuth 2.0 with per-user tokens stored encrypted.
- Python SDK on PyPI: `splitwise` (community-maintained, solid).
- Register the OAuth app at https://secure.splitwise.com/oauth_clients.
- Key endpoint: `POST /create_expense` with cost, description, group_id (or no group for friend splits), users array with split shares.

## Telegram bot quirks to know

- **Privacy mode is on by default**. Bot only sees messages that mention it or reply to its messages. Keep this on. Do NOT disable in BotFather. This is what we want.
- **Bot does not need admin privileges**. Don't ask users to make it admin, they'll be suspicious.
- **No full member list access**. Build the member roster incrementally by tracking who has DM'd the bot and who has interacted in each group.
- **Bot can't see messages from before it was added**. Don't try to backfill.
- **Use python-telegram-bot library** (the most mature Python SDK).

## Data model

```sql
users (
  telegram_user_id BIGINT PRIMARY KEY,
  telegram_username TEXT,
  splitwise_user_id INTEGER,
  splitwise_access_token TEXT,  -- encrypted at rest
  default_currency TEXT DEFAULT 'USD',
  created_at TIMESTAMPTZ DEFAULT NOW()
)

groups (
  telegram_group_id BIGINT PRIMARY KEY,
  telegram_group_name TEXT,
  splitwise_group_id INTEGER,  -- optional: link to a Splitwise group if it exists
  created_at TIMESTAMPTZ DEFAULT NOW()
)

group_memberships (
  telegram_user_id BIGINT,
  telegram_group_id BIGINT,
  last_seen_at TIMESTAMPTZ,
  PRIMARY KEY (telegram_user_id, telegram_group_id)
)

expenses_pending (
  id UUID PRIMARY KEY,
  telegram_message_id BIGINT,
  telegram_group_id BIGINT,
  payer_telegram_user_id BIGINT,
  parsed_data JSONB,
  receipt_image_url TEXT,
  expires_at TIMESTAMPTZ
)

expenses_completed (
  id UUID PRIMARY KEY,
  splitwise_expense_id INTEGER,
  telegram_message_id BIGINT,
  telegram_group_id BIGINT,
  payer_telegram_user_id BIGINT,
  amount_cents INTEGER,
  currency TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
)
```

## Architecture flow

```
User (in Telegram group)
  | sends photo + voice + "@SplitwiseBot"
  v
Telegram servers
  | webhook
  v
FastAPI backend (Oracle VM)
  | download files from Telegram file API
  |-- voice -> Groq Whisper -> transcript
  |-- photo + transcript + group context -> Gemini Flash
  |       returns: {amount, merchant, currency, split_with, split_ratio}
  v
Post confirmation to group with inline keyboard

User taps Confirm
  | callback to webhook
  v
FastAPI backend
  | lookup payer's Splitwise token
  | POST /create_expense to Splitwise API
  v
Edit bot message: "Added to Splitwise"
```

## User flows

### First-time setup (per user)

1. User joins (or is added to) a group that already has the bot.
2. Bot posts welcome message: "DM me to connect your Splitwise account."
3. User opens DM with bot, sends `/start`.
4. Bot replies with an OAuth URL.
5. User taps URL, logs into Splitwise, approves access.
6. Splitwise redirects to `https://<backend>/oauth/callback?code=...`.
7. Backend exchanges code for access token, stores it mapped to telegram_user_id.
8. Bot DMs: "Connected. Go back to your group and add a bill."

### Adding an expense

1. User in group sends a photo with caption mentioning the bot, optionally a voice note.
2. Bot processes (Whisper + Gemini), posts confirmation with inline buttons.
3. User taps Confirm.
4. Bot creates expense in Splitwise from user's account.
5. Bot edits its message to show success.

If the user taps Edit, the bot prompts for which field to change (amount, split, etc.) and re-confirms. If Cancel, the pending expense is discarded.

## Implementation plan

Build in this order. Each phase is independently testable.

### Phase 1: Bot scaffolding

- Register bot with BotFather, save token in `.env`.
- Set up FastAPI backend.
- Deploy to Oracle Cloud VM, get a stable HTTPS URL (Caddy or nginx with Let's Encrypt).
- Configure Telegram webhook to point at the FastAPI endpoint.
- Echo bot: bot replies "Got it: [message type]" for every message it sees.
- Verify webhook is reachable and bot responds in a test group.

### Phase 2: Splitwise OAuth

- Register OAuth client at https://secure.splitwise.com/oauth_clients.
- Build OAuth URL generator (include state parameter for CSRF).
- Build `/oauth/callback` endpoint that exchanges code for token.
- Implement encrypted token storage in Supabase (use `cryptography.fernet` with key in env var).
- Build `/start` DM command that returns OAuth link.
- Test: connect own Splitwise account, store token, retrieve it, fetch own friends list via the SDK.

### Phase 3: AI parsing pipeline

- Integrate Groq Whisper API for voice transcription.
- Integrate Gemini 2.0 Flash for vision + reasoning.
- Define structured output schema (JSON), validated with Pydantic:
  ```json
  {
    "amount": 47.32,
    "currency": "USD",
    "merchant": "Trader Joe's",
    "split_type": "equal",
    "splits": [{"name": "Priya", "share": 0.5}, {"name": "self", "share": 0.5}],
    "confidence": 0.85
  }
  ```
- Build LLM provider interface so Gemini can be swapped for Claude later.
- Test end-to-end with sample receipts and voice notes.

### Phase 4: Confirmation flow

- Inline keyboard with Confirm / Edit / Cancel buttons.
- Store pending expense in `expenses_pending` table on initial parse.
- Handle Telegram callback queries from button taps.
- On Confirm: call Splitwise API with payer's OAuth token, move record to `expenses_completed`.
- On Cancel: delete pending record.
- TTL on pending records (10 minutes) so abandoned confirmations don't clog the DB.

### Phase 5: Group context

- Track group memberships as users interact (insert into `group_memberships` on every message seen).
- Default split is "all connected group members" if not specified in voice/text.
- Resolve mentioned names ("split with Priya") against group roster.
- Handle ambiguity (multiple Priyas, name not in group) with a follow-up question or by listing options.

### Phase 6: Polish

- Currency handling: default per user, override via receipt detection or explicit mention.
- Error states: Splitwise API down, OAuth expired, Gemini timeout, malformed receipt.
- Commands: `/help`, `/status`, `/disconnect`, `/groups`.
- Better edit flow (let users edit specific fields without redoing the whole thing).

### Phase 7 (optional): Observability

- Log every expense parse + confirm to a separate table for evaluation.
- Track Gemini parse accuracy vs user edits (does Gemini get the amount right? merchant? split?).
- Simple dashboard later to monitor and improve prompts.

## Open questions to resolve during build

1. **Non-USD currencies**: Splitwise supports many. Set per-user default at OAuth time? Detect from receipt? Both, with receipt detection winning.
2. **Unequal splits at item level**: v1 supports equal and percentage. Item-level ("Sarah had salad, Mike had steak") is v2.
3. **Bot in many groups**: Telegram caps at 30 messages/second to different chats. Not a real concern at our scale but note it.
4. **Notifications**: Should the bot DM each affected user after expense creation, or only post in the group? Default to group-only, add DM as a per-user setting if needed.
5. **Receipt storage**: keep images? For how long? Privacy considerations. Decision: keep for 90 days for re-parsing/editing, then delete. Make this configurable.

## Things we are NOT building in v1

- Item-level receipt splitting.
- Recurring expenses.
- Settlement / "settle up" via the bot. Use the Splitwise app for that.
- Multi-language support beyond English voice transcription.
- Web dashboard for browsing expenses.
- Splitwise Pro features (we hit the API, so we don't need Pro).

## Repo layout (suggested)

```
splitwise-bot/
  app/
    main.py                # FastAPI entry, webhook routes
    config.py              # env loading, settings
    db/
      models.py            # SQLAlchemy or Supabase client wrappers
      migrations/
    telegram/
      handlers.py          # message and callback handlers
      keyboards.py         # inline keyboard builders
    splitwise/
      oauth.py             # OAuth flow
      client.py            # API wrapper, uses python `splitwise` SDK
    ai/
      transcribe.py        # Groq Whisper integration
      parse.py             # Gemini Flash integration
      provider.py          # abstraction so we can swap LLMs
      prompts.py           # system prompts for receipt parsing
    services/
      expense_pipeline.py  # orchestrates: transcribe -> parse -> confirm -> create
      group_context.py     # build context (members, currency, history) for parser
  tests/
  .env.example
  pyproject.toml
  README.md
  CLAUDE.md                # this file
```

## Reference links

- Splitwise API docs: https://dev.splitwise.com/
- Splitwise OAuth clients: https://secure.splitwise.com/oauth_clients
- Telegram bot API: https://core.telegram.org/bots/api
- python-telegram-bot: https://github.com/python-telegram-bot/python-telegram-bot
- Groq API (Whisper): https://console.groq.com/docs/speech-text
- Gemini API: https://ai.google.dev/gemini-api/docs
- Oracle Always Free: https://www.oracle.com/cloud/free/
- Supabase: https://supabase.com/

## First commit checklist

- [ ] Create GitHub repo
- [ ] Add this file as `CLAUDE.md` and a brief `README.md`
- [ ] Initialize `pyproject.toml` with FastAPI, python-telegram-bot, splitwise, supabase-py, google-generativeai, groq, cryptography, pydantic
- [ ] Set up `.env.example` with all required secrets (Telegram token, Splitwise consumer key/secret, Groq API key, Gemini API key, Supabase URL/key, encryption key)
- [ ] Stub out the directory structure
- [ ] Pick a name for the bot (something better than "SplitwiseBot")
