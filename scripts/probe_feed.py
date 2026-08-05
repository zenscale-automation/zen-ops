"""Per-machine breakdown of a /data response — which looms are running, what the rpm
distribution actually looks like, and where the thread-low rows are.

    KEY=$(grep '^LOOM_API_KEY' .env | cut -d= -f2)
    curl -s -H "X-API-Key: $KEY" \
      'https://cldserver.tailf33eb4.ts.net/data?limit=2000' | python3 scripts/probe_feed.py

The fleet-wide summary in the runbook tells you the feed is alive; this tells you what
state the shed is in, which is what you need before trusting an incident count.
"""
import json, sys, collections, datetime as dt
rows = json.load(sys.stdin)["rows"]
ts = sorted(r["ts"] for r in rows)
span = dt.datetime.fromisoformat(ts[-1]) - dt.datetime.fromisoformat(ts[0])
print(f"{len(rows)} rows spanning {span} ({ts[0]} -> {ts[-1]})")
print(f"{'loom':>6} {'rows':>5} {'rpm min':>8} {'rpm max':>8} {'median':>7} "
      f"{'>=40':>5} {'weft0':>6} {'warp0':>6}  verdict")
by = collections.defaultdict(list)
for r in rows:
    by[str(r["machine"])].append(r)
for m in sorted(by):
    rs = by[m]
    rpm = sorted(float(x.get("rpm") or 0) for x in rs)
    run = sum(1 for v in rpm if v >= 40)
    w0 = sum(1 for x in rs if x.get("weft") == 0)
    p0 = sum(1 for x in rs if x.get("warp") == 0)
    med = rpm[len(rpm) // 2]
    verdict = ("RUNNING" if run == len(rs) else "STOPPED" if run == 0
               else f"mixed ({run}/{len(rs)} running)")
    print(f"{m:>6} {len(rs):>5} {rpm[0]:>8.1f} {rpm[-1]:>8.1f} {med:>7.1f} "
          f"{run:>5} {w0:>6} {p0:>6}  {verdict}")
zero = sum(1 for r in rows if float(r.get("rpm") or 0) == 0)
low = sum(1 for r in rows if 0 < float(r.get("rpm") or 0) < 40)
print(f"\nsub-40 breakdown: exactly 0.0 -> {zero}, coasting 0<rpm<40 -> {low}")
for r in rows:
    if r.get("weft") == 0 or r.get("warp") == 0:
        print("thread-low row:", {k: r.get(k) for k in ("ts","machine","weft","warp","rpm")})
