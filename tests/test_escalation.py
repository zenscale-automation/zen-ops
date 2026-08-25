"""Escalation ladder advance + recurrence override."""

from datetime import timedelta

from app import clock, db
from app.core import incidents, ticker


def _fired_roles(ticket_id):
    return [r["notify_role"] for r in db.query(
        "SELECT notify_role FROM escalations WHERE ticket_id=? AND status='fired' ORDER BY rung",
        (ticket_id,))]


def test_ladder_progresses_owner_supervisor_shiftincharge(cfg):
    inc = incidents.open_incident(cfg, "loom_5", "STOPPED")
    res = incidents.set_reason(cfg, inc["id"], "weaving.electrical", method="reply", actor="amarjit_s")
    tid = res["ticket"]["id"]

    ticker.tick(cfg)                      # rung 0 due immediately -> owner
    assert _fired_roles(tid) == ["owner"]

    clock.CLOCK.advance(20 * 60)          # electrical rung 1 at +20m -> supervisor
    ticker.tick(cfg)
    assert _fired_roles(tid) == ["owner", "supervisor"]

    clock.CLOCK.advance(25 * 60)          # rung 2 at +45m -> shift_incharge
    ticker.tick(cfg)
    assert _fired_roles(tid) == ["owner", "supervisor", "shift_incharge"]

    # ladder exhausted: no more pending rungs
    assert db.query_one(
        "SELECT COUNT(*) n FROM escalations WHERE ticket_id=? AND status='pending'", (tid,))["n"] == 0


def test_first_notified_at_set_once(cfg):
    inc = incidents.open_incident(cfg, "loom_5", "STOPPED")
    res = incidents.set_reason(cfg, inc["id"], "weaving.electrical", method="reply", actor="x")
    ticker.tick(cfg)
    t1 = db.query_one("SELECT first_notified_at FROM tickets WHERE id=?", (res["ticket"]["id"],))["first_notified_at"]
    assert t1 is not None
    clock.CLOCK.advance(20 * 60)
    ticker.tick(cfg)
    t2 = db.query_one("SELECT first_notified_at FROM tickets WHERE id=?", (res["ticket"]["id"],))["first_notified_at"]
    assert t2 == t1  # not overwritten by later escalations


def _resolve(cfg, incident_id):
    incidents.begin_resolve(cfg, incident_id)
    clock.CLOCK.advance(60)
    ticker.tick(cfg)
    clock.CLOCK.advance(60)


def test_recurrence_skips_to_higher_rung(cfg):
    ticket = None
    for i in range(3):
        inc = incidents.open_incident(cfg, "loom_9", "STOPPED")
        res = incidents.set_reason(cfg, inc["id"], "weaving.electrical", method="reply", actor="x")
        ticket = res["ticket"]
        if i < 2:
            _resolve(cfg, inc["id"])

    # the 3rd occurrence within the window jumps straight to rung 2, trigger=recurrence
    first = db.query_one(
        "SELECT rung, `trigger` FROM escalations WHERE ticket_id=? ORDER BY id LIMIT 1",
        (ticket["id"],))
    assert first["rung"] == 2
    assert first["trigger"] == "recurrence"


def test_unknown_ladder_prompts_then_escalates(cfg):
    inc = incidents.open_incident(cfg, "loom_7", "STOPPED")
    from app.core import classify
    classify.on_open(cfg, inc)  # schedules unknown ladder (no auto-classify)

    # Walk the ladder as configured rather than at hardcoded intervals — the timings are
    # tuned against real feed data, and pinning them here turns tuning into a red suite.
    for rung in cfg.unknown_ladder:
        clock.CLOCK.set_virtual(
            clock.parse(inc["opened_at"]) + timedelta(minutes=rung["after_minutes"] + 1))
        ticker.tick(cfg)

    fired = db.query(
        "SELECT action, notify_role FROM escalations WHERE incident_id=? AND status='fired' ORDER BY rung",
        (inc["id"],))
    assert [f["action"] for f in fired] == ["ask_reason", "ask_reason", None]
    assert fired[-1]["notify_role"] == "shift_incharge"
