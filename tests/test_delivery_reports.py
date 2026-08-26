"""Delivery reports — the answer to "how do I know a message actually arrived?".

PickyAssist answers 100 the moment a message is QUEUED and Meta's verdict lands later,
in a web panel. During the pilot that gap hid an hour of total send failure (131037)
behind a green /health and an outbox full of 'sent'. Their Event Webhook now posts the
verdict to /webhook/events; these tests pin the two behaviours that matter: a failure
flips the row to status='failed' (which the health metric counts), and a report for a
message we never sent changes nothing.
"""
import pytest

from app import clock, db
from app.main import create_app


@pytest.fixture()
def client(cfg):
    return create_app(cfg=cfg, start_workers=False).test_client()


def _sent_row(msg_id="9844217"):
    db.execute(
        "INSERT INTO outbox(channel, recipient, payload, status, created_at, sent_at,"
        " provider_msg_id) VALUES ('whatsapp', '+919000000005', '{}', 'sent', ?, ?, ?)",
        (clock.now_iso(), clock.now_iso(), msg_id))
    return db.query_one("SELECT id FROM outbox WHERE provider_msg_id=?", (msg_id,))["id"]


def _report(status, msg_id="9844217", **extra):
    entry = {"push_id": "7478630", "number": "919000000005", "msg_id": msg_id,
             "status": str(status), "reference_number": "inc:1:rung:0",
             "template_id": "AX211481604", "channel_id": "5", **extra}
    return {"project_id": "1", "event_id": "1", "data": [entry]}


def test_a_failure_flips_the_row_and_keeps_the_providers_reason(cfg, client):
    oid = _sent_row()
    resp = client.post("/webhook/events", json=_report(
        2, error_code="131037", error_message="display name not approved"))
    assert resp.status_code == 200 and resp.get_json()["failed"] == 1

    row = db.query_one("SELECT * FROM outbox WHERE id=?", (oid,))
    assert row["status"] == "failed", \
        "status='failed' is what the outbox_failed_1h health metric counts"
    assert row["delivery_status"] == "failed"
    assert "display name" in row["delivery_error"]


def test_delivered_and_read_are_recorded_without_touching_send_state(cfg, client):
    oid = _sent_row()
    client.post("/webhook/events", json=_report(1))
    row = db.query_one("SELECT status, delivery_status FROM outbox WHERE id=?", (oid,))
    assert (row["status"], row["delivery_status"]) == ("sent", "delivered")

    client.post("/webhook/events", json=_report(3))
    assert db.query_one("SELECT delivery_status FROM outbox WHERE id=?",
                        (oid,))["delivery_status"] == "read"


def test_a_report_for_a_message_we_never_sent_changes_nothing(cfg, client):
    oid = _sent_row("111")
    resp = client.post("/webhook/events", json=_report(2, msg_id="999"))
    assert resp.get_json() == {"ok": True, "matched": 0, "failed": 0}
    assert db.query_one("SELECT status FROM outbox WHERE id=?", (oid,))["status"] == "sent"


def test_other_event_kinds_are_acknowledged_and_ignored(cfg, client):
    # Event 2 is "Incoming Message" on the Event Webhook — the Global Webhook already
    # delivers those to /webhook/whatsapp; double-processing would double replies.
    resp = client.post("/webhook/events", json={"event_id": "2", "data": [{}]})
    assert resp.status_code == 200 and resp.get_json() == {"ignored": True}
