"""Auto-classification: the min-duration gate, fleet power, and boundary changeover."""

from datetime import datetime, timezone

from app import clock, db
from app.core import classify, incidents, ticker
from app.sources.base import IncidentOpened
from app.core import poller


def _reason(incident_id):
    r = db.query_one("SELECT code FROM incident_reasons WHERE incident_id=? ORDER BY id DESC LIMIT 1",
                     (incident_id,))
    return r["code"] if r else None


def test_short_stop_auto_coded(cfg):
    inc = incidents.open_incident(cfg, "loom_1", "STOPPED")
    classify.on_open(cfg, inc)
    # resumes after 60s (< 120s min-duration)
    clock.CLOCK.advance(60)
    incidents.begin_resolve(cfg, inc["id"])
    clock.CLOCK.advance(60)  # past 45s grace
    ticker.tick(cfg)
    assert _reason(inc["id"]) == "short_stop"
    # record-only: no ticket
    assert db.query_one("SELECT COUNT(*) n FROM tickets WHERE incident_id=?", (inc["id"],))["n"] == 0
    assert db.query_one("SELECT status FROM incidents WHERE id=?", (inc["id"],))["status"] == "resolved"


def test_fleet_stop_is_power_failure(cfg):
    # eight looms stop in one poll (same instant) -> power_failure for all
    events = [IncidentOpened(asset_ref=f"loom_{i}", condition="STOPPED", at=clock.now_iso())
              for i in range(1, 9)]
    poller.apply_events(cfg, events)
    codes = {_reason(r["id"]) for r in db.query("SELECT id FROM incidents")}
    assert codes == {"power_failure"}
    # power is non-ticketable -> no tickets, no reason prompts scheduled
    assert db.query_one("SELECT COUNT(*) n FROM tickets")["n"] == 0
    assert db.query_one("SELECT COUNT(*) n FROM escalations WHERE action='ask_reason'")["n"] == 0


def test_within_shift_boundary_helper(cfg):
    at_boundary = datetime(2026, 8, 5, 0, 35, 0, tzinfo=timezone.utc)   # 06:05 IST
    away = datetime(2026, 8, 5, 4, 0, 0, tzinfo=timezone.utc)           # 09:30 IST
    assert clock.within_shift_boundary(at_boundary, cfg.shifts, 20) is True
    assert clock.within_shift_boundary(away, cfg.shifts, 20) is False


def test_fleet_stop_at_boundary_is_changeover(cfg):
    # 06:00 IST boundary; looms stop spread over ~2 min (NOT within 5s of each other),
    # so the tight power rule does not fire but the boundary rule does.
    base = datetime(2026, 8, 5, 0, 30, 0, tzinfo=timezone.utc)  # 06:00 IST
    last = None
    for i in range(7):
        clock.CLOCK.set_virtual(base)
        clock.CLOCK.advance(i * 20)  # 0,20,40,... seconds apart
        last = incidents.open_incident(cfg, f"loom_{i+1}", "STOPPED")
    # classify the final incident directly: spread means <threshold within 5s, but
    # >=threshold within the 300s boundary window at a shift start.
    code = classify.classify_at_open(cfg, last)
    assert code == "shift_change"


# --- fleet threshold at the real (small) fleet size ----------------------------

def _register(cfg, refs):
    from app.core import incidents
    for ref in refs:
        with db.transaction() as c:
            incidents.ensure_asset(c, cfg, ref, ref)


def _stop_together(cfg, refs):
    """One poll cycle: the poller stamps every event in a batch with a single now_iso,
    so these are simultaneous by construction — not merely 'within 5 seconds'."""
    from app.core import poller
    from app.sources.base import IncidentOpened
    at = clock.now_iso()
    poller.apply_events(cfg, [IncidentOpened(asset_ref=r, condition="STOPPED", at=at)
                              for r in refs])


def _codes(cfg):
    return {r["asset_ref"]: r["code"] for r in db.query(
        "SELECT a.asset_ref, r.code FROM incidents i JOIN assets a ON a.id=i.asset_id"
        " LEFT JOIN incident_reasons r ON r.incident_id=i.id")}


FLEET = ["loom_91", "loom_92", "loom_93", "loom_94"]   # the machines actually on the API


def test_two_of_four_stopping_is_not_a_power_failure(cfg):
    """The dangerous direction. With fleet_fraction 0.5 the threshold on a 4-machine
    fleet collapses to 2, so two ordinary coincident stops were auto-coded
    power_failure — which is not ticketable, so both faults vanished silently."""
    _register(cfg, FLEET)
    _stop_together(cfg, ["loom_91", "loom_93"])
    codes = _codes(cfg)
    assert codes["loom_91"] is None and codes["loom_93"] is None, \
        f"two of four must NOT auto-classify as fleet-wide, got {codes}"
    pending = db.query_one("SELECT COUNT(*) n FROM escalations WHERE status='pending'")["n"]
    assert pending == 2, "both stops must reach the unknown ladder so someone is asked"


def test_all_four_stopping_is_still_a_power_failure(cfg):
    """The safe direction must keep working: a real power cut takes the whole shed."""
    _register(cfg, FLEET)
    _stop_together(cfg, FLEET)
    assert set(_codes(cfg).values()) == {"power_failure"}
    pending = db.query_one("SELECT COUNT(*) n FROM escalations WHERE status='pending'")["n"]
    assert pending == 0, "a power failure pages nobody by design"
