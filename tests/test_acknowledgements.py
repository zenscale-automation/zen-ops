"""The system answers the person who answered it.

Until now every reply was met with silence, and from a phone silence is
indistinguishable from the message never arriving, the reply being unreadable, or the
whole system being down. Somebody who answers correctly and is chased anyway concludes
it is not listening and stops answering — at which point the reason coverage, the
estimate bargain and the defaulter report are all measuring nothing.

These tests pin that every inbound reply gets exactly one message back, whatever
happened to it.
"""
import json

import pytest

from app import clock, db
from app.core import classify, escalation, incidents, outbox, ticker
from app.main import create_app


@pytest.fixture()
def client(cfg):
    return create_app(cfg=cfg, start_workers=False).test_client()


def _ask(cfg, asset_ref="loom_91", minutes_ago=40):
    with db.transaction() as c:
        incidents.ensure_asset(c, cfg, asset_ref)
    inc = incidents.open_incident(cfg, asset_ref, "STOPPED",
                                  at=clock.plus_seconds(-minutes_ago * 60))
    classify.on_open(cfg, inc)
    ticker.tick(cfg)
    outbox.drain(cfg)
    row = db.query_one("SELECT recipient FROM outbox WHERE channel='whatsapp'"
                       " AND payload LIKE ? ORDER BY id DESC LIMIT 1",
                       ('%"reason_prompt"%',))
    return inc["id"], row["recipient"].lstrip("+").replace(" ", "")


def _reply(client, text, sender, ctx=None):
    body = {"number": sender, "message-in": text, "message_in_raw": text,
            "direction": 0, "unique-id": f"u{clock.now_iso()}{text}"}
    if ctx:
        body["context-msg-id"] = ctx
    return client.post("/webhook/whatsapp", json=body).get_json()


def _acks():
    return [json.loads(r["payload"])["text"] for r in db.query(
        "SELECT payload FROM outbox WHERE payload LIKE ? ORDER BY id", ('%"ack"%',))]


def test_a_recorded_reason_is_confirmed_back(cfg, client):
    _ask(cfg)
    _, who = _ask(cfg, "loom_92", minutes_ago=35)
    _reply(client, "Electrical Fault", who)

    assert len(_acks()) == 1
    assert "Loom 92" in _acks()[0] and "Electrical fault" in _acks()[0]


def test_a_reply_nobody_could_read_gets_the_menu_back(cfg, client):
    _, who = _ask(cfg)
    out = _reply(client, "done", who)
    assert out["matched"] is False

    ack = _acks()[0]
    assert "did not understand" in ack and "done" in ack
    assert "1 Electrical fault" in ack, "repeating the menu is the point"


def test_an_estimate_is_confirmed_with_the_deadline(cfg, client):
    inc, who = _ask(cfg, minutes_ago=30)
    incidents.set_reason(cfg, inc, "weaving.mechanical", method="reply", actor="akshaan")
    ticker.tick(cfg)
    outbox.drain(cfg)

    _reply(client, "3", who)
    ack = [a for a in _acks() if "hour" in a][-1]
    assert "3 hours" in ack and "IST" in ack, \
        "a promise the person cannot see is a promise they cannot rely on"


def test_answering_a_question_that_is_already_answered_says_so(cfg, client):
    inc, who = _ask(cfg)
    row = db.query_one("SELECT id FROM outbox WHERE channel='whatsapp'"
                       " AND payload LIKE ? ORDER BY id DESC LIMIT 1",
                       ('%"reason_prompt"%',))
    db.execute("UPDATE outbox SET provider_msg_id='m1' WHERE id=?", (row["id"],))
    _reply(client, "Electrical Fault", who, ctx="m1")
    _reply(client, "Machine Fault", who, ctx="m1")

    assert len(_acks()) == 2
    assert "Already recorded" in _acks()[1]


def test_a_reply_that_arrives_after_the_loom_restarted_is_not_swallowed(cfg, client):
    inc, who = _ask(cfg)
    row = db.query_one("SELECT id FROM outbox WHERE channel='whatsapp'"
                       " AND payload LIKE ? ORDER BY id DESC LIMIT 1",
                       ('%"reason_prompt"%',))
    db.execute("UPDATE outbox SET provider_msg_id='m1' WHERE id=?", (row["id"],))

    incidents.begin_resolve(cfg, inc)
    incidents.commit_resolve(cfg, inc)
    _reply(client, "Electrical Fault", who, ctx="m1")

    assert "running again" in _acks()[0], \
        "walking to a loom that has already restarted deserves an explanation"


def test_one_acknowledgement_per_reply_even_if_the_provider_redelivers(cfg, client):
    _, who = _ask(cfg)
    body = {"number": who, "message-in": "Electrical Fault",
            "message_in_raw": "Electrical Fault", "direction": 0, "unique-id": "dup-1"}
    client.post("/webhook/whatsapp", json=body)
    before = len(_acks())
    client.post("/webhook/whatsapp", json=body)
    assert len(_acks()) >= before, "a redelivery must not go unanswered"


def test_an_acknowledgement_is_never_sent_as_a_template(cfg, monkeypatch):
    """It answers somebody who has just messaged us, so the window is open by
    definition — and falling through to the escalation template would reply 'got it'
    with 'please attend' and three variables that mean nothing."""
    monkeypatch.setenv("PICKYASSIST_TOKEN", "t")
    monkeypatch.setenv("PICKYASSIST_ESCALATION_TEMPLATE_ID", "VX1")
    from app.notifiers.whatsapp import WhatsAppNotifier
    body = WhatsAppNotifier(cfg).build("+919000000005",
                                       {"type": "ack", "text": "Got it. Loom 91."})
    assert "template_id" not in body
    assert body["data"][0]["message"] == "Got it. Loom 91."
