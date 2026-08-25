"""GET /health — liveness, worker heartbeats, outbox depth, last poll time.
Unauthenticated, intended to be bound to localhost (nginx does not expose it).
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from .. import clock
from ..workers import SUPERVISOR

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    h = SUPERVISOR.health()
    now = clock.now()
    stale = []
    for name, beat in (h.get("workers") or {}).items():
        age = (now - clock.parse(beat)).total_seconds()
        if age > 120:  # no heartbeat in 2 minutes => stale
            stale.append(name)
    # A poll that has never succeeded is not a heartbeat problem — the worker ticks
    # happily and returns nothing — so age the last successful poll separately. Six
    # minutes is three cycles at the 30s default before anyone is woken.
    poll_age = None
    if h.get("last_poll_at"):
        poll_age = int((now - clock.parse(h["last_poll_at"])).total_seconds())
    poll_stale = (poll_age is None) or (poll_age > 360)

    failed = int(h.get("outbox_failed_1h") or 0)
    wedged = int(h.get("escalations_wedged") or 0)

    problems = []
    if not h.get("started"):
        problems.append("workers not started")
    if stale:
        problems.append("no heartbeat from: " + ", ".join(stale))
    if poll_stale:
        problems.append("no successful loom poll"
                        + (f" for {poll_age}s" if poll_age is not None else " ever"))
    if failed:
        problems.append(f"{failed} page(s) gave up in the last hour — somebody was not called")
    if wedged:
        problems.append(f"{wedged} escalation(s) parked and will never fire")

    ok = not problems
    cfg = current_app.config.get("OPS_CFG")
    body = {"ok": ok, "now": clock.now_iso(), "stale_workers": stale,
            "problems": problems, "last_poll_age_s": poll_age,
            "shadow_mode": bool(getattr(cfg, "shadow_mode", True)), **h}
    return jsonify(body), (200 if ok else 503)
