# auto-split

Telegram bot that turns a receipt photo + voice note into a Splitwise expense.
Snap, say "split with Priya," confirm. See [CLAUDE.md](./CLAUDE.md) for the full design.

## Stack

FastAPI · python-telegram-bot · Splitwise SDK · Supabase (Postgres + Storage)
Groq Whisper (transcription) · Gemini 2.0 Flash (vision + reasoning)

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync
cp .env.example .env   # then fill in tokens
uv run uvicorn app.main:app --reload
```

## Layout

```
app/
  main.py        FastAPI entry + webhook routes
  config.py      env loading
  db/            Supabase models + migrations
  telegram/      message + callback handlers, inline keyboards
  splitwise/     OAuth flow + API client
  ai/            transcribe (Groq), parse (Gemini), provider abstraction, prompts
  services/      expense pipeline + group context
tests/
```
