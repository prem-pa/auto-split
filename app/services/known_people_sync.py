"""Sync a bot user's Splitwise friends into the ``known_people`` cache.

Splitwise auto-friends anyone you share an expense with, so ``getFriends`` is
effectively the user's "people I've split with." We pull it on OAuth connect
and on ``/start`` for already-connected users, so the name resolver can match
splits against Splitwise people who never joined the bot.
"""

from __future__ import annotations

import logging

from app.db.known_people import KnownPerson, upsert_known_people

log = logging.getLogger(__name__)


async def sync_known_people(owner_telegram_user_id: int, plain_token: str) -> int:
    """Pull ``owner``'s Splitwise friends and cache them. Returns rows written.

    Best-effort by contract: callers should wrap this and treat any failure as
    non-fatal — the feature degrades to "only connected members are
    resolvable," which is the pre-existing behaviour.
    """
    # Imported lazily, inside the function, to avoid an import cycle:
    # app.splitwise.oauth imports this module, and importing app.splitwise.*
    # at module load runs the package __init__ which imports oauth.
    from app.splitwise.client import SplitwiseClient

    client = SplitwiseClient(plain_token)
    friends = await client.get_friends()
    people = [
        KnownPerson(
            splitwise_user_id=f.id,
            first_name=f.first_name,
            last_name=f.last_name,
            email=f.email,
        )
        for f in friends
    ]
    written = await upsert_known_people(owner_telegram_user_id, people)
    log.info(
        "known_people synced owner=%s friends=%d written=%d",
        owner_telegram_user_id,
        len(friends),
        written,
    )
    return written


__all__ = ["sync_known_people"]
