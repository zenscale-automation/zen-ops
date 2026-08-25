"""Looms that are deliberately not in production.

The loom feed cannot tell "no order, no yarn, planned maintenance" from "broken" — the
machine is still powered and still reporting rpm 0 either way. Nobody on the floor records
which it is, so today every downtime figure the system produces is two different things
added together, and a deliberately idle loom is escalated about like a fault.

This is a set-once switch, not a per-incident question. Asking each time would be exactly
the extra work the system exists to remove; one action covers a shift or a week.

While a loom is off plan:
  * no incident is opened for it — there is nothing to report
  * anything already open for it is resolved, so a fault that turned into planned
    downtime does not sit escalating
  * its stopped time is still recorded, but attributed, so "how much did we lose to
    faults" and "how much did we choose not to run" stop being the same number
"""

from __future__ import annotations

from .. import clock, db

REASONS = ("no_order", "no_yarn", "maintenance", "other")


def active(asset_id: str, at: str | None = None) -> dict | None:
    """The live off-plan row for this asset, or None. Expired rows are simply not
    returned — the mandatory until_at is what stops a forgotten flag hiding a real
    fault indefinitely."""
    now = at or clock.now_iso()
    try:
        return db.query_one(
            "SELECT * FROM asset_offplan WHERE asset_id=? AND from_at<=? AND until_at>?",
            (asset_id, now, now),
        )
    except Exception:
        return None       # table missing (pre-migration) must never block paging


def active_map(at: str | None = None) -> dict:
    """asset_id -> row, for every loom currently off plan. One query, because the poller
    checks this for every asset on every cycle."""
    now = at or clock.now_iso()
    try:
        rows = db.query(
            "SELECT * FROM asset_offplan WHERE from_at<=? AND until_at>?", (now, now))
    except Exception:
        return {}
    return {r["asset_id"]: r for r in rows}


def set_offplan(c, asset_id: str, reason: str, until_at: str,
                note: str | None = None, actor: str | None = None,
                at: str | None = None) -> None:
    now = at or clock.now_iso()
    c.execute(
        "INSERT INTO asset_offplan(asset_id, reason, note, from_at, until_at, set_by)"
        " VALUES (?,?,?,?,?,?) ON DUPLICATE KEY UPDATE"
        " reason=VALUES(reason), note=VALUES(note), from_at=VALUES(from_at),"
        " until_at=VALUES(until_at), set_by=VALUES(set_by)",
        (asset_id, reason, note, now, until_at, actor),
    )


def clear(c, asset_id: str) -> int:
    return c.execute("DELETE FROM asset_offplan WHERE asset_id=?", (asset_id,)).rowcount
