"""The closed loop: the fixer names their own deadline, the system holds them to it.

reason set -> owner is asked "how many hours?" -> their answer snoozes ALL chasing for
exactly that long -> machine still stopped at expiry counts a MISS and the cycle
restarts. The misses are what the accountability report is built from, so every test
here is really a test of whether "biggest defaulter" can be trusted.
"""
from datetime import timedelta

from app import clock, db
from app.core import escalation, incidents, prompts, ticker


def _open_with_reason(cfg, ref="loom_7"):
    inc = incidents.open_incident(cfg, ref, "STOPPED")
    from app.core import classify
    classify.on_open(cfg, inc)
    res = incidents.set_reason(cfg, inc["id"], "weaving.electrical",
                               method="reply", actor="test")
    return inc, res["ticket"]


# --- the question ---------------------------------------------------------------

def test_opening_a_ticket_asks_for_the_estimate_not_a_bare_page(cfg):
    inc, tkt = _open_with_reason(cfg)
    ticker.tick(cfg)          # rung 0 due immediately
    row = db.query_one("SELECT payload FROM outbox ORDER BY id DESC LIMIT 1")
    import json
    payload = json.loads(row["payload"])
    assert payload["type"] == "eta_request"
    assert "hours" in payload["text"].lower()


def test_parse_eta_accepts_hours_and_nothing_else():
    assert prompts.parse_eta("3") == 3
    assert prompts.parse_eta(" 1 ") == 1
    assert prompts.parse_eta("0.5") == 1, "under an hour floors to 1, as the prompt says"
    assert prompts.parse_eta("24") == 24
    assert prompts.parse_eta("25") is None, "beyond the cap is parking, not an estimate"
    assert prompts.parse_eta("2 hours") is None, "shares an inbox with the reason menu"
    assert prompts.parse_eta("") is None


# --- the snooze -----------------------------------------------------------------

def test_an_estimate_silences_every_pending_rung_for_exactly_that_long(cfg):
    inc, tkt = _open_with_reason(cfg)
    ticker.tick(cfg)

    res = escalation.set_eta(cfg, tkt["id"], 3, actor="shailendra")
    assert res["ok"]

    pending = db.query(
        "SELECT action, due_at FROM escalations WHERE ticket_id=? AND status='pending'",
        (tkt["id"],))
    assert len(pending) == 1, "everything else is cancelled — that is the bargain"
    assert pending[0]["action"] == "eta_check"

    # Two hours in: still inside the promise, the ticker must stay silent.
    clock.CLOCK.set_virtual(clock.now() + timedelta(hours=2))
    before = db.query_one("SELECT COUNT(*) n FROM outbox")["n"]
    ticker.tick(cfg)
    assert db.query_one("SELECT COUNT(*) n FROM outbox")["n"] == before, \
        "chasing during the snooze breaks the deal that makes people answer at all"


def test_a_resumed_machine_ends_the_cycle_with_no_miss(cfg):
    inc, tkt = _open_with_reason(cfg)
    ticker.tick(cfg)
    escalation.set_eta(cfg, tkt["id"], 2, actor="shailendra")

    incidents.begin_resolve(cfg, inc["id"])
    incidents.commit_resolve(cfg, inc["id"])
    clock.CLOCK.set_virtual(clock.now() + timedelta(hours=3))
    ticker.tick(cfg)

    t = db.query_one("SELECT eta_misses FROM tickets WHERE id=?", (tkt["id"],))
    assert t["eta_misses"] == 0, "the machine came back inside the estimate — no miss"


# --- the miss -------------------------------------------------------------------

def test_a_lapsed_estimate_counts_a_miss_and_asks_again(cfg):
    inc, tkt = _open_with_reason(cfg)
    ticker.tick(cfg)
    escalation.set_eta(cfg, tkt["id"], 2, actor="shailendra")

    clock.CLOCK.set_virtual(clock.now() + timedelta(hours=2, minutes=5))
    ticker.tick(cfg)

    t = db.query_one("SELECT eta_misses FROM tickets WHERE id=?", (tkt["id"],))
    assert t["eta_misses"] == 1, "the promise lapsed with the machine still stopped"

    import json
    row = db.query_one("SELECT payload FROM outbox ORDER BY id DESC LIMIT 1")
    payload = json.loads(row["payload"])
    assert payload["type"] == "eta_request"
    assert "still" in payload["text"].lower(), \
        "the re-ask must say the estimate lapsed — that plain sentence IS the enforcement"

    ev = db.query("SELECT kind FROM events WHERE entity='ticket' AND entity_id=?",
                  (tkt["id"],))
    assert "eta_missed" in [e["kind"] for e in ev]


def test_misses_accumulate_across_repeated_lapses(cfg):
    inc, tkt = _open_with_reason(cfg)
    ticker.tick(cfg)
    for expected in (1, 2):
        escalation.set_eta(cfg, tkt["id"], 1, actor="shailendra")
        clock.CLOCK.set_virtual(clock.now() + timedelta(hours=1, minutes=2))
        ticker.tick(cfg)
        t = db.query_one("SELECT eta_misses FROM tickets WHERE id=?", (tkt["id"],))
        assert t["eta_misses"] == expected


# --- the report -----------------------------------------------------------------

def test_the_report_names_the_defaulter_from_recorded_promises(cfg, monkeypatch):
    monkeypatch.setenv("OPS_ADMIN_API_KEY", "k")
    inc, tkt = _open_with_reason(cfg)
    ticker.tick(cfg)
    escalation.set_eta(cfg, tkt["id"], 1, actor="shailendra")
    clock.CLOCK.set_virtual(clock.now() + timedelta(hours=1, minutes=2))
    ticker.tick(cfg)

    from app.main import create_app
    app = create_app(cfg=cfg, start_workers=False)
    r = app.test_client().get("/api/admin/report",
                              headers={"X-Admin-Key": "k"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["defaulters"], "one promise was made and broken — the report must show it"
    worst = d["defaulters"][0]
    assert worst["person"] == "Shailendra"
    assert worst["misses"] == 1
    assert d["downtime_by_machine"] is not None


# --- the reply path, end to end through the webhook ------------------------------

def test_a_pickyassist_number_reply_lands_as_an_estimate(cfg, monkeypatch):
    """The full loop as it happens on a phone: reason set, estimate question sent, the
    person types "3" into WhatsApp, PickyAssist posts it to the webhook — and that bare
    number must land as THEIR estimate on THEIR ticket, not as a reason for something
    else. Both questions are numeric; the router keys on which question was asked last."""
    from app.core import routing
    from app.main import create_app

    inc, tkt = _open_with_reason(cfg)
    ticker.tick(cfg)
    from app.core import outbox as outbox_mod
    outbox_mod.drain(cfg)                    # the eta question reaches the outbox log

    sent_to = db.query_one("SELECT recipient FROM outbox ORDER BY id DESC LIMIT 1")
    replier = sent_to["recipient"].lstrip("+").replace(" ", "")

    app = create_app(cfg=cfg, start_workers=False)
    resp = app.test_client().post("/webhook/whatsapp", json={
        "number": replier, "message-in": "3", "message_in_raw": "3",
        "direction": 0, "unique-id": "t-1"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["matched"] is True and data["kind"] == "eta" and data["hours"] == 3

    t = db.query_one("SELECT eta_hours, eta_by FROM tickets WHERE id=?", (tkt["id"],))
    assert t["eta_hours"] == 3
    assert t["eta_by"] == "shailendra", "the promise carries the person's name"

    pending = db.query(
        "SELECT action FROM escalations WHERE ticket_id=? AND status='pending'",
        (tkt["id"],))
    assert [p["action"] for p in pending] == ["eta_check"], "everything else snoozed"
