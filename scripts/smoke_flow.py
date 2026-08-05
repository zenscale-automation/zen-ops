"""Manual end-to-end smoke of the core lifecycle against MySQL, driven by a virtual
clock. Not a pytest test — a quick visual check. Run: python -m scripts.smoke_flow
"""
from datetime import datetime, timezone

from app import clock, config, db
from app.core import events, incidents, ticker, outbox

cfg = config.load()
db.init(cfg.db_params(), cfg.table_prefix)
db.reset_all()
db.migrate()

# 09:00 IST (03:30 UTC), shift A, away from any boundary
clock.CLOCK.set_virtual(datetime(2026, 8, 5, 3, 30, 0, tzinfo=timezone.utc))
print("shift at open:", clock.resolve_shift(clock.now(), cfg.shifts))

inc = incidents.open_incident(cfg, "loom_23", "STOPPED")
from app.core import classify
classify.on_open(cfg, inc)
print("opened incident", inc["id"], "status", inc["status"])
esc = db.query("SELECT rung, action, notify_role, due_at, status FROM escalations WHERE incident_id=?", (inc["id"],))
print("unknown ladder scheduled:", esc)

# +15 min -> first ask_reason fires
clock.CLOCK.advance(15 * 60)
print("tick@+15:", ticker.tick(cfg))
print("outbox drain:", outbox.drain(cfg))

# supervisor replies '1' -> electrical
res = incidents.set_reason(cfg, inc["id"], "weaving.electrical", method="reply", actor="amarjit_s")
print("reason set -> ticket:", res["ticket"]["id"], "owner", res["ticket"]["owner_role"])
print("unknown escalations now:",
      db.query("SELECT rung,status FROM escalations WHERE incident_id=?", (inc["id"],)))

# ticket ladder rung 0 (owner) is due immediately
print("tick (owner):", ticker.tick(cfg))
print("outbox drain:", outbox.drain(cfg))
tkt = db.query_one("SELECT * FROM tickets WHERE id=?", (res["ticket"]["id"],))
print("ticket first_notified_at:", tkt["first_notified_at"], "status", tkt["status"])

# +20 -> supervisor rung; +45 -> shift_incharge rung
clock.CLOCK.advance(20 * 60)
print("tick@+20:", ticker.tick(cfg)); outbox.drain(cfg)
clock.CLOCK.advance(25 * 60)
print("tick@+45:", ticker.tick(cfg)); outbox.drain(cfg)

# machine resumes -> grace -> resolve
incidents.begin_resolve(cfg, inc["id"])
print("after begin_resolve, incident status:",
      db.query_one("SELECT status,resolve_due_at FROM incidents WHERE id=?", (inc["id"],)))
clock.CLOCK.advance(60)  # past 45s grace
print("tick (resolve):", ticker.tick(cfg))

fin = db.query_one("SELECT status,duration_s FROM incidents WHERE id=?", (inc["id"],))
tkt = db.query_one("SELECT status,close_reason FROM tickets WHERE id=?", (res["ticket"]["id"],))
print("FINAL incident:", fin, "ticket:", tkt)
print("\n--- incident timeline ---")
for e in events.timeline("incident", inc["id"]):
    print(f"  {e['at']}  {e['kind']:<10} {e['detail']}")
print("--- ticket timeline ---")
for e in events.timeline("ticket", res["ticket"]["id"]):
    print(f"  {e['at']}  {e['kind']:<10} {e['detail']}")
print("\nSMOKE OK")
