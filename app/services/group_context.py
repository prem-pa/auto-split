"""Group context construction and split-name resolution (Brick F).

This module bridges Brick E (persistence) and Brick D (AI parser):

* :func:`build_group_context` walks the persisted group roster to produce
  the small :class:`app.ai.GroupContext` bundle the parser needs.
* :func:`resolve_split_names` reconciles the parser's "by name" splits
  against the actual ``telegram_user_id``/``splitwise_user_id`` pairs we
  have in our DB.

We never invent names: a member must already exist in ``group_memberships``
to be eligible for a split. Brick F populates that table on every inbound
message it processes, so the roster fills in incrementally — Telegram
gives no other way to enumerate group members.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.ai import GroupContext
from app.ai import Split as ParsedSplit
from app.db.groups import list_group_members
from app.db.models import User
from app.db.users import get_user

log = logging.getLogger(__name__)

# Sentinel inserted into ``ResolvedSplit.telegram_user_id`` when a name from
# the parser cannot be confidently matched to a single group member. The
# orchestrator uses this to flag the expense to the user before sending it
# off to Splitwise.
UNRESOLVED: int = 0

# Reserved name the parser emits for the payer.
SELF_TOKEN = "self"


@dataclass(frozen=True, slots=True)
class ResolvedSplit:
    """A :class:`app.ai.Split` after name resolution.

    Attributes:
        name: The original name as emitted by the parser ("self", "Priya"…).
        share: Fractional share in [0, 1].
        telegram_user_id: The matched Telegram user id, or :data:`UNRESOLVED`
            (``0``) if no unambiguous match was found.
        splitwise_user_id: The matched Splitwise user id, or ``None`` if the
            user has not finished OAuth (or could not be resolved at all).
        ambiguous: ``True`` if the name matched more than one connected
            member; in that case ``telegram_user_id`` is :data:`UNRESOLVED`
            and the orchestrator should ask the user to clarify.
    """

    name: str
    share: float
    telegram_user_id: int
    splitwise_user_id: int | None
    ambiguous: bool = False


def _display_name(user: User) -> str:
    """Best-effort human-readable name for ``user``.

    Order of preference: Telegram username, telegram_user_id as a string
    fallback. We do not have first/last name in our schema (Telegram doesn't
    push them through the privacy-mode filter reliably for groups), so this
    is deliberately spartan.
    """
    return user.telegram_username or f"user_{user.telegram_user_id}"


async def build_group_context(
    telegram_group_id: int | None,
    payer_telegram_user_id: int,
) -> GroupContext:
    """Assemble the :class:`GroupContext` the parser expects.

    Args:
        telegram_group_id: ``None`` for DMs (solo capture); the Telegram
            chat id otherwise.
        payer_telegram_user_id: The Telegram user who sent the photo.

    Behaviour:
        * In a DM (no group_id) we return a context with no other members
          and the payer's preferred currency.
        * In a group we list every recorded membership and emit the
          display name of each *other* connected user. The payer is
          omitted from ``member_names`` — the parser always refers to
          them as ``"self"``.
    """
    payer = await get_user(payer_telegram_user_id)
    payer_name = _display_name(payer) if payer is not None else SELF_TOKEN
    default_currency = payer.default_currency if payer is not None else "USD"

    member_names: list[str] = []
    if telegram_group_id is not None:
        members = await list_group_members(telegram_group_id)
        seen: set[str] = set()
        for m in members:
            if m.telegram_user_id == payer_telegram_user_id:
                continue
            name = _display_name(m)
            # Dedupe by display name so the parser doesn't see "Priya, Priya".
            if name in seen:
                continue
            seen.add(name)
            member_names.append(name)

    return GroupContext(
        payer_name=payer_name,
        member_names=member_names,
        default_currency=default_currency,
    )


_NORMALISE_RE = re.compile(r"[^a-z0-9]+")


def _normalise(name: str) -> str:
    """Lowercase + strip non-alphanumerics for forgiving name matching."""
    return _NORMALISE_RE.sub("", name.lower())


def resolve_split_names(
    splits: list[ParsedSplit],
    group_members: list[User],
    *,
    payer_telegram_user_id: int,
    payer_splitwise_user_id: int | None,
) -> list[ResolvedSplit]:
    """Resolve each ``Split.name`` to a concrete user.

    Matching rules (case- and punctuation-insensitive):
        1. ``"self"`` → the payer.
        2. Exact match against ``telegram_username``.
        3. Prefix match (``"priya"`` matches ``"priya_p"``) IF unique.
        4. Anything else → :data:`UNRESOLVED`, ``ambiguous=False``.

    Multiple matches at step 2 or 3 produce ``ambiguous=True``.

    The payer is always resolvable; everyone else must already be a member
    and connected (have a ``splitwise_user_id``) for the orchestrator to
    actually post the expense to Splitwise. ``splitwise_user_id`` may still
    be ``None`` here — the orchestrator decides what to do about that.
    """
    # Pre-index members by their normalised display name.
    by_norm_name: dict[str, list[User]] = {}
    for m in group_members:
        if m.telegram_user_id == payer_telegram_user_id:
            continue
        key = _normalise(_display_name(m))
        by_norm_name.setdefault(key, []).append(m)

    resolved: list[ResolvedSplit] = []
    for split in splits:
        norm = _normalise(split.name)
        if norm == "self":
            resolved.append(
                ResolvedSplit(
                    name=split.name,
                    share=split.share,
                    telegram_user_id=payer_telegram_user_id,
                    splitwise_user_id=payer_splitwise_user_id,
                )
            )
            continue

        exact = by_norm_name.get(norm, [])
        if len(exact) == 1:
            user = exact[0]
            resolved.append(
                ResolvedSplit(
                    name=split.name,
                    share=split.share,
                    telegram_user_id=user.telegram_user_id,
                    splitwise_user_id=user.splitwise_user_id,
                )
            )
            continue
        if len(exact) > 1:
            log.info(
                "resolve_split_names: ambiguous name=%r matches=%d",
                split.name,
                len(exact),
            )
            resolved.append(
                ResolvedSplit(
                    name=split.name,
                    share=split.share,
                    telegram_user_id=UNRESOLVED,
                    splitwise_user_id=None,
                    ambiguous=True,
                )
            )
            continue

        # Prefix search across all members.
        prefix_hits = [
            users
            for key, users in by_norm_name.items()
            if norm and key.startswith(norm)
        ]
        # Flatten — multiple keys may each have multiple users.
        flat = [u for group in prefix_hits for u in group]
        if len(flat) == 1:
            user = flat[0]
            resolved.append(
                ResolvedSplit(
                    name=split.name,
                    share=split.share,
                    telegram_user_id=user.telegram_user_id,
                    splitwise_user_id=user.splitwise_user_id,
                )
            )
            continue
        if len(flat) > 1:
            log.info(
                "resolve_split_names: ambiguous prefix name=%r matches=%d",
                split.name,
                len(flat),
            )
            resolved.append(
                ResolvedSplit(
                    name=split.name,
                    share=split.share,
                    telegram_user_id=UNRESOLVED,
                    splitwise_user_id=None,
                    ambiguous=True,
                )
            )
            continue

        # No match found at all.
        log.info("resolve_split_names: no match name=%r", split.name)
        resolved.append(
            ResolvedSplit(
                name=split.name,
                share=split.share,
                telegram_user_id=UNRESOLVED,
                splitwise_user_id=None,
            )
        )
    return resolved


__all__ = [
    "ResolvedSplit",
    "SELF_TOKEN",
    "UNRESOLVED",
    "build_group_context",
    "resolve_split_names",
]
