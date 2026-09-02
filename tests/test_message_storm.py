"""Nobody gets three contradictory messages in ninety seconds.

Two faults that compounded into one storm, both found by auditing the flow from the
phone's side:

  * When an estimate lapsed, the ladder was rescheduled from the ticket's opening time
    — hours in the past — so every remaining rung was already overdue and fired on the
    next ticks.
  * The rescue that re-asks when no question has arrived asked "is it DEFINITELY
    delivered?", and a question sent seconds earlier has no verdict yet, so it converted
    those reminders into two more copies of the same question. Worse, a provider report
    of 'submitted' is neither success nor failure, so a row stuck there jammed every
    reminder into a re-ask forever.
"""
import datetime
import json

from app import clock, db
from app.core import classify, escalation, incidents, outbox, ticker


def _ticketed(cfg, asset_ref="loom_91", minutes_ago=30):
    with db.transaction() as c:
        incidents.ensure_asset(c, cfg, asset_ref)
    inc = incidents.open_incident(cfg, asset_ref, "STOPPED",
                                  at=clock.plus_seconds(-minutes_ago * 60))
    classify.on_open(cfg, inc)
    incidents.set_reason(cfg, inc["id"], "weaving.mechanical", method="reply",
                         actor="akshaan")
    tkt = db.query_one("SELECT * FROM tickets WHERE incident_id=?", (inc["id"],))
    return inc, tkt


def _sent():
    return [json.loads(r["payload"]) for r in db.query(
        "SELECT payload FROM outbox WHERE channel='whatsapp' ORDER BY id")]


def _run(cfg, ticks=3):
    for _ in range(ticks):
        ticker.tick(cfg)
        outbox.drain(cfg)


def test_a_lapsed_estimate_sends_one_message_not_three(cfg):
    inc, tkt = _ticketed(cfg)
    _run(cfg, ticks=1)                                   # rung 0 asks for hours
    escalation.set_eta(cfg, tkt["id"], 2, actor="akshaan")

    clock.CLOCK.set_virtual(clock.now() + datetime.timedelta(hours=2, minutes=1))
    before = len(_sent())
    _run(cfg, ticks=4)                                   # four ticker passes, two minutes

    new = _sent()[before:]
    assert len(new) == 1, f"one message, not {len(new)}: {[m.get('type') for m in new]}"
    assert "STILL stopped" in new[0]["text"], \
        "and it must say the estimate lapsed, not read as a brand-new fault"


def test_the_ladder_after_a_lapse_is_paced_from_now(cfg):
    inc, tkt = _ticketed(cfg)
    _run(cfg, ticks=1)
    escalation.set_eta(cfg, tkt["id"], 2, actor="akshaan")
    clock.CLOCK.set_virtual(clock.now() + datetime.timedelta(hours=2, minutes=1))
    _run(cfg, ticks=1)

    nxt = db.query_one("SELECT due_at FROM escalations WHERE ticket_id=?"
                       " AND status='pending' ORDER BY due_at LIMIT 1", (tkt["id"],))
    assert nxt and nxt["due_at"] > clock.now_iso(), \
        "the next reminder must be in the future, not overdue the moment it is written"


def test_an_interim_delivery_report_does_not_jam_the_loop(cfg):
    """'submitted' is neither delivered nor failed. A row stuck there used to satisfy
    no branch, so every reminder silently became another copy of the question."""
    inc, tkt = _ticketed(cfg)
    _run(cfg, ticks=1)
    db.execute("UPDATE outbox SET delivery_status='submitted' WHERE channel='whatsapp'")

    clock.CLOCK.set_virtual(clock.now() + datetime.timedelta(hours=3))
    _run(cfg, ticks=4)

    kinds = [m["type"] for m in _sent()]
    assert kinds.count("eta_request") == 1, \
        "the question was asked once; the rest must be reminders"
    assert "escalation" in kinds


def test_a_question_that_definitely_died_is_still_re_asked(cfg):
    # The original rule must survive: a failed send means nobody was asked.
    inc, tkt = _ticketed(cfg)
    _run(cfg, ticks=1)
    db.execute("UPDATE outbox SET status='failed', delivery_status='failed'"
               " WHERE channel='whatsapp'")

    clock.CLOCK.set_virtual(clock.now() + datetime.timedelta(hours=3))
    before = len(_sent())
    _run(cfg, ticks=3)
    assert any(m["type"] == "eta_request" for m in _sent()[before:]), \
        "every ask died, so the rung must ask again rather than chase an answer"


def test_a_ticket_reminder_counts_minutes_from_the_stop_not_the_ticket(cfg):
    """The ticket opens when the supervisor answers, 20+ minutes after the loom stopped.
    Measuring the sentence from the ticket told people a loom down 47 minutes had been
    down 25 — and the earlier prompt had already said 20."""
    inc, tkt = _ticketed(cfg, minutes_ago=40)
    _run(cfg, ticks=1)
    escalation.set_eta(cfg, tkt["id"], 1, actor="akshaan")

    clock.CLOCK.set_virtual(clock.now() + datetime.timedelta(hours=1, minutes=1))
    _run(cfg, ticks=2)

    msg = [m for m in _sent() if m["type"] == "escalation"]
    assert msg, "a reminder should have gone out"
    down = msg[-1]["minutes_down"]
    assert down >= 100, \
        f"the loom has been down about 101 min; the message says {down}"
