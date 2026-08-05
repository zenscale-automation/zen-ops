"""Full incident lifecycle, the false-restart guard, and the inbound webhook."""

import hashlib
import hmac
import json

from app import clock, db
from app.core import events, incidents, ticker
from app.main import create_app


def test_reply_opens_ticket_then_resolves(cfg):
    inc = incidents.open_incident(cfg, "loom_5", "STOPPED")
    from app.core import classify
    classify.on_open(cfg, inc)
    res = incidents.set_reason(cfg, inc["id"], "weaving.electrical", method="reply", actor="amarjit_s")
    tid = res["ticket"]["id"]

    ticker.tick(cfg)  # page the owner
    # resume + grace -> resolve
    clock.CLOCK.advance(30 * 60)
    incidents.begin_resolve(cfg, inc["id"])
    clock.CLOCK.advance(60)
    ticker.tick(cfg)

    incident = db.query_one("SELECT * FROM incidents WHERE id=?", (inc["id"],))
    ticket = db.query_one("SELECT * FROM tickets WHERE id=?", (tid,))
    assert incident["status"] == "resolved"
    assert incident["duration_s"] == 30 * 60
    assert ticket["status"] == "closed"
    assert ticket["close_reason"] == "asset_resumed"
    # the append-only log tells the whole story
    kinds = [e["kind"] for e in events.timeline("incident", inc["id"])]
    assert "opened" in kinds and "reason_set" in kinds and "resolved" in kinds
    # unknown-ladder prompt was cancelled by the reply (never fired)
    assert db.query_one(
        "SELECT COUNT(*) n FROM escalations WHERE incident_id=? AND status='fired'", (inc["id"],))["n"] == 0


def test_false_restart_within_grace_reopens(cfg):
    inc = incidents.open_incident(cfg, "loom_8", "STOPPED")
    incidents.set_reason(cfg, inc["id"], "weaving.mechanical", method="reply", actor="x")

    clock.CLOCK.advance(120)
    incidents.begin_resolve(cfg, inc["id"])          # machine ran again...
    assert db.query_one("SELECT status FROM incidents WHERE id=?", (inc["id"],))["status"] == "resolving"

    clock.CLOCK.advance(20)                           # ...but stops again inside the 45s grace
    incidents.open_incident(cfg, "loom_8", "STOPPED")  # poller sees STOPPED -> reopen
    row = db.query_one("SELECT status FROM incidents WHERE id=?", (inc["id"],))
    assert row["status"] == "open"
    tkt = db.query_one("SELECT reopen_count, status FROM tickets WHERE incident_id=?", (inc["id"],))
    assert tkt["reopen_count"] == 1 and tkt["status"] != "closed"


def test_webhook_signed_reply_sets_reason(cfg, monkeypatch):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SECRET", "test-secret")
    app = create_app(start_workers=False, cfg=cfg)
    client = app.test_client()

    inc = incidents.open_incident(cfg, "loom_5", "STOPPED")
    from app.core import classify
    classify.on_open(cfg, inc)

    body = json.dumps({"from": "+919000000005", "text": "1", "asset_ref": "loom_5"}).encode()
    sig = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    resp = client.post("/webhook/whatsapp", data=body,
                       headers={"Content-Type": "application/json", "X-Signature": sig})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["matched"] is True and data["code"] == "weaving.electrical"
    # a ticket now exists for the electrician
    tkt = db.query_one("SELECT owner_role FROM tickets WHERE incident_id=?", (inc["id"],))
    assert tkt["owner_role"] == "electrician"
    # verbatim inbound was recorded and linked
    raw = db.query_one("SELECT matched_incident_id FROM inbound_raw ORDER BY id DESC LIMIT 1")
    assert raw["matched_incident_id"] == inc["id"]


def test_webhook_bad_signature_rejected(cfg, monkeypatch):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SECRET", "test-secret")
    app = create_app(start_workers=False, cfg=cfg)
    client = app.test_client()
    resp = client.post("/webhook/whatsapp", json={"from": "x", "text": "1", "asset_ref": "loom_5"})
    assert resp.status_code == 401
