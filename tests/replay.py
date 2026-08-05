"""Replay harness: run the poller + ticker + outbox against a recorded shift at
virtual-clock speed, so a full slice replays in seconds. This is how the timer logic is
exercised without waiting out real hours (design doc section 10).

Run: python -m tests.replay            (uses tests/fixtures/recorded_shift.json)
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app import clock, config, db
from app.core import outbox, poller, ticker
from app.sources.fixture import FixtureLoomSource

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "recorded_shift.json"


def run_replay(cfg, fixture_path=FIXTURE, step_seconds=30, tail_minutes=20,
               fresh=True) -> dict:
    if fresh:
        db.reset_all()
        db.migrate()

    source = FixtureLoomSource.from_file(cfg, fixture_path)
    start = clock.parse(source.first_snapshot_at)
    end = clock.parse(source.last_snapshot_at) + timedelta(minutes=tail_minutes)

    clock.CLOCK.set_virtual(start)
    poller.ensure_discovered_assets(cfg, source)
    source.seed()

    steps = 0
    t = start
    while t <= end:
        clock.CLOCK.set_virtual(t)
        poller.poll_once(cfg, source)
        ticker.tick(cfg)
        outbox.drain(cfg)
        steps += 1
        t += timedelta(seconds=step_seconds)

    clock.CLOCK.real()
    return summarize(steps)


def summarize(steps: int) -> dict:
    def scalar(sql, params=()):
        r = db.query_one(sql, params)
        return list(r.values())[0] if r else 0

    incidents_by_status = {
        r["status"]: r["n"]
        for r in db.query("SELECT status, COUNT(*) n FROM incidents GROUP BY status")
    }
    reasons = {
        r["code"]: r["n"]
        for r in db.query("SELECT code, COUNT(*) n FROM incident_reasons GROUP BY code")
    }
    return {
        "steps": steps,
        "incidents": scalar("SELECT COUNT(*) n FROM incidents"),
        "incidents_by_status": incidents_by_status,
        "reasons": reasons,
        "tickets": scalar("SELECT COUNT(*) n FROM tickets"),
        "escalations_fired": scalar("SELECT COUNT(*) n FROM escalations WHERE status='fired'"),
        "outbox_sent": scalar("SELECT COUNT(*) n FROM outbox WHERE status='sent'"),
        "events": scalar("SELECT COUNT(*) n FROM events"),
    }


if __name__ == "__main__":
    cfg = config.load()
    db.init(cfg.db_params(), cfg.table_prefix)
    result = run_replay(cfg)
    print("REPLAY SUMMARY")
    for k, v in result.items():
        print(f"  {k}: {v}")
