"""The three things a person needs to be able to say, and could not.

Audited from the phone's side: naming which loom you mean, revising an estimate you
have already given, and saying the job is not yours. Every one of them used to be
either silently discarded or impossible to express.
"""
import datetime
import json

import pytest

from app import clock, db
from app.core import classify, escalation, incidents, outbox, prompts, ticker
from app.main import create_app


@pytest.fixture()
def client(cfg):
    return create_app(cfg=cfg, start_workers=False).test_client()


def _stopped(cfg, asset_ref, minutes_ago):
    with db.transaction() as c:
        incidents.ensure_asset(c, cfg, asset_ref)
    inc = incidents.open_incident(cfg, asset_ref, "STOPPED",
                                  at=clock.plus_seconds(-minutes_ago * 60))
    classify.on_open(cfg, inc)
    return inc["id"]


def _drive(cfg, ticks=2):
    for _ in range(ticks):
        ticker.tick(cfg)
        outbox.drain(cfg)


def _who():
    r = db.query_one("SELECT recipient FROM outbox WHERE channel='whatsapp'"
                     " ORDER BY id DESC LIMIT 1")
    return r["recipient"].lstrip("+").replace(" ", "")


def _reply(client, text, sender):
    return client.post("/webhook/whatsapp", json={
        "number": sender, "message-in": text, "message_in_raw": text,
        "direction": 0, "unique-id": f"u{clock.now_iso()}{text}"}).get_json()


def _reason_of(incident_id):
    r = db.query_one("SELECT code FROM incident_reasons WHERE incident_id=?",
                     (incident_id,))
    return r["code"] if r else None


# --- naming the loom -------------------------------------------------------------

def test_the_loom_number_in_the_reply_decides_which_loom(cfg, client):
    """Two looms stopped, two identical questions on one screen. Answering the older one
    used to record the fault against the newer."""
    first = _stopped(cfg, "loom_91", 40)
    second = _stopped(cfg, "loom_92", 35)
    _drive(cfg)
    who = _who()

    out = _reply(client, "91 1", who)
    assert out["matched"] is True and out["incident_id"] == first
    assert _reason_of(first) == "weaving.electrical"
    assert _reason_of(second) is None


def test_the_prompt_tells_them_to_write_the_loom_number(cfg):
    text = prompts.render(cfg, "loom_91", clock.plus_seconds(-1200), 25)
    assert "91 1" in text, "an instruction nobody is given is an instruction nobody follows"


def test_a_bare_number_still_works(cfg, client):
    first = _stopped(cfg, "loom_91", 40)
    _drive(cfg)
    assert _reply(client, "1", _who())["matched"] is True
    assert _reason_of(first) == "weaving.electrical"


def test_loom_ninety_one_written_out_in_words_also_parses(cfg, client):
    first = _stopped(cfg, "loom_91", 40)
    _drive(cfg)
    assert _reply(client, "loom 91 2", _who())["matched"] is True
    assert _reason_of(first) == "weaving.mechanical"


# --- revising an estimate --------------------------------------------------------

def test_a_fitter_can_revise_an_estimate_he_has_already_given(cfg, client):
    """He says 2, opens the machine, finds it worse, sends 6. That used to be discarded
    in silence and he was marked a defaulter four hours later."""
    inc = _stopped(cfg, "loom_91", 30)
    incidents.set_reason(cfg, inc, "weaving.mechanical", method="reply", actor="akshaan")
    _drive(cfg)
    who = _who()

    assert _reply(client, "2", who)["hours"] == 2
    out = _reply(client, "6", who)
    assert out["matched"] is True and out["hours"] == 6

    t = db.query_one("SELECT eta_hours, eta_misses FROM tickets WHERE incident_id=?",
                     (inc,))
    assert t["eta_hours"] == 6
    assert t["eta_misses"] == 0, "revising honestly is not a missed promise"


def test_the_revision_is_acknowledged_as_a_revision(cfg, client):
    inc = _stopped(cfg, "loom_91", 30)
    incidents.set_reason(cfg, inc, "weaving.mechanical", method="reply", actor="akshaan")
    _drive(cfg)
    who = _who()
    _reply(client, "2", who)
    _reply(client, "6", who)

    acks = [json.loads(r["payload"])["text"] for r in db.query(
        "SELECT payload FROM outbox WHERE payload LIKE ? ORDER BY id", ('%"ack"%',))]
    assert "Updated" in acks[-1] and "6 hours" in acks[-1]


# --- handing it over -------------------------------------------------------------

def test_pass_brings_the_next_step_forward_instead_of_waiting(cfg, client):
    inc = _stopped(cfg, "loom_91", 30)
    incidents.set_reason(cfg, inc, "weaving.mechanical", method="reply", actor="akshaan")
    _drive(cfg)
    who = _who()

    later = db.query_one("SELECT COUNT(*) n FROM escalations WHERE status='pending'"
                         " AND due_at>?", (clock.now_iso(),))["n"]
    assert later, "there should be a future rung to bring forward"

    out = _reply(client, "PASS", who)
    assert out["matched"] is True and out["kind"] == "pass"

    still_later = db.query_one("SELECT COUNT(*) n FROM escalations WHERE status='pending'"
                               " AND due_at>?", (clock.now_iso(),))["n"]
    assert still_later == 0, "everything pending should now be due"

    acks = [json.loads(r["payload"])["text"] for r in db.query(
        "SELECT payload FROM outbox WHERE payload LIKE ? ORDER BY id", ('%"ack"%',))]
    assert "Passed on" in acks[-1]


def test_pass_with_nothing_pending_says_so_rather_than_going_quiet(cfg, client):
    _stopped(cfg, "loom_91", 40)
    _drive(cfg)
    who = _who()
    _reply(client, "1", who)          # answered; nothing is waiting on them now

    out = _reply(client, "pass", who)
    acks = [json.loads(r["payload"])["text"] for r in db.query(
        "SELECT payload FROM outbox WHERE payload LIKE ? ORDER BY id", ('%"ack"%',))]
    assert out["matched"] is False
    assert "Nothing is waiting on you" in acks[-1]


def test_the_estimate_question_offers_pass(cfg):
    text = prompts.render_eta(cfg, "loom_91", "Machine fault")
    assert "PASS" in text and "new number any time" in text
