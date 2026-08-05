"""Generate tests/fixtures/recorded_shift.json — a recorded slice of a weaving shift in
the real API's terms (machine numbers; a machine is "stopped" when its rpm is 0). The
fixture lists, per snapshot, which machine numbers are stopped; the fixture source
synthesizes /data rows from it. Snapshots are sparse (state holds between them); the
replay harness steps the virtual clock finely enough to catch escalation timers.

Scenario (shift A; times in UTC, 00:30Z == 06:00 IST):
  * machine 10  short stop  00:35 -> 00:36  (60s < 120s => short_stop auto-coded)
  * machine 3   long stop   00:40 -> 01:30  (50 min => full 'unknown' ladder)
  * machines 1,2,4,5,6,7,8,9  power  01:00 -> 01:03  (8 simultaneous => power_failure)
Run: python -m scripts.gen_fixture
"""

import json
from pathlib import Path

N = 12
MACHINES = [str(i) for i in range(1, N + 1)] + ["99_test"]
POWER = ["1", "2", "4", "5", "6", "7", "8", "9"]

snapshots = [
    {"at": "2026-08-05T00:30:00+00:00", "stopped": []},
    {"at": "2026-08-05T00:35:00+00:00", "stopped": ["10"]},
    {"at": "2026-08-05T00:36:00+00:00", "stopped": []},
    {"at": "2026-08-05T00:40:00+00:00", "stopped": ["3"]},
    {"at": "2026-08-05T01:00:00+00:00", "stopped": ["3"] + POWER},
    {"at": "2026-08-05T01:03:00+00:00", "stopped": ["3"]},
    {"at": "2026-08-05T01:30:00+00:00", "stopped": []},
    {"at": "2026-08-05T01:35:00+00:00", "stopped": []},
]

fixture = {
    "department": "weaving",
    "note": "recorded shift slice for replay; see scripts/gen_fixture.py",
    "machines": MACHINES,
    "poll_seconds": 30,
    "snapshots": snapshots,
}

out = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "recorded_shift.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
print("wrote", out, "with", len(snapshots), "snapshots,", len(MACHINES), "machines")
