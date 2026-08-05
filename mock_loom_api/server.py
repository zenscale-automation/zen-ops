"""A stand-in for the Shingora Loom Data API that speaks the SAME contract as the real
one (see API_GUIDE): cursor-paged GET /data returning per-machine time-series rows where
rpm 0 means stopped, plus an open GET /health. Extra /control/* endpoints let the demo
stop and resume looms (they set rpm), which the real API obviously does not have.

    GET  /data?since=&device=&machine=&limit=   -> {since,to,count,truncated,rows:[...]}
    GET  /health                                -> {"status":"ok", ...}
    POST /control/stop   {"machine_id":"loom_5"|"5"}   (or {"machine_ids":[...]})
    POST /control/run    {"machine_id":"loom_5"|"5"}
    POST /control/reset

Auth: if LOOM_API_KEY is set, /data requires a matching X-API-Key header (401 otherwise),
mirroring the real API.
"""

from __future__ import annotations

import os
from datetime import datetime

from flask import Flask, jsonify, request

# Mirror the real fleet: only looms 91-94 are on the API today, the rest are planned.
# MOCK_LOOM_IDS overrides the list; MOCK_LOOM_COUNT still works for scale testing.
_IDS = os.environ.get("MOCK_LOOM_IDS", "91,92,93,94")
_COUNT = os.environ.get("MOCK_LOOM_COUNT")
LOOM_IDS = ([str(i) for i in range(1, int(_COUNT) + 1)] if _COUNT
            else [m.strip() for m in _IDS.split(",") if m.strip()])
API_KEY = os.environ.get("LOOM_API_KEY", "")
RUNNING_RPM = 300.0
COASTING_RPM = 7.0     # a stopped loom idles here, not at a clean zero

# machine-number -> rpm. A halted loom does NOT read a clean 0: it coasts down and
# idles in the single digits, which is why the real rule is a threshold (RPM_ON = 40),
# not "rpm == 0". The mock stops looms at COASTING_RPM so the demo exercises the real
# rule; a mock that only ever emits 0 or 300 would pass an adapter that is wrong.
STATE: dict[str, float] = {m: RUNNING_RPM for m in LOOM_IDS}
STATE["99_test"] = RUNNING_RPM  # excluded by source.yaml

# machine-number -> (weft, warp); 0 == thread broken
THREADS: dict[str, tuple[int, int]] = {}

# machines the mock has "unplugged" — they emit no rows at all (dead ESP32 / network)
DARK: set[str] = set()


def _now_local() -> str:
    # server local time, millisecond precision, NO timezone suffix (like the real API)
    return datetime.now().isoformat(timespec="milliseconds")


def _device_for(machine: str) -> str:
    try:
        idx = LOOM_IDS.index(machine)          # one ESP32 serves two looms
        return f"esp32-{(idx // 2) + 1:02d}"
    except ValueError:
        return "esp32-99"


def _norm(mid: str) -> str:
    return mid[5:] if mid.startswith("loom_") else mid


def _targets() -> list[str]:
    data = request.get_json(silent=True) or {}
    ids = data.get("machine_ids") or ([data["machine_id"]] if "machine_id" in data else [])
    return [_norm(str(m)) for m in ids]


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/data")
    def data():
        if API_KEY and request.headers.get("X-API-Key") != API_KEY \
                and request.headers.get("Authorization") != f"Bearer {API_KEY}":
            return jsonify({"error": "unauthorized"}), 401

        machine_filter = request.args.get("machine")
        ts = _now_local()
        rows = []
        for machine, rpm in STATE.items():
            if machine_filter and machine != machine_filter:
                continue
            if machine in DARK:          # unplugged: emits nothing, as a dead node does
                continue
            weft, warp = THREADS.get(machine, (1, 1))
            stopped = rpm < 40 or weft == 0 or warp == 0
            rows.append({
                "ts": ts,
                "device": _device_for(machine),
                "machine": machine,
                "weft": weft,
                "warp": warp,
                "rpm": rpm,
                "rpm_raw_sensor": 0 if stopped else 1,
                "ExtData": 1,
            })
        limit = request.args.get("limit", type=int)
        if limit:
            rows = rows[:limit]
        return jsonify({
            "since": request.args.get("since"),
            "to": ts,
            "count": len(rows),
            "truncated": False,
            "rows": rows,
        })

    @app.get("/health")
    def health():
        stopped = [m for m, rpm in STATE.items() if rpm < 40]
        return jsonify({"status": "ok", "looms": len(STATE), "stopped": stopped})

    @app.post("/control/stop")
    def stop():
        t = _targets()
        for m in t:
            if m in STATE:
                STATE[m] = COASTING_RPM
        return jsonify({"ok": True, "stopped": t})

    @app.post("/control/thread_break")
    def thread_break():
        """Break the weft on a loom that keeps turning — the case an rpm-only rule misses."""
        t = _targets()
        for m in t:
            if m in STATE:
                THREADS[m] = (0, 1)
        return jsonify({"ok": True, "thread_broken": t})

    @app.post("/control/dark")
    def dark():
        """Unplug a loom: it stops emitting rows entirely, like a dead edge device."""
        t = _targets()
        DARK.update(m for m in t if m in STATE)
        return jsonify({"ok": True, "dark": sorted(DARK)})

    @app.post("/control/run")
    def run():
        t = _targets()
        for m in t:
            if m in STATE:
                STATE[m] = RUNNING_RPM
                THREADS.pop(m, None)
                DARK.discard(m)
        return jsonify({"ok": True, "running": t})

    @app.post("/control/reset")
    def reset():
        for m in STATE:
            STATE[m] = RUNNING_RPM
        return jsonify({"ok": True})

    return app


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_LOOM_PORT", "8081"))
    create_app().run(host="127.0.0.1", port=port)
