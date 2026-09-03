"""The board of what is stopped right now, and acting on it from a desk.

Everything here answers a question somebody has to walk the shed to answer today: what
is down, for how long, does anyone know why, who is on the hook, did they promise a
time, and has this loom done this before. Plus the four things that could only be done
by curl, or not at all — record a reason given out loud, hand one fault to one person,
send the next message now, and retire a machine.
"""
import datetime
import json

import pytest

from app import clock, db
from app.core import classify, escalation, incidents, outbox, ticker


def _client(cfg, monkeypatch):
    monkeypatch.setenv("OPS_ADMIN_API_KEY", "k")
    from app.main import create_app
    return create_app(cfg=cfg, start_workers=False).test_client()


def _h():
    return {"X-Admin-Key": "k", "X-Admin-User": "test"}


def _stopped(cfg, asset_ref="loom_91", minutes_ago=40):
    with db.transaction() as c:
        incidents.ensure_asset(c, cfg, asset_ref)
    inc = incidents.open_incident(cfg, asset_ref, "STOPPED",
                                  at=clock.plus_seconds(-minutes_ago * 60))
    classify.on_open(cfg, inc)
    return inc["id"]


def _board(client):
    r = client.get("/api/admin/incidents", headers=_h())
    assert r.status_code == 200
    return r.get_json()["incidents"]


# --- what the board shows --------------------------------------------------------

def test_the_board_lists_what_is_stopped_longest_first(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    _stopped(cfg, "loom_91", 90)
    _stopped(cfg, "loom_92", 20)

    rows = _board(client)
    assert [r["asset_ref"] for r in rows] == ["loom_91", "loom_92"]
    assert rows[0]["minutes_down"] >= 89
    assert rows[0]["reason"] is None, "nobody has said why yet"
    assert rows[0]["owner"], "somebody must be named as being on the hook"


def test_a_promise_and_its_deadline_are_on_the_row(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    inc = _stopped(cfg, "loom_91", 30)
    incidents.set_reason(cfg, inc, "weaving.mechanical", method="reply", actor="akshaan")
    tkt = db.query_one("SELECT id FROM tickets WHERE incident_id=?", (inc,))
    escalation.set_eta(cfg, tkt["id"], 3, actor="akshaan")

    row = _board(client)[0]
    assert row["promise"]["hours"] == 3
    assert row["promise"]["overdue"] is False
    assert row["reason"]["label"] == "Machine fault"


def test_a_repeating_fault_is_flagged_as_repeating(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    for _ in range(3):
        i = _stopped(cfg, "loom_91", 30)
        incidents.set_reason(cfg, i, "weaving.electrical", method="reply", actor="akshaan")
        incidents.begin_resolve(cfg, i)
        incidents.commit_resolve(cfg, i)
    live = _stopped(cfg, "loom_91", 30)
    incidents.set_reason(cfg, live, "weaving.electrical", method="reply", actor="akshaan")

    row = _board(client)[0]
    assert row["recurrence"]["count"] >= 3
    assert row["recurrence"]["repeating"] is True, \
        "three of the same fault in a shift is one unfinished repair, not three faults"


def test_the_timeline_shows_what_was_asked_answered_and_whether_it_arrived(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    inc = _stopped(cfg, "loom_91", 40)
    ticker.tick(cfg)
    outbox.drain(cfg)
    db.execute("UPDATE outbox SET delivery_status='failed' WHERE channel='whatsapp'")

    r = client.get(f"/api/admin/incidents/{inc}/timeline", headers=_h())
    assert r.status_code == 200
    entries = r.get_json()["entries"]
    kinds = [e["kind"] for e in entries]
    assert "event" in kinds and "sent" in kinds
    asked = [e for e in entries if e["kind"] == "sent"][0]
    assert asked["delivery"] == "failed", \
        "'is he ignoring us' and 'was he ever asked' must be distinguishable"


# --- acting on it ----------------------------------------------------------------

def test_a_reason_given_out_loud_can_be_recorded_and_opens_the_ticket(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    inc = _stopped(cfg, "loom_91", 40)
    ticker.tick(cfg)

    r = client.post(f"/api/admin/incidents/{inc}/reason", headers=_h(),
                    json={"code": "weaving.electrical"})
    assert r.status_code == 200 and r.get_json()["ticket"]

    row = db.query_one("SELECT method FROM incident_reasons WHERE incident_id=?", (inc,))
    assert row["method"] == "panel", "recorded as told to a person, not as a reply"
    assert not db.query("SELECT id FROM escalations WHERE incident_id=?"
                        " AND status='pending'", (inc,)), \
        "and the re-asking stops, because the question has been answered"


def test_a_reason_is_not_overwritten_once_somebody_has_given_one(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    inc = _stopped(cfg, "loom_91", 40)
    incidents.set_reason(cfg, inc, "weaving.mechanical", method="reply", actor="akshaan")

    r = client.post(f"/api/admin/incidents/{inc}/reason", headers=_h(),
                    json={"code": "weaving.electrical"})
    assert r.status_code == 409


def test_one_fault_can_be_handed_to_one_person(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    inc = _stopped(cfg, "loom_91", 40)
    incidents.set_reason(cfg, inc, "weaving.mechanical", method="reply", actor="akshaan")
    tkt = db.query_one("SELECT id FROM tickets WHERE incident_id=?", (inc,))

    r = client.post(f"/api/admin/incidents/{inc}/assign", headers=_h(),
                    json={"person": "shailendra"})
    assert r.status_code == 200

    ticker.tick(cfg)
    outbox.drain(cfg)
    sent = db.query_one("SELECT recipient FROM outbox WHERE channel='whatsapp'"
                        " ORDER BY id DESC LIMIT 1")
    assert sent["recipient"] == cfg.people["shailendra"]["whatsapp"], \
        "the next message goes to the person named, not to whoever the rota says"

    # and it sticks for the rungs that follow
    assert db.query_one("SELECT owner_person FROM tickets WHERE id=?",
                        (tkt["id"],))["owner_person"] == "shailendra"


def test_assigning_to_nobody_hands_it_back_to_the_rota(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    inc = _stopped(cfg, "loom_91", 40)
    client.post(f"/api/admin/incidents/{inc}/assign", headers=_h(),
                json={"person": "shailendra"})
    r = client.post(f"/api/admin/incidents/{inc}/assign", headers=_h(), json={"person": ""})
    assert r.status_code == 200 and r.get_json()["assigned_to"] is None


def test_an_unknown_person_is_refused_rather_than_silently_ignored(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    inc = _stopped(cfg, "loom_91", 40)
    r = client.post(f"/api/admin/incidents/{inc}/assign", headers=_h(),
                    json={"person": "nobody_here"})
    assert r.status_code == 400


def test_send_now_brings_the_scheduled_message_forward(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    inc = _stopped(cfg, "loom_91", 5)          # nothing due for another 15 minutes
    before = len(db.query("SELECT id FROM outbox"))

    r = client.post(f"/api/admin/incidents/{inc}/remind", headers=_h())
    assert r.status_code == 200 and r.get_json()["brought_forward"] == 1

    ticker.tick(cfg)
    outbox.drain(cfg)
    assert len(db.query("SELECT id FROM outbox")) > before, \
        "it should go out on the next tick, not sit waiting for its timer"


def test_send_now_says_so_when_there_is_nothing_to_bring_forward(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    inc = _stopped(cfg, "loom_91", 40)
    db.execute("UPDATE escalations SET status='cancelled' WHERE incident_id=?", (inc,))
    r = client.post(f"/api/admin/incidents/{inc}/remind", headers=_h())
    assert r.status_code == 409


def test_decommissioning_retires_the_machine_and_closes_what_is_open(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    inc = _stopped(cfg, "loom_91", 40)
    incidents.set_reason(cfg, inc, "weaving.mechanical", method="reply", actor="akshaan")

    r = client.post("/api/admin/assets/loom_91/decommission", headers=_h(),
                    json={"confirm": True})
    assert r.status_code == 200 and r.get_json()["incidents_closed"] == 1

    assert db.query_one("SELECT active FROM assets WHERE asset_ref='loom_91'")["active"] == 0
    assert db.query_one("SELECT status FROM incidents WHERE id=?", (inc,))["status"] == "resolved"
    assert db.query_one("SELECT close_reason FROM tickets WHERE incident_id=?",
                        (inc,))["close_reason"] == "decommissioned"
    assert not db.query("SELECT id FROM escalations WHERE status='pending'"
                        " AND incident_id=?", (inc,))
    assert _board(client) == [], "a retired machine is off the board"


def test_decommissioning_needs_saying_so_explicitly(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    _stopped(cfg, "loom_91", 40)
    r = client.post("/api/admin/assets/loom_91/decommission", headers=_h(), json={})
    assert r.status_code == 400 and "confirm" in r.get_json()["error"]


def test_the_board_needs_the_key_like_every_other_read(cfg, monkeypatch):
    client = _client(cfg, monkeypatch)
    _stopped(cfg, "loom_91", 40)
    assert client.get("/api/admin/incidents").status_code == 403
    assert client.post("/api/admin/incidents/1/remind").status_code == 403
