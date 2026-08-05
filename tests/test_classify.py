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
