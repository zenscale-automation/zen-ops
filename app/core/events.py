"""The append-only event log — the table that actually kills the blame game.

Every state change is written here with a timestamp and an actor, inside the same
transaction as the change itself, so the timeline can never disagree with the state.
Never updated, never deleted. `log()` therefore *requires* a live cursor from an open
transaction rather than opening its own.
"""

from __future__ import annotations

import json

from .. import clock


def log(
    c,
    entity: str,
    entity_id: int,
    kind: str,
    actor: str = "system",
    detail: dict | None = None,
    department: str | None = None,
    at: str | None = None,
) -> None:
    c.execute(
        "INSERT INTO events(at, department, entity, entity_id, kind, actor, detail)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            at or clock.now_iso(),
            department,
            entity,
            entity_id,
            kind,
            actor,
            json.dumps(detail) if detail is not None else None,
        ),
    )


def timeline(entity: str, entity_id: int) -> list[dict]:
    """Full ordered audit trail for one entity, for GET /api/events/{entity}/{id}."""
    from .. import db

    rows = db.query(
        "SELECT * FROM events WHERE entity=? AND entity_id=? ORDER BY at, id",
        (entity, entity_id),
    )
    out = []
    for r in rows:
        d = dict(r)
        if d.get("detail"):
            try:
                d["detail"] = json.loads(d["detail"])
            except Exception:
                pass
        out.append(d)
    return out
