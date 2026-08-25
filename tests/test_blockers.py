"""Regression tests for the defects an adversarial audit found.

Every one of these was a path where the system reported success and did nothing. The
suite passed through all of them, which is the point: each test here exists because its
absence let a silent failure ship.
"""
from datetime import timedelta

import pytest

from app import clock, config, db
from app.core import escalation, incidents, offplan, ticker


# --- deletions actually delete -------------------------------------------------

def test_compose_patch_keeps_the_tombstone_merge_patch_executes():
    """merge_patch APPLIES a patch (null = delete now); compose_patch COMBINES two
    patches (null = an instruction that must survive). Using the first for the second is
    why every deletion through the config API silently did nothing."""
    stored = {}
    delete = {"people": {"store_desk": None}}

    assert config.merge_patch(stored, delete) == {"people": {}}, \
        "merge_patch executes the null immediately — correct when applying, wrong here"
    assert config.compose_patch(stored, delete) == {"people": {"store_desk": None}}, \
        "compose_patch must preserve the tombstone so it can be executed later"

    base = {"people": {"store_desk": {"name": "Yarn store"}, "ravi_k": {"name": "Ravi"}}}
    applied = config.merge_patch(base, config.compose_patch(stored, delete))
    assert "store_desk" not in applied["people"]
    assert "ravi_k" in applied["people"]


def test_clearing_placeholder_survives_composition(cfg):
    """The go-live gate depends on this. Replacing a sample contact sets
    placeholder:None; if that null is dropped, validate() refuses live mode forever and
    the admin UI cannot do the one job it exists for."""
    composed = config.compose_patch(
        {}, {"people": {"ravi_k": {"whatsapp": "+919812345678", "placeholder": None}}})
    person = config.merge_patch(cfg.routing, composed)["people"]["ravi_k"]
    assert person["whatsapp"] == "+919812345678"
    assert "placeholder" not in person, "the placeholder flag must actually clear"


# --- a split roster replaces the 24/7 bucket -----------------------------------

def test_splitting_an_all_shift_team_replaces_it(cfg):
    """roles.<team> is a dict, so merge_patch recurses and only the per-shift lists
    replace — a leftover `all` survives, and role_person_ids checks `all` FIRST. Splitting
    a 24/7 team into named shifts returned 200, echoed the new roster, and changed
    nothing about who was paged."""
    assert "all" in cfg.roles["shift_incharge"], "fixture assumption"

    naive = config.merge_patch(cfg.routing, {"roles": {"shift_incharge": {
        "A": ["amarjit_s"], "B": ["gurmeet_s"], "C": ["balwinder_s"]}}})
    assert "all" in naive["roles"]["shift_incharge"], \
        "without a tombstone the 24/7 bucket survives — this is the bug"

    with_tombstone = config.merge_patch(cfg.routing, {"roles": {"shift_incharge": {
        "A": ["amarjit_s"], "B": ["gurmeet_s"], "C": ["balwinder_s"], "all": None}}})
    spec = with_tombstone["roles"]["shift_incharge"]
    assert "all" not in spec
    assert spec["B"] == ["gurmeet_s"]


# --- nobody may lose their only contact channel --------------------------------

def test_a_person_with_no_channel_is_refused(cfg):
    """A person with no channel still resolves to a Recipient, so the default_owner
    backstop never fires — but their channel is 'log'. Pages go to a file, the outbox
    says sent, the event log says notified."""
    broken = config.candidate(cfg, {"routing": {"people": {"ravi_k": {"whatsapp": None}}}})
    with pytest.raises(config.ConfigError) as e:
        config.validate(broken)
    assert "ravi_k" in str(e.value)


def test_default_owner_must_exist_and_be_reachable(cfg):
    bad = config.candidate(cfg, {"routing": {"default_owner": "nobody_at_all"}})
    with pytest.raises(config.ConfigError) as e:
        config.validate(bad)
    assert "default_owner" in str(e.value)


def test_a_team_may_not_be_called_owner(cfg):
    """`owner` is reserved — ladders use it to mean the reason's own team, so a real
    team by that name captures every rung in every ladder."""
    bad = config.candidate(cfg, {"routing": {"roles": {"owner": {"all": ["ravi_k"]}}}})
    with pytest.raises(config.ConfigError) as e:
        config.validate(bad)
    assert "reserved" in str(e.value)


# --- one bad rung must not wedge the plant -------------------------------------

def test_a_rung_that_cannot_fire_is_parked_not_left_blocking(cfg, monkeypatch):
    """The due query is ORDER BY due_at. A row that raises rolls back still pending with
    a past due_at, so it is first again on every subsequent tick — forever. One bad rung
    stopped every escalation in the plant with no symptom but a frozen heartbeat."""
    inc = incidents.open_incident(cfg, "loom_7", "STOPPED")
    from app.core import classify
    classify.on_open(cfg, inc)
    clock.CLOCK.set_virtual(clock.now() + timedelta(hours=2))

    boom = {"n": 0}

    def explode(*a, **k):
        boom["n"] += 1
        raise OverflowError("date value out of range")

    monkeypatch.setattr(escalation, "fire", explode)
    out = ticker.tick(cfg)
    assert out["wedged"] >= 1, "the bad rung must be parked"

    monkeypatch.undo()
    pending = db.query_one(
        "SELECT COUNT(*) n FROM escalations WHERE status='pending' AND due_at<=?",
        (clock.now_iso(),))
    assert pending["n"] == 0, "nothing overdue may still be blocking the queue"


# --- off plan ------------------------------------------------------------------

def test_an_offplan_loom_opens_no_incident(cfg):
    """The feed cannot tell 'no order' from 'broken' — both are a powered machine at
    rpm 0. Paging about a deliberately idle one is how people learn to ignore the system."""
    from app.core import poller
    from app.sources.base import IncidentOpened

    with db.transaction() as c:
        aid = incidents.ensure_asset(c, cfg, "loom_7")
        offplan.set_offplan(c, aid, "no_order",
                            until_at=clock.plus_seconds(3600), actor="test")

    out = poller.apply_events(cfg, [IncidentOpened(asset_ref="loom_7", condition="STOPPED",
                                                   at=clock.now_iso())])
    assert out["skipped_offplan"] == 1
    assert out["opened"] == 0
    assert db.query_one("SELECT COUNT(*) n FROM incidents WHERE asset_id=?", (aid,))["n"] == 0


def test_offplan_expires_on_its_own(cfg):
    """until_at is mandatory precisely so a forgotten flag cannot hide a real fault."""
    with db.transaction() as c:
        aid = incidents.ensure_asset(c, cfg, "loom_7")
        offplan.set_offplan(c, aid, "maintenance",
                            until_at=clock.plus_seconds(600), actor="test")
    assert offplan.active(aid) is not None
    clock.CLOCK.set_virtual(clock.now() + timedelta(minutes=20))
    assert offplan.active(aid) is None, "an expired flag must stop suppressing faults"


# --- being told once is not being told -----------------------------------------

def test_the_last_rung_repeats_while_the_asset_is_still_stopped(cfg):
    """Ladders used to run out. Somebody who missed the message — phones are not always
    looked at over machine noise — was never told again, so a nine-hour fault was as
    quiet as a forty-minute one."""
    ladder = cfg.unknown_ladder
    assert ladder[-1].get("repeat_every_minutes"), "fixture assumption"

    inc = incidents.open_incident(cfg, "loom_7", "STOPPED")
    from app.core import classify
    classify.on_open(cfg, inc)

    for rung in ladder:
        clock.CLOCK.set_virtual(
            clock.parse(inc["opened_at"]) + timedelta(minutes=rung["after_minutes"] + 1))
        ticker.tick(cfg)

    still_pending = db.query_one(
        "SELECT COUNT(*) n FROM escalations WHERE incident_id=? AND status='pending'",
        (inc["id"],))
    assert still_pending["n"] == 1, \
        "past the last rung there must still be a future ask scheduled"


# --- an unrouted page is not a page --------------------------------------------

def test_unrouted_is_not_recorded_as_notified(cfg, monkeypatch):
    """A routing failure laundered into a delivery record, in the one table whose whole
    premise is that it is the record of what happened."""
    from app.core import models, routing

    inc = incidents.open_incident(cfg, "loom_7", "STOPPED")
    from app.core import classify
    classify.on_open(cfg, inc)
    monkeypatch.setattr(routing, "resolve", lambda *a, **k: [])
    clock.CLOCK.set_virtual(clock.now() + timedelta(hours=2))
    ticker.tick(cfg)

    kinds = [r["kind"] for r in db.query(
        "SELECT kind FROM events WHERE entity='incident' AND entity_id=?", (inc["id"],))]
    assert models.K_UNROUTED in kinds
    assert models.K_NOTIFIED not in kinds, "nobody was called; it must not say notified"
