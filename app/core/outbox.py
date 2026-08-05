"""Outbox: at-least-once delivery with idempotency.

Rule 2 — every notification carries a dedupe key with a UNIQUE constraint. A crash
between sending and recording must not re-page the electrician on restart, so we
enqueue with INSERT OR IGNORE (the key does the real work) and drain with retry +
exponential backoff. Delivery is at-least-once; the key makes that safe.
"""

from __future__ import annotations

import json

from .. import clock, db
from . import events

_BASE_BACKOFF_S = 5
_MAX_BACKOFF_S = 300
_MAX_ATTEMPTS = 8


def enqueue(c, channel: str, recipient: str, payload: dict, dedupe_key: str,
            at: str | None = None) -> bool:
    """Queue one notification inside an existing transaction. Returns True if newly
    queued, False if the dedupe key already existed (idempotent no-op)."""
    at = at or clock.now_iso()
    cur = c.execute(
        "INSERT IGNORE INTO outbox"
        "(channel, recipient, payload, dedupe_key, attempts, next_try_at, status)"
        " VALUES (?,?,?,?,0,?, 'queued')",
        (channel, recipient, json.dumps(payload), dedupe_key, at),
    )
    return cur.rowcount > 0


def _backoff_seconds(attempts: int) -> int:
    return min(_BASE_BACKOFF_S * (2 ** max(0, attempts - 1)), _MAX_BACKOFF_S)


def drain(cfg, limit: int = 50) -> dict:
    """Send everything due. Called by the outbox worker (~5s) or directly in tests.

    Returns a small summary dict {sent, failed, retried}.
    """
    from ..notifiers import get_notifier  # lazy: avoids import cycle

    now = clock.now_iso()
    rows = db.query(
        "SELECT * FROM outbox WHERE status='queued' AND next_try_at<=?"
        " ORDER BY next_try_at, id LIMIT ?",
        (now, limit),
    )
    sent = failed = retried = 0
    for r in rows:
        payload = json.loads(r["payload"])
        notifier = get_notifier(cfg, r["channel"])
        try:
            provider_id = notifier.send(r["recipient"], payload)
            with db.transaction() as c:
                c.execute(
                    "UPDATE outbox SET status='sent', sent_at=?, provider_msg_id=?,"
                    " attempts=attempts+1 WHERE id=?",
                    (clock.now_iso(), provider_id, r["id"]),
                )
            sent += 1
        except Exception as exc:  # delivery failed — retry with backoff
            attempts = r["attempts"] + 1
            if attempts >= _MAX_ATTEMPTS:
                with db.transaction() as c:
                    c.execute(
                        "UPDATE outbox SET status='failed', attempts=? WHERE id=?",
                        (attempts, r["id"]),
                    )
                failed += 1
            else:
                nxt = clock.plus_seconds(_backoff_seconds(attempts))
                with db.transaction() as c:
                    c.execute(
                        "UPDATE outbox SET attempts=?, next_try_at=? WHERE id=?",
                        (attempts, nxt, r["id"]),
                    )
                retried += 1
            _ = exc
    return {"sent": sent, "failed": failed, "retried": retried}


def depth() -> int:
    r = db.query_one("SELECT COUNT(*) n FROM outbox WHERE status='queued'")
    return r["n"] if r else 0
