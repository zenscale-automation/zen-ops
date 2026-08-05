"""One-command local demo.

Starts the mock loom API and ops-core (fast worker cadences), seeds a day of resolved
downtime, stops two looms, and posts signed supervisor replies so two live tickets open.
Then it just runs — open the dashboard and watch. Ctrl-C stops everything.

    python -m scripts.run_demo

Requires the MySQL settings in .env to point at a reachable database.
"""

import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

APP = "http://127.0.0.1:8000"
MOCK = "http://127.0.0.1:8081"
LOOM_KEY = os.environ.get("LOOM_API_KEY", "dev-loom-key-changeme")
SECRET = os.environ.get("WHATSAPP_WEBHOOK_SECRET", "")

procs: list[subprocess.Popen] = []


def _spawn(mod, extra_env=None):
    env = dict(os.environ)
    env.setdefault("LOOM_API_KEY", LOOM_KEY)
    if extra_env:
        env.update(extra_env)
    p = subprocess.Popen([sys.executable, "-m", mod], cwd=str(ROOT), env=env)
    procs.append(p)
    return p


def _wait(url, tries=40):
    for _ in range(tries):
        try:
            if requests.get(url, timeout=2).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _stop_loom(mid):
    requests.post(f"{MOCK}/control/stop", json={"machine_id": mid}, timeout=5)


def _reply(asset_ref, digit, sender="+919000000005"):
    body = json.dumps({"from": sender, "text": digit, "asset_ref": asset_ref}).encode()
    headers = {"Content-Type": "application/json"}
    if SECRET:
        headers["X-Signature"] = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    r = requests.post(f"{APP}/webhook/whatsapp", data=body, headers=headers, timeout=5)
    print("   reply", asset_ref, "->", digit, ":", r.json())


def _cleanup(*_):
    print("\nstopping demo…")
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    # fresh database
    from app import config, db
    cfg = config.load()
    db.init(cfg.db_params(), cfg.table_prefix)
    db.reset_all(); db.migrate()
    print("· database reset")

    print("· starting mock loom API on :8081")
    _spawn("mock_loom_api.server", {"MOCK_LOOM_COUNT": "44"})
    if not _wait(f"{MOCK}/health"):
        print("mock loom API did not start"); _cleanup()

    print("· starting ops-core on :8000 (fast cadences)")
    _spawn("app.main", {"OPS_POLL_SECONDS": "2", "OPS_TICKER_SECONDS": "2", "OPS_OUTBOX_SECONDS": "1"})
    if not _wait(f"{APP}/health"):
        print("ops-core did not start"); _cleanup()

    print("· seeding a day of resolved downtime")
    subprocess.run([sys.executable, "-m", "scripts.seed_demo"], cwd=str(ROOT), check=False)

    print("· stopping loom_5 and loom_12")
    _stop_loom("loom_5"); _stop_loom("loom_12")
    time.sleep(4)  # let the poller open incidents
    print("· supervisor replies:")
    _reply("loom_5", "1")   # electrical
    _reply("loom_12", "2")  # mechanical

    print("\n  ✔ Demo running.")
    print(f"  ▶ Dashboard:  {APP}/")
    print(f"  ▶ Health:     {APP}/health")
    print("  ▶ Notifications (log notifier):  logs/notifications.log")
    print("  Ctrl-C to stop.\n")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
