"""The ticker asks the database what is due, ~every 30s.

Three kinds of thing can be due, all of them ROWS (Rule 1), so a restart resumes them
instead of dropping a countdown:
  * an incident whose resolve grace has elapsed  -> commit the resolution,
  * a pending escalation rung (a ticket ladder)  -> page the next role,
  * a pending 'unknown' rung (action ask_reason)  -> send / re-send the reason prompt.

Resolutions are processed first so a machine that has already resumed is never paged.
"""

from __future__ import annotations

import logging

from .. import clock, config, db
from . import escalation, incidents

log = logging.getLogger("ops.ticker")


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
    fired = wedged = 0
    for r in due:
        # Per-row, because the query is ORDER BY due_at and a row that raises is rolled
        # back still pending with a past due_at — so it is first again on the next tick,
        # and on every tick after that. One unfireable rung would stop EVERY escalation
        # in the plant, permanently, with no symptom but a frozen heartbeat.
        try:
            with db.transaction() as c:
                fresh = c.execute(
                    "SELECT * FROM escalations WHERE id=? AND status='pending'", (r["id"],)
                ).fetchone()
                if fresh:
                    escalation.fire(c, cfg, fresh)
                    fired += 1
        except Exception:
            wedged += 1
            log.exception(
                "escalation %s could not fire — parking it so the queue keeps moving",
                r["id"])
            # Park it out of the way. Cancelling loses the fault; leaving it pending
            # blocks everyone behind it. `wedged` is surfaced on /health so this is not
            # a quiet burial.
            try:
                with db.transaction() as c:
                    c.execute("UPDATE escalations SET status='wedged' WHERE id=?",
                              (r["id"],))
            except Exception:
                log.exception("could not park escalation %s", r["id"])

    return {"resolved": len(resolving), "fired": fired, "wedged": wedged}
