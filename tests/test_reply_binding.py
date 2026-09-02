"""Binding an answer to the question it actually answers.

Every question this system asks is answered with a bare number or a tapped button, and
a supervisor with two stopped looms has two identical-looking questions on one screen.
Guessing between them by content is impossible, so until now the rule was "your most
recent outstanding question" — which is right most of the time and silently wrong in
exactly the cases that matter: a duplicate tap, an answer to yesterday's message, two
questions outstanding at once.

WhatsApp already tells us which message was replied to, and PickyAssist passes it
through as context-msg-id. These tests pin the two guarantees that gives us: the answer
lands on the question it was sent for, and a second answer to an answered question
changes nothing at all.
"""
import json

import pytest

from app import clock, db
from app.core import incidents, outbox, ticker
from app.main import create_app


@pytest.fixture()
def client(cfg):
    return create_app(cfg=cfg, start_workers=False).test_client()


def _ask(cfg, asset_ref, provider_msg_id, minutes_ago=40):
    """Open an incident, let the ladder ask its reason question, and pin the provider id
    the way a real send does."""
    with db.transaction() as c:
        incidents.ensure_asset(c, cfg, asset_ref)
    # Distinct stop times on purpose: two looms stopping within 5 seconds of each other
    # IS a fleet stop, and the classifier correctly auto-codes it power_failure before
    # anyone is asked anything. These tests are about two INDEPENDENT faults.
    inc = incidents.open_incident(cfg, asset_ref, "STOPPED",
                                  at=clock.plus_seconds(-minutes_ago * 60))
    from app.core import classify
    classify.on_open(cfg, inc)
    ticker.tick(cfg)
    outbox.drain(cfg)
    row = db.query_one(
        "SELECT id, recipient, payload FROM outbox WHERE channel='whatsapp'"
        " AND payload LIKE '%reason_prompt%' ORDER BY id DESC LIMIT 1")
    assert json.loads(row["payload"])["type"] == "reason_prompt"
    assert json.loads(row["payload"])["incident_id"] == inc["id"]
    db.execute("UPDATE outbox SET provider_msg_id=? WHERE id=?",
               (provider_msg_id, row["id"]))
    # The reply must come from whoever was actually asked — derived from the message,
    # never a number pinned here that a routing change would quietly invalidate.
    return inc["id"], row["recipient"].lstrip("+").replace(" ", "")


def _reply(client, text, sender, context_msg_id=None):
    body = {"number": sender, "message-in": text, "message_in_raw": text,
            "direction": 0, "unique-id": f"u{clock.now_iso()}{text}"}
    if context_msg_id:
        body["context-msg-id"] = context_msg_id
    return client.post("/webhook/whatsapp", json=body).get_json()


def _reason_of(incident_id):
    r = db.query_one("SELECT code FROM incident_reasons WHERE incident_id=?",
                     (incident_id,))
    return r["code"] if r else None


def test_the_answer_lands_on_the_question_it_replied_to_not_the_newest(cfg, client):
    """Two looms stopped, two questions outstanding. The supervisor scrolls up and
    answers the FIRST one. Most-recent-wins would put that answer on the wrong loom."""
    first, who = _ask(cfg, "loom_91", "msg-first")
    second, _ = _ask(cfg, "loom_92", "msg-second", minutes_ago=35)

    out = _reply(client, "Electrical Fault", who, context_msg_id="msg-first")
    assert out["matched"] is True and out["incident_id"] == first

    assert _reason_of(first) == "weaving.electrical"
    assert _reason_of(second) is None, "the loom they did not answer for stays unanswered"


def test_a_second_answer_to_the_same_question_changes_nothing(cfg, client):
    first, who = _ask(cfg, "loom_91", "msg-first")
    _reply(client, "Electrical Fault", who, context_msg_id="msg-first")
    assert _reason_of(first) == "weaving.electrical"

    out = _reply(client, "Machine Fault", who, context_msg_id="msg-first")
    assert out["matched"] is False and out["reason"] == "already answered"
    assert _reason_of(first) == "weaving.electrical", "the first answer is the record"
    assert db.query_one("SELECT COUNT(*) n FROM incident_reasons WHERE incident_id=?",
                        (first,))["n"] == 1


def test_a_duplicate_tap_does_not_leak_onto_another_incident(cfg, client):
    """The dangerous case. They answer loom 91, then a newer question arrives for loom
    92, then they tap the OLD button again — a stray tap must not answer loom 92."""
    first, who = _ask(cfg, "loom_91", "msg-first")
    _reply(client, "Electrical Fault", who, context_msg_id="msg-first")
    second, _ = _ask(cfg, "loom_92", "msg-second", minutes_ago=35)

    out = _reply(client, "Electrical Fault", who, context_msg_id="msg-first")
    assert out["matched"] is False
    assert _reason_of(second) is None, \
        "a re-tap of an answered question must never become another loom's answer"


def test_a_reply_to_a_message_we_never_sent_falls_back_to_the_old_rule(cfg, client):
    # Unknown context (a forwarded message, a test push outside the outbox): we do not
    # recognise it, so behaviour is exactly what it was before — most recent question.
    first, who = _ask(cfg, "loom_91", "msg-first")
    out = _reply(client, "Electrical Fault", who, context_msg_id="msg-we-never-sent")
    assert out["matched"] is True and out["incident_id"] == first


def test_a_reply_with_no_context_still_works(cfg, client):
    # Typing a bare answer instead of using reply-to is normal and must keep working.
    first, who = _ask(cfg, "loom_91", "msg-first")
    out = _reply(client, "Electrical Fault", who)
    assert out["matched"] is True and out["incident_id"] == first
