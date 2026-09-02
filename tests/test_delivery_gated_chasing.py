"""Never chase somebody about a question they never received.

Found live. A loom stopped, the fault question was queued while WhatsApp was broken and
never reached a phone, and the ladder carried on regardless — so an hour later the only
message that DID arrive read "down 90 min with no reason given, please check", to a
person who had never been asked anything. The system was demanding an answer to a
question it had failed to deliver, and no amount of chasing could produce one.

Delivery reports tell us which of the two happened. These tests pin the rule: a reminder
that assumes an earlier question is only sent once that question actually arrived —
otherwise the rung asks again instead.
"""
import datetime
import json

from app import clock, db
from app.core import classify, escalation, incidents, outbox, ticker


def _stopped_loom(cfg, asset_ref="loom_91", minutes_ago=95):
    with db.transaction() as c:
        incidents.ensure_asset(c, cfg, asset_ref)
    inc = incidents.open_incident(cfg, asset_ref, "STOPPED",
                                  at=clock.plus_seconds(-minutes_ago * 60))
    classify.on_open(cfg, inc)
    return inc


def _messages():
    return [json.loads(r["payload"]) for r in db.query(
        "SELECT payload FROM outbox WHERE channel='whatsapp' ORDER BY id")]


def _mark(delivery_status, kind=None):
    """Apply a delivery verdict to the newest matching outbound message."""
    rows = db.query("SELECT id, payload FROM outbox WHERE channel='whatsapp'"
                    " ORDER BY id DESC")
    for r in rows:
        if kind is None or json.loads(r["payload"]).get("type") == kind:
            db.execute("UPDATE outbox SET delivery_status=? WHERE id=?",
                       (delivery_status, r["id"]))
            return r["id"]
    raise AssertionError(f"no {kind} message to mark")


def _run(cfg, ticks=3):
    """Drive the ladder forward. The unknown ladder asks twice before it ever reminds,
    and each tick fires only what is due, so reaching the reminder rung takes several."""
    for _ in range(ticks):
        ticker.tick(cfg)
        outbox.drain(cfg)


def test_a_reminder_becomes_a_re_ask_when_the_question_never_arrived(cfg):
    _stopped_loom(cfg)
    ticker.tick(cfg)                       # rung 0 asks the reason
    outbox.drain(cfg)
    assert _messages()[-1]["type"] == "reason_prompt"
    _mark("failed", kind="reason_prompt")  # Meta dropped it; nobody saw it

    # Far enough on that the ladder has reached its reminder rung.
    clock.CLOCK.set_virtual(clock.now() + datetime.timedelta(hours=2))
    _run(cfg)

    kinds = [m["type"] for m in _messages()]
    assert kinds[-1] == "reason_prompt", \
        "the ladder must ask again, not send 'no reason given' to someone never asked"
    assert "escalation" not in kinds, "no reminder may go out before a question lands"


def test_once_the_question_is_delivered_the_reminders_resume(cfg):
    _stopped_loom(cfg)
    ticker.tick(cfg)
    outbox.drain(cfg)
    _mark("delivered", kind="reason_prompt")   # it arrived; they simply have not replied

    clock.CLOCK.set_virtual(clock.now() + datetime.timedelta(hours=2))
    _run(cfg)

    assert _messages()[-1]["type"] == "escalation", \
        "a delivered question that goes unanswered is exactly what reminders are for"


def test_a_read_receipt_counts_as_arrival(cfg):
    _stopped_loom(cfg)
    ticker.tick(cfg)
    outbox.drain(cfg)
    _mark("read", kind="reason_prompt")

    clock.CLOCK.set_virtual(clock.now() + datetime.timedelta(hours=2))
    _run(cfg)
    assert _messages()[-1]["type"] == "escalation"


def test_a_missing_delivery_report_does_not_hold_the_loop_open_forever(cfg):
    """Reports can stop arriving for their own reasons. An old send with no verdict at
    all is treated as arrived — otherwise a provider that quietly stops reporting turns
    every ladder into an endless re-ask and nobody is ever escalated to."""
    _stopped_loom(cfg)
    ticker.tick(cfg)
    outbox.drain(cfg)
    assert _messages()[-1]["type"] == "reason_prompt"   # no verdict recorded at all

    clock.CLOCK.set_virtual(clock.now() + datetime.timedelta(hours=2))
    _run(cfg)
    assert _messages()[-1]["type"] == "escalation"


def test_the_re_ask_is_not_swallowed_by_the_reminder_it_replaced(cfg):
    """The reminder and the re-ask land on the same rung. Sharing a dedupe key would let
    INSERT IGNORE drop the question while the event log still said it was sent."""
    _stopped_loom(cfg)
    ticker.tick(cfg)
    outbox.drain(cfg)
    _mark("failed", kind="reason_prompt")

    for _ in range(3):
        clock.CLOCK.set_virtual(clock.now() + datetime.timedelta(hours=2))
        _run(cfg, ticks=2)
        _mark("failed", kind="reason_prompt")

    asks = [m for m in _messages() if m["type"] == "reason_prompt"]
    assert len(asks) >= 3, "each cycle must produce its own question, not a swallowed one"


def test_a_ticket_re_asks_for_the_estimate_rather_than_nagging(cfg):
    """Same rule on the ticket ladder: if the hours question never arrived, the later
    rungs ask for hours again instead of 'still down, please step in'."""
    inc = _stopped_loom(cfg, minutes_ago=30)
    incidents.set_reason(cfg, inc["id"], "weaving.mechanical", method="reply",
                         actor="akshaan")
    ticker.tick(cfg)
    outbox.drain(cfg)
    assert any(m["type"] == "eta_request" for m in _messages())
    _mark("failed", kind="eta_request")

    clock.CLOCK.set_virtual(clock.now() + datetime.timedelta(hours=3))
    _run(cfg)
    assert _messages()[-1]["type"] == "eta_request", \
        "an unanswered-but-undelivered estimate question must be asked again"
