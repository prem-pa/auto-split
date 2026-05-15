"""Prompts for the receipt parser (Brick D).

Kept in code (not inlined f-strings) so we can A/B them later and so
tests can introspect what we send to the model. Nothing here should hit
the network or read env vars.
"""

from __future__ import annotations

from app.ai.provider import GroupContext

SYSTEM_PROMPT = """\
You are a receipt-parsing assistant for a group expense-splitting bot.

You will receive:
  1. A photo of a receipt.
  2. (Optional) a user instruction (transcript of a voice note, or text).
  3. The list of group members the expense can be split with.

Your job is to return a single JSON object that conforms to the provided
schema. Do not return prose. Do not wrap the JSON in markdown.

Rules:
  - "amount" is the receipt total (taxes and tips included).
  - "currency" is the ISO 4217 code (e.g. "USD", "EUR", "INR"). If the
    receipt is ambiguous, fall back to the default currency provided.
  - "merchant" is the business name from the receipt, or null if unclear.
  - "split_type" is the user's stated intent:
      * "equal"      — everyone pays the same fraction (the default if
                       the user does not specify how to split).
      * "percentage" — user gave explicit percentages, e.g.
                       "Priya 60%, me 40%" → shares 0.6 and 0.4.
      * "shares"     — user gave a ratio of parts, e.g. "split 2:1 with
                       Priya" or "I'll do 3 shares, you do 1". Convert
                       to normalised fractions: 2:1 → 0.667 / 0.333.
      * "exact"      — user gave specific dollar amounts (e.g. "I had
                       $30, you had $17"). Convert each amount into a
                       fraction of the receipt total.
  - "splits" is a list of {"name", "share"} pairs.
      * "name" must match either "self" (the payer) or one of the
        provided member names exactly. Do not invent names that are not
        in the member list.
      * "share" is a fraction in [0, 1]. All shares MUST sum to 1.0
        (within 0.01). Round to 4 decimal places.
  - "confidence" is your own 0-to-1 estimate of how sure you are about
    the amount + split. Penalise blurry receipts and ambiguous voice
    notes.

If the user instruction does not name anyone, split equally across the
payer and every other provided member.

If the user instruction names people who are not in the member list,
include them anyway (the orchestrator will surface the mismatch) but
lower your confidence.
"""


def build_user_prompt(transcript: str | None, context: GroupContext) -> str:
    """Build the per-request text portion of the Gemini prompt.

    Includes payer name, member names, default currency, and the
    transcript / user instruction if one was supplied.
    """
    others = ", ".join(context.member_names) if context.member_names else "(none)"
    instruction = (transcript or "").strip() or "(no voice note or text supplied)"
    return (
        f'Payer: {context.payer_name} (use the name "self" for them in splits).\n'
        f"Group members available to split with: {others}.\n"
        f"Default currency if unclear: {context.default_currency}.\n"
        f"User instruction: {instruction}\n"
    )


# Appended to a retry attempt when the first response was not valid JSON.
STRICT_RETRY_SUFFIX = (
    "\n\nYour previous response was not valid JSON. Respond with VALID JSON "
    "ONLY, matching the schema exactly. No markdown fences, no commentary."
)
