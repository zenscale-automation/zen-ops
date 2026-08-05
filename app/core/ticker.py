"""The ticker asks the database what is due, ~every 30s.

Three kinds of thing can be due, all of them ROWS (Rule 1), so a restart resumes them
instead of dropping a countdown:
  * an incident whose resolve grace has elapsed  -> commit the resolution,
  * a pending escalation rung (a ticket ladder)  -> page the next role,
  * a pending 'unknown' rung (action ask_reason)  -> send / re-send the reason prompt.

Resolutions are processed first so a machine that has already resumed is never paged.
"""

from __future__ import annotations

from .. import clock, config, db
from . import escalation, incidents


def tick(cfg: "config.Config") -> dict:
    now = clock.now_iso()

    # 1) commit resolutions whose grace window has elapsed
    resolving = db.query(
        "SELECT id FROM incidents WHERE status='resolving'"
        " AND resolve_due_at IS NOT NULL AND resolve_due_at<=?",
        (now,),
    )
    for r in resolving:
        incidents.commit_resolve(cfg, r["id"], at=now)

    # 2) fire due escalation / prompt rungs
    due = db.query(
        "SELECT id FROM escalations WHERE status='pending' AND due_at<=?"
        " ORDER BY due_at, id",
        (now,),
    )
    fired = 0
    for r in due:
        with db.transaction() as c:
            fresh = c.execute(
                "SELECT * FROM escalations WHERE id=? AND status='pending'", (r["id"],)
            ).fetchone()
            if fresh:
                escalation.fire(c, cfg, fresh)
                fired += 1

    return {"resolved": len(resolving), "fired": fired}
