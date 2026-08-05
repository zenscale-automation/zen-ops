"""Fixture loom source — the replay transport.

Subclasses the real adapter and swaps only the HTTP page fetch for synthesized /data
rows built from a recorded sequence of snapshots, driven by the virtual clock. Each
snapshot lists which machines are stopped at a given instant; the fixture emits one row
per machine (rpm 0 when stopped) so the adapter's real diff/cursor logic is exercised.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import clock, config
from .weaving_loom_api import WeavingLoomApiSource

RUNNING_RPM = 300.0


class FixtureLoomSource(WeavingLoomApiSource):
    def __init__(self, cfg: "config.Config", fixture):
        super().__init__(cfg)
        self.machines = [str(m) for m in (fixture.get("machines") or [])]
        self.snapshots = sorted(fixture["snapshots"], key=lambda s: s["at"])

    @classmethod
    def from_file(cls, cfg: "config.Config", path: str | Path) -> "FixtureLoomSource":
        with open(path, encoding="utf-8") as fh:
            return cls(cfg, json.load(fh))

    def _stopped_at(self, now_iso: str) -> set[str]:
        chosen = None
        for snap in self.snapshots:
            if snap["at"] <= now_iso:
                chosen = snap
            else:
                break
        return set(str(m) for m in (chosen["stopped"] if chosen else []))

    def _fetch_page(self, since):
        now = clock.now_iso()
        stopped = self._stopped_at(now)
        rows = []
        for m in self.machines:
            rpm = 0.0 if m in stopped else RUNNING_RPM
            rows.append({
                "ts": now, "device": "esp32-fix", "machine": m,
                "weft": 1, "warp": 1, "rpm": rpm,
                "rpm_raw_sensor": 0 if rpm <= 0 else 1, "ExtData": 1,
            })
        return rows, now, False

    @property
    def first_snapshot_at(self) -> str | None:
        return self.snapshots[0]["at"] if self.snapshots else None

    @property
    def last_snapshot_at(self) -> str | None:
        return self.snapshots[-1]["at"] if self.snapshots else None
