# Deploying ops-core to the AWS box (ap-south-1)

Target: the same EC2 instance running zenscale-mcp. Storage: the empty `automation`
database on the Zenscale MySQL server (15.207.31.57), with every table prefixed
`opscore_` because that database will be shared with zenscale-mcp artifacts later.

What was changed in this build for AWS: `PyMySQL` added to `requirements.txt` (it was
imported but never listed — a fresh venv on the box would have crashed at boot),
`.env.aws` (a filled template for this server), `deploy/ops-core.aws.service` (systemd
unit without the local-MariaDB ordering, running as `ubuntu` from the home directory),
and `deploy/schema.mysql.sql` regenerated with the `opscore_` prefix applied so a manual
phpMyAdmin import matches what the app creates. No application code changed.

---

## 1. Copy the project up and install

From your laptop:

```bash
scp ops-core.zip ubuntu@<EC2-IP>:~
```

On the box:

```bash
cd ~ && unzip -o ops-core.zip && cd ops-core
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.aws .env && nano .env
```

Fill the three placeholders: `OPS_DB_USER` / `OPS_DB_PASSWORD` (same credentials
zenscale-mcp uses — `grep '^ZS_DB' ~/zenscale-mcp/.env` — unless you were issued a
separate write account for `automation`) and `LOOM_API_KEY` (the X-API-Key from the
API guide; it's the same key the loom dashboard uses).

## 3. Verify reachability before first boot

Both legs, from the box itself:

```bash
cd ~/ops-core

# MySQL: connect, confirm the automation db, confirm write grants
.venv/bin/python - << 'EOF'
import os, pymysql
from dotenv import load_dotenv; load_dotenv()
c = pymysql.connect(host=os.environ["OPS_DB_HOST"], port=int(os.environ["OPS_DB_PORT"]),
                    user=os.environ["OPS_DB_USER"], password=os.environ["OPS_DB_PASSWORD"],
                    database=os.environ["OPS_DB_NAME"])
with c.cursor() as cur:
    cur.execute("SELECT DATABASE(), VERSION()"); print(cur.fetchone())
    cur.execute("SHOW GRANTS FOR CURRENT_USER()")
    for (g,) in cur.fetchall(): print(g)
EOF

# Loom API: the Funnel URL is public, so this works without Tailscale
curl -s -H "X-API-Key: $(grep '^LOOM_API_KEY' .env | cut -d= -f2)" \
  https://cldserver.tailf33eb4.ts.net/health
```

The grants must show write rights (`ALL PRIVILEGES` or at least SELECT, INSERT,
UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES) **on `automation`.\***. If the
account is SELECT-only — the zenscale-mcp account may deliberately be — ask for a
write grant on `automation.*` before going further; nothing here touches
`scaleshi_scaledb`.

## 3b. Sanity-check the feed

The adapter's stop rules were audited against `loom_dashboard-10.html` and now match it
exactly: running is `rpm >= 40` **and** both threads intact; `weft == 0` or `warp == 0`
is a thread stop that overrides the tachometer. Polarity is confirmed from the
dashboard's own `isThreadStop`, so there is nothing left to guess — but it costs one
command to confirm the feed looks like the shed you expect:

```bash
cd ~/ops-core
KEY=$(grep '^LOOM_API_KEY' .env | cut -d= -f2)
curl -s -H "X-API-Key: $KEY" 'https://cldserver.tailf33eb4.ts.net/data?limit=2000' \
  | python3 -c "
import json,sys,collections
rows=json.load(sys.stdin)['rows']
print('machines seen:', len({r[\"machine\"] for r in rows}))
print('weft:', collections.Counter(r.get('weft') for r in rows))
print('warp:', collections.Counter(r.get('warp') for r in rows))
rpm=[float(r.get('rpm') or 0) for r in rows]
print('rpm range:', min(rpm), '->', max(rpm), '| under 40:', sum(1 for v in rpm if v<40), 'of', len(rpm))
"
```

Expect ~44 machines, `weft`/`warp` overwhelmingly 1 on a running shed, and an rpm range
that looks continuous rather than only 0 and ~300. If `machines seen` is well under 44,
the missing looms are invisible to monitoring until they transmit — ops-core logs a
warning at seed for exactly this, using `expected_count` in `source.yaml`.

## 4. Create the tables

Nothing to do — the app runs `migrations/` itself on first boot and records them in
`opscore_schema_migrations`. If you'd rather see the DDL land through phpMyAdmin
first, paste `deploy/schema.mysql.sql` (already prefix-applied) into the SQL tab of
the `automation` database; boot then finds everything applied and skips it.

## 4b. Shadow mode

This deployment ships with `OPS_SHADOW_MODE=true`. The full pipeline runs — poll,
classify, route, escalate, and write every event to MySQL — but no message goes out on
any channel. Everything that *would* have been sent lands in `logs/notifications.log`,
tagged with the channel it was destined for.

It is a single explicit switch rather than the side effect of blank credential fields,
because the placeholder roster in `routing.yaml` carries validly-formatted Indian mobile
numbers. Without the switch, the day someone pastes in a BSP token to test the wiring,
ops-core starts texting strangers. Two things make that impossible:

- every channel resolves to the log notifier while shadow mode is on, credentials or not;
- with shadow mode **off**, ops-core refuses to boot while any routed person still has
  `placeholder: true` — it fails loudly with the list, the same way a bad reason code does.

Confirm it: `curl -s localhost:8000/health | grep shadow_mode` and the WARNING line in
`logs/stdout.log` at every start.

Going live later is three steps: replace the people block in `routing.yaml` with the real
roster and drop the `placeholder` flags, set the channel credentials in `.env`, then set
`OPS_SHADOW_MODE=false` and restart.

## 4c. Fleet size — retune as looms come online

Only looms **91-94** are on the API today; the other 40 are planned. Two settings are
tied to that number and both are already set for a fleet of four:

| Setting | File | Now | Why it matters |
|---|---|---|---|
| `fleet_fraction` | `reasons.yaml` | `1.0` | Threshold is `max(2, ceil(assets * fraction))`. At `0.5` on four machines that collapses to **2**, so any two looms stopping in the same poll would be auto-coded `power_failure` — which is not ticketable, so both faults disappear silently and nobody is paged. |
| `expected_count` | `source.yaml` | `4` | Logs a warning at seed if fewer machines report, so a loom that stops transmitting is noticed rather than quietly missing. |

As machines come online, raise `expected_count` to match and lower `fleet_fraction` —
roughly 0.7 past ten looms, 0.6 past twenty — checking against real power-cut events in
the event log. Erring high is the safe direction: an unclassified fleet stop merely asks
the supervisor, whereas a threshold set too low silences genuine faults. Two tests in
`tests/test_classify.py` pin both directions at the current fleet size and will fail if
`fleet_fraction` drifts back down before the fleet grows.

## 5. First run, foreground

```bash
cd ~/ops-core && .venv/bin/python -m app.main
```

Boot is loud on purpose: a config typo or a refused DB connection kills it with the
reason. Healthy looks like `ops-core serving on http://127.0.0.1:8000`. From a second
shell on the box: `curl -s localhost:8000/health` — expect `"ok": true` and both
worker heartbeats. Check `automation` in phpMyAdmin: nine `opscore_*` tables. Within
a poll cycle (30 s), `opscore_assets` fills with the discovered looms. Ctrl-C.

## 6. Install as a service

```bash
sudo cp deploy/ops-core.aws.service /etc/systemd/system/ops-core.service
sudo systemctl daemon-reload
sudo systemctl enable --now ops-core
systemctl status ops-core --no-pager
```

Then verify the way the handover checklist demands — reboot the instance and confirm
it came back by itself:

```bash
sudo reboot
# ...reconnect...
curl -s localhost:8000/health
```

**Exactly one instance.** This box is now the one place ops-core runs. Don't also
start it on the plant server against the same database — every notification doubles.

## 7. Reaching the dashboard

The app binds 127.0.0.1 only; nothing is exposed on the EC2 public IP and no
security-group change is needed. Two options, use either:

SSH tunnel (zero setup): `ssh -L 8000:127.0.0.1:8000 ubuntu@<EC2-IP>` then open
http://localhost:8000/ on your laptop.

Tailscale serve (persistent, HTTPS, shareable inside the tailnet — the better
end-state, and it also future-proofs the loom-API leg for the day the Funnel is
closed):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg 8000
tailscale serve status   # prints the https://<machine>.<tailnet>.ts.net URL
```

The bundled `deploy/nginx.conf` is for the on-prem/public-TLS variant and isn't
needed in either option; skip it.

## 8. Day-2

Logs: `~/ops-core/logs/stdout.log`, `stderr.log`, and every outbound message in
`logs/notifications.log` (log notifier is the default until WhatsApp/GChat
credentials go into `.env`). Restart after any YAML edit:
`sudo systemctl restart ops-core` — timers are rows, in-flight incidents resume.
Health from the box: `curl -s localhost:8000/health` (503 = a worker heartbeat is
stale). To wipe and restart clean during the pilot, stop the service, drop only the
`opscore_*` tables in phpMyAdmin, start the service.

Two cautions with the shared database. First, never run `scripts/run_demo`,
`scripts/smoke_flow`, or the pytest suite with `.env` pointing at the real
`automation` — they call `reset_all()`, which drops the `opscore_*` tables (only
those, but still your production data). Second, don't change `OPS_TABLE_PREFIX`
after first boot; the app would happily migrate a second, empty table set under the
new prefix and start from nothing.
