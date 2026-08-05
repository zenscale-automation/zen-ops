"""Manual source — human-reported stoppages (panel / webhook).

In Phase 1 the *stop* is always detected from the loom API; the only human input is the
reason reply. This adapter is the extension point for departments (or Phase 2 panels)
where a person, not a machine, declares that an asset has entered a state needing
resolution. It emits the same IncidentOpened / IncidentResolved the core already
understands, so nothing in core changes.
"""

from __future__ import annotations

import threading

from .. import clock, config
from .base import IncidentOpened, IncidentResolved


class ManualSource:
    def __init__(self, cfg: "config.Config"):
        self.cfg = cfg
        self.department = cfg.department
        self._q: list = []
        self._lock = threading.Lock()

    def seed(self) -> None:
        return None

    def discover_assets(self) -> list[dict]:
        return []

    # called by an inbound webhook / panel handler
    def report_stop(self, asset_ref: str, condition: str = "REPORTED",
                    at: str | None = None, context: dict | None = None) -> None:
        with self._lock:
            self._q.append(IncidentOpened(asset_ref=asset_ref, condition=condition,
                                          at=at or clock.now_iso(), context=context or {}))

    def report_resume(self, asset_ref: str, at: str | None = None) -> None:
        with self._lock:
            self._q.append(IncidentResolved(asset_ref=asset_ref, at=at or clock.now_iso()))

    def poll(self, now_iso: str | None = None) -> list:
        with self._lock:
            drained, self._q = self._q, []
        return drained
