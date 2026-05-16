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
from app.db.users import get_user, list_connected_users

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

    Order of preference: first_name, telegram_username, telegram_user_id
    as a string fallback. We prefer ``first_name`` because that's what
    the parser is most likely to hear in a voice note ("split with
    Shreya") — the @username is for matching only.
    """
    if user.first_name:
        return user.first_name
    return user.telegram_username or f"user_{user.telegram_user_id}"


def _name_candidates(user: User) -> list[str]:
    """Every string we'll accept as referring to ``user`` in a split.

    Order doesn't matter for matching, but we keep first_name first for
    clarity. Empty / falsy values are skipped.
    """
    candidates: list[str] = []
    if user.first_name:
        candidates.append(user.first_name)
    if user.last_name:
        candidates.append(user.last_name)
    if user.telegram_username:
        candidates.append(user.telegram_username)
    return candidates


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
        * **Group context**: list every recorded membership and emit the
          display name of each *other* member.
        * **DM context**: list every OAuth'd user system-wide as the
          candidate pool. The sender naturally knows their friends are
          on the bot and might say "split with Hardik" in a DM — we
          treat all connected users as eligible split targets.

    In both cases the payer is omitted from ``member_names`` (the parser
    always refers to them as ``"self"``) and names are deduped.
    """
    payer = await get_user(payer_telegram_user_id)
    payer_name = _display_name(payer) if payer is not None else SELF_TOKEN
    default_currency = payer.default_currency if payer is not None else "USD"

    if telegram_group_id is not None:
        members = await list_group_members(telegram_group_id)
    else:
        members = await list_connected_users()

    member_names: list[str] = []
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


def _dedupe_by_user(users: list[User]) -> list[User]:
    """Dedupe a list of User by ``telegram_user_id`` preserving first-seen order.

    Needed because a single user has multiple name candidates (first_name,
    last_name, username) — if a query matches more than one of them, naive
    flattening would double-count them.
    """
    seen: set[int] = set()
    out: list[User] = []
    for u in users:
        if u.telegram_user_id in seen:
            continue
        seen.add(u.telegram_user_id)
        out.append(u)
    return out


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
        2. Exact match against any of a member's name candidates
           (first_name, last_name, telegram_username) — preferred.
        3. Prefix match across the same candidates — fallback.
        4. Anything else → :data:`UNRESOLVED`, ``ambiguous=False``.

    Multiple distinct members matching at step 2 or 3 produce
    ``ambiguous=True``. A single member matching via multiple of their
    own candidates is still one match (not ambiguous).

    ``splitwise_user_id`` may be ``None`` on a resolved member if they
    haven't OAuth'd yet — the orchestrator decides whether to surface
    that to the user.
    """
    # Pre-index every (candidate -> [users]) so a single user with both a
    # first_name and a username appears under multiple keys.
    by_norm_name: dict[str, list[User]] = {}
    for m in group_members:
        if m.telegram_user_id == payer_telegram_user_id:
            continue
        for cand in _name_candidates(m):
            key = _normalise(cand)
            if not key:
                continue
            bucket = by_norm_name.setdefault(key, [])
            if not any(u.telegram_user_id == m.telegram_user_id for u in bucket):
                bucket.append(m)

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

        # 1. Exact match — preferred. Dedupe by user in case a user
        #    appears under multiple keys that all happen to equal ``norm``
        #    (rare but possible if first_name == username).
        exact = _dedupe_by_user(by_norm_name.get(norm, []))
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
                "resolve_split_names: ambiguous exact name=%r matches=%d",
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

        # 2. Prefix match across all candidates, deduped by user.
        prefix_users: list[User] = []
        for key, users in by_norm_name.items():
            if norm and key.startswith(norm):
                prefix_users.extend(users)
        prefix_users = _dedupe_by_user(prefix_users)
        if len(prefix_users) == 1:
            user = prefix_users[0]
            resolved.append(
                ResolvedSplit(
                    name=split.name,
                    share=split.share,
                    telegram_user_id=user.telegram_user_id,
                    splitwise_user_id=user.splitwise_user_id,
                )
            )
            continue
        if len(prefix_users) > 1:
            log.info(
                "resolve_split_names: ambiguous prefix name=%r matches=%d",
                split.name,
                len(prefix_users),
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

        # 3. No match found at all.
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
