"""Full incident lifecycle, the false-restart guard, and the inbound webhook."""

import hashlib
import hmac
import json
from datetime import timedelta

from app import clock, db
from app.core import events, incidents, ticker
from app.main import create_app


def test_reply_opens_ticket_then_resolves(cfg):
    inc = incidents.open_incident(cfg, "loom_5", "STOPPED")
    from app.core import classify
    classify.on_open(cfg, inc)
    res = incidents.set_reason(cfg, inc["id"], "weaving.electrical", method="reply", actor="test")
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


def _meta_envelope(frm, text, context_id=None):
    """Exactly the shape Meta POSTs: entry -> changes -> value -> messages."""
    msg = {"from": frm, "id": "wamid.TEST", "timestamp": "1754300000",
           "type": "text", "text": {"body": text}}
    if context_id:
        msg["context"] = {"id": context_id}
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": "PNID"},
                "contacts": [{"wa_id": frm, "profile": {"name": "Supervisor"}}],
                "messages": [msg]}}]}]}


def _meta_post(client, payload, secret=b"test-secret"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return client.post("/webhook/whatsapp", data=body,
                       headers={"Content-Type": "application/json",
                                "X-Hub-Signature-256": sig})


def test_webhook_signed_reply_sets_reason(cfg, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "test-secret")
    app = create_app(start_workers=False, cfg=cfg)
    client = app.test_client()

    inc = incidents.open_incident(cfg, "loom_5", "STOPPED")
    from app.core import classify, outbox, ticker
    classify.on_open(cfg, inc)

    # The real Meta payload carries no asset_ref — the old BSP placeholder invented one.
    # So the prompt must actually have been SENT for the reply to have something to
    # match against. Advance past prompt_after_minutes, fire the ladder, drain the
    # outbox, and only then reply, exactly as it happens on the floor.
    # Derived from config, not hardcoded: these timings are meant to be tuned against
    # real data, and a test that pins them makes tuning look like a regression.
    first_ask = next(r["after_minutes"] for r in cfg.unknown_ladder
                     if r.get("action") == "ask_reason")
    clock.CLOCK.set_virtual(clock.now() + timedelta(minutes=first_ask + 1))
    ticker.tick(cfg)
    outbox.drain(cfg)
    sent_to = db.query_one("SELECT recipient FROM outbox ORDER BY id DESC LIMIT 1")
    # Derive from config: the prompt goes to whoever the unknown ladder's first rung
    # resolves to today, not to a number pinned from a roster since replaced.
    from app.core import routing
    ask_role = cfg.unknown_ladder[0]["notify"]
    expected = {r.address for r in routing.resolve(cfg, ask_role, for_prompt=True)}
    assert sent_to["recipient"] in expected, "prompt went to the on-duty asker"

    # Meta reports `from` without a '+'; routing.yaml stores it with one.
    resp = _meta_post(client, _meta_envelope("919000000005", "1"))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["matched"] is True and data["code"] == "weaving.electrical"
    # a ticket now exists, owned by whichever team the config assigns
    tkt = db.query_one("SELECT owner_role FROM tickets WHERE incident_id=?", (inc["id"],))
    assert tkt["owner_role"] == cfg.owner_role("weaving.electrical")
    # verbatim inbound was recorded and linked
    raw = db.query_one("SELECT matched_incident_id FROM inbound_raw ORDER BY id DESC LIMIT 1")
    assert raw["matched_incident_id"] == inc["id"]


def test_webhook_bad_signature_rejected(cfg, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "test-secret")
    app = create_app(start_workers=False, cfg=cfg)
    client = app.test_client()
    resp = _meta_post(client, _meta_envelope("919000000005", "1"), secret=b"wrong-secret")
    assert resp.status_code == 401


def test_delivery_receipts_are_acknowledged_not_treated_as_replies(cfg, monkeypatch):
    """Meta posts `statuses` for every sent/delivered/read event. A non-200 makes it
    retry and eventually disable the subscription, so these must be accepted quietly."""
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "test-secret")
    app = create_app(start_workers=False, cfg=cfg)
    client = app.test_client()
    status_only = {"object": "whatsapp_business_account", "entry": [{"id": "WABA",
        "changes": [{"field": "messages", "value": {"messaging_product": "whatsapp",
            "statuses": [{"id": "wamid.X", "status": "delivered",
                          "recipient_id": "919000000005"}]}}]}]}
    resp = _meta_post(client, status_only)
    assert resp.status_code == 200 and resp.get_json()["ignored"] is True
    assert db.query_one("SELECT COUNT(*) n FROM inbound_raw")["n"] == 0


def test_subscription_handshake_echoes_the_challenge(cfg, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "tok-123")
    client = create_app(start_workers=False, cfg=cfg).test_client()
    ok = client.get("/webhook/whatsapp?hub.mode=subscribe"
                    "&hub.verify_token=tok-123&hub.challenge=abc987")
    assert ok.status_code == 200 and ok.get_data(as_text=True) == "abc987"
    bad = client.get("/webhook/whatsapp?hub.mode=subscribe"
                     "&hub.verify_token=wrong&hub.challenge=abc987")
    assert bad.status_code == 403
