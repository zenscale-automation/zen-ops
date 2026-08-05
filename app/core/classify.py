"""Auto-classification, evaluated on stop-started.

Anything the machine data can classify without a human is one less thing an operator
has to tap. Two families:

  * decidable at open time from simultaneity + the clock — a fleet-wide stop within a
    few seconds is a power failure; a fleet-wide stop at a shift boundary is a
    changeover / punch-station queue. These are recorded and page nobody.
  * decidable only at resolve time — a stop shorter than min_duration_seconds is a short
    stop (handled in incidents.commit_resolve, the min-duration gate).

If nothing matches at open, the 'unknown' escalation ladder is scheduled: after
prompt_after_minutes it will ask the supervisor for a reason.

"Fleet-wide" needs a count the design doc leaves to measurement; we default to a
fraction of the active fleet (reasons.yaml: fleet_fraction) with a short simultaneity
window. Documented placeholder — tune with real data.
"""

from __future__ import annotations

import math

from .. import clock, config, db
from . import escalation, incidents

BOUNDARY_SIMULTANEITY_SECONDS = 300  # window to call a boundary stop "fleet-wide"


def _active_fleet_size(cfg: "config.Config") -> int:
    r = db.query_one("SELECT COUNT(*) n FROM assets WHERE department=? AND active=1",
                     (cfg.department,))
    return r["n"] if r and r["n"] else 1


def _fleet_threshold(cfg: "config.Config") -> int:
    frac = float(cfg.reasons.get("fleet_fraction", 0.5))
    return max(2, math.ceil(_active_fleet_size(cfg) * frac))


def _stopped_within(cfg: "config.Config", center_iso: str, window_s: float) -> int:
    lo = clock.plus_seconds(-window_s, base=clock.parse(center_iso))
    r = db.query_one(
        "SELECT COUNT(*) n FROM incidents WHERE department=? AND status IN ('open','resolving')"
        " AND opened_at BETWEEN ? AND ?",
        (cfg.department, lo, center_iso),
    )
    return r["n"] if r else 0


def classify_at_open(cfg: "config.Config", incident_row) -> str | None:
    """Return an auto-classify code if one applies at open time, else None."""
    opened_at = incident_row["opened_at"]
    threshold = _fleet_threshold(cfg)
    for rule in cfg.auto_classify:
        kind = rule.get("rule")
        code = rule.get("code")
        if kind == "fleet_stop":
            window = float(rule.get("within_seconds", 5))
            if _stopped_within(cfg, opened_at, window) >= threshold:
                return code
        elif kind == "fleet_stop_at_boundary":
            window_min = float(rule.get("window_minutes", 20))
            at_boundary = clock.within_shift_boundary(clock.parse(opened_at), cfg.shifts, window_min)
            if at_boundary and _stopped_within(cfg, opened_at, BOUNDARY_SIMULTANEITY_SECONDS) >= threshold:
                return code
        # duration_under is handled at resolve time (min-duration gate)
    return None


def on_open(cfg: "config.Config", incident_row) -> None:
    """Called by the poller right after an incident opens."""
    code = classify_at_open(cfg, incident_row)
    if code:
        incidents.set_reason(cfg, incident_row["id"], code, method="auto",
                             actor="system", at=incident_row["opened_at"])
        return
    # nobody could classify it — start the reason-prompt ladder
    with db.transaction() as c:
        escalation.schedule_unknown(c, cfg, incident_row)
