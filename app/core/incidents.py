"""Incident lifecycle: open / reason / resolve / reopen, with the grace window.

Both ends are anchored in machine data: an incident opens because an asset changed
state and closes because it resumed. Only two points need a human — the reason and
(Phase 2) the acknowledgement. The false-restart guard (resolve_grace_seconds) means a
machine that runs 20s and stops again reopens the *same* incident rather than creating a
new one.
"""

from __future__ import annotations

from .. import clock, config, db
from . import escalation, events, models


def asset_id_for(cfg: "config.Config", asset_ref: str) -> str:
    return f"{cfg.department}:{asset_ref}"


def ensure_asset(c, cfg: "config.Config", asset_ref: str, label: str | None = None) -> str:
    """Register an asset the feed is reporting. Reporting IS the reactivation signal:
    a machine we retired (active=0) that starts sending data again is back in service —
    the feed is the one source of truth for what exists — so the flag flips back rather
    than leaving the machine half-known: paged about, but missing from the fleet-size
    denominator the power-cut detector divides by."""
    aid = asset_id_for(cfg, asset_ref)
    cur = c.execute(
        "INSERT IGNORE INTO assets(id, department, asset_ref, label, active)"
        " VALUES (?,?,?,?,1)",
        (aid, cfg.department, asset_ref, label),
    )
    if cur.rowcount == 0:                       # row existed — make sure it is live
        # …unless a human retired it. A decommissioned machine is often still powered
        # and still on the network — that is precisely the case this guard exists for,
        # because without it "permanently" lasted until the next poll.
        c.execute("UPDATE assets SET active=1 WHERE id=? AND active=0"
                  " AND decommissioned_at IS NULL", (aid,))
    return aid


def decommissioned_ids(c=None) -> set:
    """Machines a human has retired. They may still be reporting; that is not a signal."""
    q = (c.execute("SELECT id FROM assets WHERE decommissioned_at IS NOT NULL").fetchall()
         if c is not None
         else db.query("SELECT id FROM assets WHERE decommissioned_at IS NOT NULL"))
    return {r["id"] for r in q}


def open_incident(cfg: "config.Config", asset_ref: str, condition: str,
                  at: str | None = None, label: str | None = None) -> dict:
    """Open an incident for a stopped asset (idempotent per open incident)."""
    at = at or clock.now_iso()
    aid = asset_id_for(cfg, asset_ref)
    existing = db.query_one(
        "SELECT * FROM incidents WHERE asset_id=? AND status IN ('open','resolving')"
        " ORDER BY id DESC LIMIT 1",
        (aid,),
    )
    if existing:
        # already open (or a false restart mid-grace) — treat as reopen if resolving
        if existing["status"] == "resolving":
            reopen(cfg, existing["id"], at)
        row = dict(db.query_one("SELECT * FROM incidents WHERE id=?", (existing["id"],)))
        row["created"] = False
        return row

    shift = clock.resolve_shift(clock.parse(at), cfg.shifts)
    with db.transaction() as c:
        ensure_asset(c, cfg, asset_ref, label)
        cur = c.execute(
            "INSERT INTO incidents(asset_id, department, opened_at, status, shift, `condition`)"
            " VALUES (?,?,?, 'open', ?, ?)",
            (aid, cfg.department, at, shift, condition),
        )
        inc_id = cur.lastrowid
        events.log(c, "incident", inc_id, models.K_OPENED, actor="system",
                   detail={"asset_ref": asset_ref, "condition": condition, "shift": shift},
                   department=cfg.department, at=at)
    row = dict(db.query_one("SELECT * FROM incidents WHERE id=?", (inc_id,)))
    row["created"] = True
    return row


def get(incident_id: int) -> dict | None:
    r = db.query_one("SELECT * FROM incidents WHERE id=?", (incident_id,))
    return dict(r) if r else None


def has_reason(c, incident_id: int) -> bool:
    r = c.execute("SELECT 1 FROM incident_reasons WHERE incident_id=? LIMIT 1",
                  (incident_id,)).fetchone()
    return r is not None


def _open_ticket(c, cfg: "config.Config", incident_row, code: str, at: str) -> dict:
    owner_role = cfg.owner_role(code) or "supervisor"
    cur = c.execute(
        "INSERT INTO tickets(incident_id, department, code, owner_role, opened_at, status)"
        " VALUES (?,?,?,?,?, 'open')",
        (incident_row["id"], cfg.department, code, owner_role, at),
    )
    tid = cur.lastrowid
    events.log(c, "ticket", tid, models.K_OPENED, actor="system",
               detail={"code": code, "owner_role": owner_role,
                       "incident_id": incident_row["id"]},
               department=cfg.department, at=at)
    ticket = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    # recurrence override, then start the ladder
    start_rung, trigger = escalation.compute_start(c, cfg, incident_row["asset_id"], code)
    escalation.start_ladder(c, cfg, ticket, start_rung=start_rung, trigger=trigger)
    return dict(ticket)


def set_reason(cfg: "config.Config", incident_id: int, code: str,
               subcode: str | None = None, method: str = "reply",
               actor: str = "system", at: str | None = None) -> dict:
    """Attach a reason to an incident. If the reason is ticketable and no open ticket
    exists yet, open one and start its escalation ladder. Cancels the 'unknown' ladder."""
    at = at or clock.now_iso()
    with db.transaction() as c:
        incident = c.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if incident is None:
            raise ValueError(f"no such incident {incident_id}")
        c.execute(
            "INSERT INTO incident_reasons(incident_id, code, subcode, method, actor, at)"
            " VALUES (?,?,?,?,?,?)",
            (incident_id, code, subcode, method, actor, at),
        )
        events.log(c, "incident", incident_id, models.K_REASON_SET, actor=actor,
                   detail={"code": code, "subcode": subcode, "method": method},
                   department=cfg.department, at=at)
        # the reason has been given — stop asking
        escalation.cancel_for_incident(c, incident_id)

        result = {"incident_id": incident_id, "code": code, "ticket": None}
        if cfg.is_ticketable(code):
            existing = c.execute(
                "SELECT * FROM tickets WHERE incident_id=? AND status!='closed' LIMIT 1",
                (incident_id,),
            ).fetchone()
            if existing is None:
                result["ticket"] = _open_ticket(c, cfg, incident, code, at)
            else:
                result["ticket"] = dict(existing)
    return result


def begin_resolve(cfg: "config.Config", incident_id: int, at: str | None = None) -> None:
    """Asset resumed. Enter the grace window rather than closing immediately."""
    at = at or clock.now_iso()
    grace = float(cfg.source_setting("resolve_grace_seconds", 45))
    with db.transaction() as c:
        inc = c.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if inc is None or inc["status"] != "open":
            return  # already resolving/resolved
        due = clock.plus_seconds(grace, base=clock.parse(at))
        c.execute(
            "UPDATE incidents SET status='resolving', resolved_at=?, resolve_due_at=?"
            " WHERE id=?",
            (at, due, incident_id),
        )


def reopen(cfg: "config.Config", incident_id: int, at: str | None = None) -> None:
    """False restart within the grace window — clear the pending resolve, bump the
    reopen counter on any open ticket."""
    at = at or clock.now_iso()
    with db.transaction() as c:
        inc = c.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if inc is None or inc["status"] != "resolving":
            return
        c.execute(
            "UPDATE incidents SET status='open', resolved_at=NULL, resolve_due_at=NULL"
            " WHERE id=?",
            (incident_id,),
        )
        tkt = c.execute(
            "SELECT * FROM tickets WHERE incident_id=? AND status!='closed' LIMIT 1",
            (incident_id,),
        ).fetchone()
        if tkt:
            c.execute("UPDATE tickets SET reopen_count=reopen_count+1 WHERE id=?", (tkt["id"],))
        events.log(c, "incident", incident_id, models.K_REOPENED, actor="system",
                   detail={"reason": "false_restart_within_grace"},
                   department=cfg.department, at=at)


def commit_resolve(cfg: "config.Config", incident_id: int, at: str | None = None) -> None:
    """Grace elapsed and still resolving — finalise. Compute duration, close the ticket,
    cancel timers, and apply the min-duration gate (auto-code a short stop)."""
    at = at or clock.now_iso()
    min_dur = cfg.min_duration_seconds
    with db.transaction() as c:
        inc = c.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if inc is None or inc["status"] != "resolving":
            return
        resumed_at = inc["resolved_at"] or at
        duration_s = int((clock.parse(resumed_at) - clock.parse(inc["opened_at"])).total_seconds())
        c.execute(
            "UPDATE incidents SET status='resolved', resolved_at=?, duration_s=?,"
            " resolve_due_at=NULL WHERE id=?",
            (resumed_at, duration_s, incident_id),
        )
        # min-duration gate: unattributed short stop auto-codes, never prompts
        if not has_reason(c, incident_id) and duration_s < min_dur:
            c.execute(
                "INSERT INTO incident_reasons(incident_id, code, subcode, method, actor, at)"
                " VALUES (?, 'short_stop', NULL, 'auto', 'system', ?)",
                (incident_id, resumed_at),
            )
            events.log(c, "incident", incident_id, models.K_REASON_SET, actor="system",
                       detail={"code": "short_stop", "method": "auto",
                               "duration_s": duration_s},
                       department=cfg.department, at=resumed_at)
        # close any open ticket + cancel all pending timers
        tkt = c.execute(
            "SELECT * FROM tickets WHERE incident_id=? AND status!='closed' LIMIT 1",
            (incident_id,),
        ).fetchone()
        if tkt:
            c.execute(
                "UPDATE tickets SET status='closed', closed_at=?, close_reason='asset_resumed'"
                " WHERE id=?",
                (resumed_at, tkt["id"]),
            )
            escalation.cancel_for_ticket(c, tkt["id"])
            events.log(c, "ticket", tkt["id"], models.K_CLOSED, actor="system",
                       detail={"close_reason": "asset_resumed", "duration_s": duration_s},
                       department=cfg.department, at=resumed_at)
        escalation.cancel_for_incident(c, incident_id)
        events.log(c, "incident", incident_id, models.K_RESOLVED, actor="system",
                   detail={"duration_s": duration_s}, department=cfg.department, at=resumed_at)
