"""Export and restore the runtime configuration overrides.

Config used to live entirely in git, where it was backed up by definition and you could
read the diff before a change. It now lives in the YAML files PLUS the config_overrides
table, and that table is in no repository and no backup. After a few weeks it holds the
real roster: names, phone numbers, who covers nights, how long each fault waits.

What makes that dangerous rather than merely untidy is how it fails. load_overrides
returns {} and logs a warning if the table cannot be read, rather than raising — so a
lost or emptied table means ops-core BOOTS CLEANLY, silently reverts to the sample roster
committed in git, and pages invented numbers. Nothing turns red.

    python -m scripts.export_config                 # print to stdout
    python -m scripts.export_config -o backup.json  # write a file
    python -m scripts.export_config --restore backup.json

Run the export from cron and keep the output with your database backups.
"""

from __future__ import annotations

import argparse
import json
import sys

from app import clock, config, db


def _connect():
    # Same boot-order trap main.py hit: config.load() reads the runtime overrides from
    # the DATABASE, so calling it before db.init silently drops the override layer and
    # this tool would export the YAML as "effective" — a backup of the wrong config,
    # discovered when the identical bug in another script routed a live test message to
    # the wrong person's phone. Load, init, then re-apply.
    cfg = config.load()
    db.init(cfg.db_params(), cfg.table_prefix)
    config.reload_into(cfg, config.load_overrides())
    return cfg


def export_all() -> dict:
    cfg = _connect()
    rows = db.query("SELECT scope, patch, updated_at, updated_by FROM config_overrides")
    return {
        "exported_at": clock.now_iso(),
        "department": cfg.department,
        "table_prefix": cfg.table_prefix,
        "overrides": [dict(r) for r in rows],
        # The effective documents too, so the file is readable on its own and you can see
        # what the plant was actually running without replaying anything.
        "effective": {"reasons": cfg.reasons, "routing": cfg.routing,
                      "escalation": cfg.escalation},
    }


def restore(path: str) -> int:
    cfg = _connect()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    if data.get("department") != cfg.department:
        sys.exit(f"refusing: backup is for department '{data.get('department')}', "
                 f"this deployment is '{cfg.department}'")

    overrides = data.get("overrides") or []
    if not overrides:
        sys.exit("refusing: that backup contains no overrides — restoring it would be "
                 "indistinguishable from the loss you are recovering from")

    # Validate BEFORE writing anything. A backup taken against different YAML can be
    # internally consistent and still invalid here, and installing it unchecked would
    # turn a recovery into a second outage.
    proposed = {r["scope"]: json.loads(r["patch"]) for r in overrides}
    config.validate(config.candidate(cfg, proposed))

    now = clock.now_iso()
    with db.transaction() as c:
        for scope, patch in proposed.items():
            c.execute(
                "INSERT INTO config_overrides(scope, patch, updated_at, updated_by)"
                " VALUES (?,?,?,?) ON DUPLICATE KEY UPDATE"
                " patch=VALUES(patch), updated_at=VALUES(updated_at),"
                " updated_by=VALUES(updated_by)",
                (scope, json.dumps(patch), now, "restore"),
            )
    return len(proposed)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--out", help="write to this file instead of stdout")
    ap.add_argument("--restore", metavar="FILE",
                    help="restore overrides from a previous export")
    args = ap.parse_args()

    if args.restore:
        n = restore(args.restore)
        print(f"restored {n} scope(s). Restart ops-core, or the running process keeps "
              f"the config it already has.")
        return

    payload = json.dumps(export_all(), indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        print(f"wrote {args.out}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
