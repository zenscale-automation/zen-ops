"""GET /health — liveness, worker heartbeats, outbox depth, last poll time.
Unauthenticated, intended to be bound to localhost (nginx does not expose it).
"""

from __future__ import annotations

from flask import Blueprint, jsonify

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
    ok = bool(h.get("started")) and not stale
    body = {"ok": ok, "now": clock.now_iso(), "stale_workers": stale, **h}
    return jsonify(body), (200 if ok else 503)
