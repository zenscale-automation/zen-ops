"""Seed ~a day of resolved downtime history so the dashboard charts have something to
show in a demo. Inserts backdated resolved incidents (+ reasons, + closed tickets for
ticketable ones) directly. Leaves any currently-open incidents untouched.

Run: python -m scripts.seed_demo
"""

import random
from datetime import timedelta

from app import clock, config, db
from app.core import events, incidents, models

random.seed(7)

# (code, min_dur_min, max_dur_min, owner_role_or_None, method)
SCEN = [
    ("weaving.electrical",      20, 45, "electrician", "reply"),
    ("weaving.electrical",      20, 45, "electrician", "reply"),
    ("weaving.mechanical",      35, 80, "fitter",      "reply"),
    ("weaving.mechanical",      35, 80, "fitter",      "reply"),
    ("weaving.yarn_not_arrived",12, 28, "yarn_store",  "reply"),
    ("weaving.operator_absent", 10, 25, "supervisor",  "reply"),
    ("weaving.beam_change",     60, 110, None,         "reply"),
    ("weaving.punch_queue",      8, 14, None,          "auto"),
    ("power_failure",            2,  5, None,          "auto"),
    ("short_stop",               1,  2, None,          "auto"),
    ("short_stop",               1,  2, None,          "auto"),
]


def main():
    cfg = config.load()
    db.init(cfg.db_params(), cfg.table_prefix)
    now = clock.now()
    n = 0
    for k in range(26):
        code, lo, hi, owner, method = random.choice(SCEN)
        loom = f"loom_{random.randint(1, 44)}"
        if loom in ("loom_5", "loom_12"):
            continue
        opened = now - timedelta(minutes=random.randint(30, 22 * 60))
        dur_min = random.randint(lo, hi)
        resolved = opened + timedelta(minutes=dur_min)
        if resolved > now:
            resolved = now
        dur_s = int((resolved - opened).total_seconds())
        shift = clock.resolve_shift(opened, cfg.shifts)
        aid = incidents.asset_id_for(cfg, loom)
        with db.transaction() as c:
            incidents.ensure_asset(c, cfg, loom)
            cur = c.execute(
                "INSERT INTO incidents(asset_id, department, opened_at, resolved_at,"
                " duration_s, shift, `condition`, status)"
                " VALUES (?,?,?,?,?,?, 'STOPPED', 'resolved')",
                (aid, cfg.department, clock.to_iso(opened), clock.to_iso(resolved),
                 dur_s, shift),
            )
            iid = cur.lastrowid
            c.execute(
                "INSERT INTO incident_reasons(incident_id, code, method, actor, at)"
                " VALUES (?,?,?,?,?)",
                (iid, code, method, "system" if method == "auto" else "amarjit_s",
                 clock.to_iso(opened)),
            )
            events.log(c, "incident", iid, models.K_OPENED, at=clock.to_iso(opened),
                       department=cfg.department, detail={"asset_ref": loom, "seed": True})
            events.log(c, "incident", iid, models.K_RESOLVED, at=clock.to_iso(resolved),
                       department=cfg.department, detail={"duration_s": dur_s})
            if cfg.is_ticketable(code):
                notified = opened + timedelta(minutes=random.randint(0, 2))
                tc = c.execute(
                    "INSERT INTO tickets(incident_id, department, code, owner_role,"
                    " opened_at, first_notified_at, closed_at, close_reason,"
                    " reopen_count, status) VALUES (?,?,?,?,?,?,?, 'asset_resumed', 0, 'closed')",
                    (iid, cfg.department, code, owner or "supervisor",
                     clock.to_iso(opened), clock.to_iso(notified), clock.to_iso(resolved)),
                )
                events.log(c, "ticket", tc.lastrowid, models.K_CLOSED,
                           at=clock.to_iso(resolved), department=cfg.department,
                           detail={"close_reason": "asset_resumed", "seed": True})
        n += 1
    print(f"seeded {n} resolved incidents")


if __name__ == "__main__":
    main()
