"""Bring loom 93 back as the live-test dummy. See original docstring."""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import clock, config, db          # noqa: E402
from app.core import classify, incidents   # noqa: E402

# config.load() reads the runtime overrides from the DATABASE — the same boot-order
# trap main.py hit: call it before db.init and load_overrides fails silently, the
# override layer vanishes, and this script schedules rungs from the raw YAML. That is
# exactly how a test prompt reached the wrong person's real phone. Load, init, then
# re-apply — mirroring main.py.
cfg = config.load()
db.init(cfg.db_params(), cfg.table_prefix)
config.reload_into(cfg, config.load_overrides())

with db.transaction() as c:
    aid = incidents.ensure_asset(c, cfg, "loom_93")
    c.execute("UPDATE assets SET active=1 WHERE id=?", (aid,))
print("loom_93 active=1")

backdated = clock.to_iso(clock.now() - timedelta(minutes=19))
inc = incidents.open_incident(cfg, "loom_93", "STOPPED", at=backdated)
print("incident", inc["id"], "opened_at", inc["opened_at"], "created:", inc.get("created"))

if inc.get("created"):
    classify.on_open(cfg, inc)
    print("unknown ladder scheduled — routed to:", cfg.unknown_ladder[0]["notify"])
else:
    print("incident already existed — ladder untouched")
