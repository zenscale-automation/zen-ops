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

On the box (this deployment lives in `~/zen-ops`; unzip preserves dotfiles, `cp dir/*`
does not — `.env.aws` and `.gitignore` are dotfiles and will be silently dropped by a
glob copy):

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
cd ~/zen-ops

# MySQL: connect, confirm the automation db, confirm write grants
.venv/bin/python - << 'EOF'
import os, pymysql
from dotenv import load_dotenv
load_dotenv(".env")   # explicit: find_dotenv() walks the call stack and fails on stdin
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

## 4. Sanity-check the feed

The adapter's stop rules were audited against `loom_dashboard-10.html` and now match it
exactly: running is `rpm >= 40` **and** both threads intact; `weft == 0` or `warp == 0`
is a thread stop that overrides the tachometer. Polarity is confirmed from the
dashboard's own `isThreadStop`, so there is nothing left to guess — but it costs one
command to confirm the feed looks like the shed you expect:

```bash
cd ~/zen-ops
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

## 5. Create the tables

Nothing to do — the app runs `migrations/` itself on first boot and records them in
`opscore_schema_migrations`. If you'd rather see the DDL land through phpMyAdmin first,
paste `deploy/schema.mysql.sql` (already prefix-applied) into the SQL tab of the
`automation` database; it creates the tables *and* writes its own bookkeeping row, so
boot recognises the import and skips it.

Both paths are re-runnable. Every `CREATE` is `IF NOT EXISTS`, because MySQL commits
each DDL statement implicitly and a migration therefore cannot be atomic — if one dies
partway, the tables exist but the bookkeeping row does not, and the next boot re-runs
the file. Re-running is now a no-op that repairs the bookkeeping instead of failing with
`(1050, "Table 'opscore_assets' already exists")`.

If you hit that 1050 error on an older copy, the tables are fine — it is only the
bookkeeping that is missing. Pull the current `migrations/001_init.sql` and boot again;
it will reconcile itself. On a first deploy with no data yet, dropping the `opscore_*`
tables in phpMyAdmin and letting boot recreate them is equally safe.

## 6. Who gets messaged

There is no "do not really send" switch. A message queued for a person is sent to that
person, and who that is comes from one place: the roster, edited in the dashboard or
through `/api/admin/*`. Two guards keep that honest at boot:

- ops-core refuses to start while any routed person still has `placeholder: true` — those
  invented numbers belong to strangers, and it fails loudly with the list;
- it also refuses to start when people are routed over a channel whose credentials are
  missing, because the notifier would quietly fall back to a log file and record every
  page as sent.

## 7. Fleet size — retune as looms come online

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

## 8. First run, foreground

```bash
cd ~/zen-ops && .venv/bin/python -m app.main
```

Boot is loud on purpose: a config typo or a refused DB connection kills it with the
reason. Two lines you should see immediately:

```
INFO ops workers started: poller(30s) ticker(30s) outbox(5s)
INFO    ops ops-core serving on http://127.0.0.1:8000
```


From a second shell on the box, with the app still running:

```bash
curl -s localhost:8000/health          # "ok": true, both workers beating
```

Then check `automation` in phpMyAdmin: nine `opscore_*` tables. Within a poll cycle
(30 s) `opscore_assets` fills with the discovered looms — expect looms 91-94. If fewer
appear, the seed warning in the same log says how many reported.

If any loom is **already stopped** when you start, the log names it:

```
WARNING ops.source.loom 2 loom(s) already stopped at start with no open incident
        (loom_91, loom_93) — incidents will open on the first poll ...
```

That is correct: a loom that is down with nobody told is the failure this system exists
to remove, so ops-core opens an incident rather than waiting for the loom to run and
stop again. It also means a service restart catches up on whatever went down during the
outage. Duration is measured from the first poll — ops-core cannot know how long a loom
was down before it started watching, so these incidents understate total downtime.

Cross-check the dashboard against `loom_dashboard.html`: the two should agree on which
looms are down. If ops-core shows fewer looms down, that warning line is where to look.
Ctrl-C when done.

## 9. Install as a service

```bash
# The unit hardcodes /home/ubuntu/ops-core; rewrite it to wherever this actually lives.
sudo sed "s#/home/ubuntu/ops-core#${PWD}#g" deploy/ops-core.aws.service \
  | sudo tee /etc/systemd/system/ops-core.service > /dev/null
grep -E 'WorkingDirectory|ExecStart' /etc/systemd/system/ops-core.service
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

## 10. Reaching the dashboard

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

## 10b. Going live on WhatsApp

ops-core talks to Meta's WhatsApp Cloud API directly — no BSP. A BSP sells a shared
inbox, agent routing and campaign tooling; ops-core has its own queue, dedupe keys,
retry and escalation engine, so all of that would be paid for and bypassed.

**Start business verification on day one — it takes days to weeks and everything else
waits on it.** Meta cross-checks your Business Manager details against a public website,
and a mismatch in legal name or address is the usual rejection.

You also need a **dedicated phone number**. Once registered to the API it can never be
used on the normal WhatsApp or WhatsApp Business app again, so it must be a spare SIM.

### The template

Business-initiated messages are template-only. ops-core uses **one** UTILITY template
whose single variable carries the fully-rendered message. Submit exactly this:

```
Shingora ops-core

{{1}}

Reply to this message to respond.
```

The fixed lines above and below matter — Meta rejects templates that begin or end with a
variable. And the one-variable design is deliberate: bake the numbered reason list into
template text and every edit to `reasons.yaml` needs a fresh Meta approval, which
quietly undoes the promise that a Shingora engineer can change a reason code and
restart. One approval, forever; the list stays in YAML.

### Credentials

Fill the `WHATSAPP_*` block in `.env`. Two are easily confused:

| Value | Where it comes from | What it does |
|---|---|---|
| `WHATSAPP_APP_SECRET` | App settings &rarr; Basic | Signs inbound webhooks (`X-Hub-Signature-256`) |
| `WHATSAPP_VERIFY_TOKEN` | You invent it | Echoed once during the subscription handshake |

Use a **system user token** for `WHATSAPP_ACCESS_TOKEN`, not the temporary one on the
API-setup tab — that one expires in 24 hours and the fault will look like a delivery
outage at 3am.

### Public HTTPS is required

Replies come in over a webhook, so ops-core needs a reachable HTTPS endpoint — the one
thing the current localhost-only deployment does not have. Expose **only** the webhook
path, never the dashboard:

```bash
sudo tailscale funnel --bg --set-path /webhook/whatsapp 8000
```

Then in the Meta app dashboard set the callback URL to that path and the verify token to
`WHATSAPP_VERIFY_TOKEN`. Meta GETs once with `hub.challenge`; ops-core echoes it back.
After that, POSTs are rejected unless the signature matches the app secret.

### Cost, so nobody is surprised

Utility templates in India run roughly &#8377;0.11&ndash;0.15 each, and messages sent
inside an open 24-hour service window are free. A supervisor replying to the first
prompt of a shift opens that window for everything else you send them that day &mdash;
ops-core detects this automatically and switches to free-form text, which is both
cheaper and exempt from template formatting. At 44 looms expect low hundreds of rupees a
month.

### Flip the switch

Only after the roster is real: replace the people block in `routing.yaml`, drop the
`placeholder` flags, then restart. Boot will refuse
while any placeholder remains.

## 10c. Changing config at runtime

The YAML files remain the base — git-tracked, commented, and what you read to understand
the system. The API stores a patch on top and reloads the live config without a restart.

Set `OPS_ADMIN_API_KEY` in `.env`. **If it is unset, config writes are refused entirely**
— absent credentials fail closed, not open.

```bash
K='X-Admin-Key: <your key>'

# read the effective config, and which scopes are overridden
curl -s localhost:8000/api/config | python3 -m json.tool

# change a ladder — RFC 7386 merge patch, so send only what changes
curl -s -X PATCH localhost:8000/api/config/escalation -H "$K" \
  -H 'X-Admin-User: gurpreet' -H 'Content-Type: application/json' \
  -d '{"ladders":{"weaving.electrical":[{"after_minutes":0,"notify":"owner"},
                                        {"after_minutes":8,"notify":"supervisor"}]}}'

# revert a scope back to the YAML
curl -s -X DELETE localhost:8000/api/config/reasons -H "$K"
```

Objects merge recursively and `null` deletes a key. **Lists replace wholesale** — an
escalation ladder is an ordered sequence, and merging two ladders element-wise produces
a rung order nobody asked for.

Three guarantees, because config is the one thing that can silently disable everything:

- **Validated before commit.** A patch is applied to a throwaway config and run through
  the same checks the process uses at boot. Fail and you get `422` with the problem list
  and nothing changes — the boot-time "fail loud" guarantee becomes "reject the write",
  never "accept a config that routes nothing to nobody at 3am".
- **Authenticated.** `X-Admin-Key` on every mutation. Reads stay open behind whatever
  fronts the dashboard.
- **Audited.** Every change lands in the event log with the patch and the actor. Read it
  back with `GET /api/events/config/0` — who widened the ladder at 2am is exactly the
  kind of question this system exists to answer.

`source` is **restart-only** and returns `409`. Its settings are read once when the
adapter is built, so changing them at runtime would appear to work and do nothing, and
rebuilding the adapter mid-shift discards the API cursor and every asset's last-known
state.

## 10d. Who gets told

`routing.yaml` now has two keys that decide this:

```yaml
default_owner: production_manager
route_all_to_default: true
```

`default_owner` is the backstop: any role that resolves to nobody — a reason with no
owner, a shift with nobody rostered — goes to that one person instead of nowhere.
Without it those notifications have no recipient at all, which is the "no owner" defect
recreated inside the fix for it.

`route_all_to_default` sends **every** ticket and escalation to that person regardless
of role. That is the right setting for the pilot: until the real per-role roster exists,
one accountable person is honest and per-role routing to invented names is not. Turn it
off with a single PATCH once the roster is in.

**Reason prompts are deliberately exempt.** The "why is this asset stopped?" question
always goes to whoever can actually see it, never to the default owner — asking a
manager who cannot see the shed floor produces no useful answer and stalls the flow
waiting for one.

## 11. Day-2

Logs: `~/zen-ops/logs/stdout.log`, `stderr.log`, and every outbound message in
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
