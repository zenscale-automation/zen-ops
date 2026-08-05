"""Poller orchestration: turn source events into core state changes.

Kept deliberately thin and free of any loom knowledge so it is testable against a
recorded shift. A newly-opened incident is classified exactly once (auto-code or start
the reason prompt ladder); a resolve enters the grace window on the matching open
incident.
"""

from __future__ import annotations

from .. import clock, config, db
from ..sources.base import IncidentOpened, IncidentResolved
from . import classify, incidents


def ensure_discovered_assets(cfg: "config.Config", source) -> int:
    """Register any assets the source can enumerate (source.yaml assets.discover)."""
    try:
        found = source.discover_assets()
    except Exception:
        return 0
    n = 0
    for a in found:
        with db.transaction() as c:
            incidents.ensure_asset(c, cfg, a["asset_ref"], a.get("label"))
        n += 1
    return n


def apply_events(cfg: "config.Config", events: list) -> dict:
    """Open all newly-stopped assets first, THEN classify the batch.

    A fleet-wide event (power cut, shift changeover) lands every loom in a single poll.
    Classifying per-incident as each row is inserted would let the simultaneity count
    cross its threshold only partway through the batch, so the first few looms would be
    mis-coded. Opening the whole batch before classifying makes fleet detection
    independent of processing order.
    """
    new_incidents: list[dict] = []
    resolved = 0
    for ev in events:
        if isinstance(ev, IncidentOpened):
            inc = incidents.open_incident(cfg, ev.asset_ref, ev.condition, at=ev.at,
                                          label=(ev.context or {}).get("label"))
            if inc.get("created"):
                new_incidents.append(inc)
        elif isinstance(ev, IncidentResolved):
            aid = incidents.asset_id_for(cfg, ev.asset_ref)
            row = db.query_one(
                "SELECT id FROM incidents WHERE asset_id=? AND status IN ('open','resolving')"
                " ORDER BY id DESC LIMIT 1",
                (aid,),
            )
            if row:
                incidents.begin_resolve(cfg, row["id"], at=ev.at)
                resolved += 1

    for inc in new_incidents:
        classify.on_open(cfg, inc)

    return {"opened": len(new_incidents), "resolved": resolved}


def poll_once(cfg: "config.Config", source) -> dict:
    """One poll cycle: fetch+diff via the source, apply to core."""
    events = source.poll(clock.now_iso())
    return apply_events(cfg, events)
