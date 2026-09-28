"""Public tape: fan-out emitter for the per-guild market-drama feed.

One `feed_items` row per bound channel per event, written inside the
emitting event's own transaction -- a rollback can't leave a tape entry
for an event that never happened, and a new binding mid-flight only
picks up subsequent events (the INSERT...SELECT snapshots `feed_channels`
at emit time).

Delivery is the bot's job (`bot/feed.py`); this module stays free of
Discord imports so every service layer can call it.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg import AsyncConnection


async def _feed_enabled(conn: AsyncConnection) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM config WHERE key = 'feed.enabled'")
        row = await cur.fetchone()
    return row is None or float(row[0]) != 0.0


async def emit_feed(
    conn: AsyncConnection,
    kind: str,
    payload: dict[str, Any],
    *,
    user_id: int | None = None,
    tick_index: int | None = None,
) -> int:
    """Broadcast one event to every bound feed channel.

    Returns the number of `feed_items` rows written (0 when no channels
    are bound or the kill switch is off). `user_id` is the subject for
    per-user grouping/mentions on the tape; `tick_index` is the
    coalescing key the poller groups by. Payload must be JSON-serializable.
    """
    if not await _feed_enabled(conn):
        return 0
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO feed_items (channel_id, kind, user_id, tick_index, payload)
            SELECT c.channel_id, %s, %s, %s, %s FROM feed_channels c
            """,
            (kind, user_id, tick_index, json.dumps(payload)),
        )
        return cur.rowcount or 0
