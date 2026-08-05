"""Read-only JSON endpoints for the dashboard and audit.

  GET /api/overview                 KPIs for the dashboard header
  GET /api/tickets/open             current open tickets
  GET /api/incidents/open           currently-down assets
  GET /api/downtime                 aggregated by reason / shift / asset (range)
  GET /api/response-times           notified->resolved by role (Phase-1 proxy; attend is Phase 2)
  GET /api/events/{entity}/{id}     full audit timeline for one incident/ticket
  GET /api/reasons                  reason catalogue (labels), for reference
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from .. import clock, db
from ..core import events

bp = Blueprint("query", __name__)


def _cfg():
    return current_app.config["OPS_CFG"]


def _minutes(a_iso: str | None, b_iso: str | None) -> float | None:
    if not a_iso or not b_iso:
        return None
    return round((clock.parse(b_iso) - clock.parse(a_iso)).total_seconds() / 60, 1)


def _label(code: str | None) -> str:
    if not code or code == "unattributed":
        return "Unattributed"
    return _cfg().label(code, "en")


@bp.get("/api/overview")
def overview():
    since = clock.plus_seconds(-24 * 3600)
    open_inc = db.query_one("SELECT COUNT(*) n FROM incidents WHERE status IN ('open','resolving')")["n"]
    looms_down = db.query_one(
        "SELECT COUNT(DISTINCT asset_id) n FROM incidents WHERE status IN ('open','resolving')")["n"]
    open_tkt = db.query_one("SELECT COUNT(*) n FROM tickets WHERE status IN ('open','attended')")["n"]
    pending_prompts = db.query_one(
        "SELECT COUNT(*) n FROM escalations WHERE status='pending' AND action='ask_reason'")["n"]
    dt = db.query_one(
        "SELECT COALESCE(SUM(duration_s),0) s, COUNT(*) n, COALESCE(AVG(duration_s),0) a"
        " FROM incidents WHERE status='resolved' AND opened_at>=?", (since,))
    resolved_24h = db.query_one(
        "SELECT COUNT(*) n FROM incidents WHERE status='resolved' AND opened_at>=?", (since,))["n"]
    return jsonify({
        "now": clock.now_iso(),
        "open_incidents": open_inc,
        "looms_down": looms_down,
        "open_tickets": open_tkt,
        "pending_reason_prompts": pending_prompts,
        "downtime_minutes_24h": round((dt["s"] or 0) / 60, 1),
        "incidents_24h": resolved_24h,
        "mean_stop_minutes_24h": round((dt["a"] or 0) / 60, 1),
    })


@bp.get("/api/tickets/open")
def tickets_open():
    rows = db.query(
        "SELECT t.id, t.code, t.owner_role, t.opened_at, t.first_notified_at,"
        " t.reopen_count, t.status, a.asset_ref,"
        " (SELECT MAX(rung) FROM escalations e WHERE e.ticket_id=t.id AND e.status='fired') cur_rung"
        " FROM tickets t JOIN incidents i ON i.id=t.incident_id JOIN assets a ON a.id=i.asset_id"
        " WHERE t.status IN ('open','attended') ORDER BY t.opened_at",
    )
    out = []
    for r in rows:
        d = dict(r)
        d["label"] = _label(r["code"])
        d["minutes_open"] = _minutes(r["opened_at"], clock.now_iso())
        d["minutes_since_notified"] = _minutes(r["first_notified_at"], clock.now_iso())
        out.append(d)
    return jsonify({"tickets": out})


@bp.get("/api/incidents/open")
def incidents_open():
    rows = db.query(
        "SELECT i.id, i.opened_at, i.status, i.shift, a.asset_ref,"
        " (SELECT code FROM incident_reasons ir WHERE ir.incident_id=i.id ORDER BY ir.id DESC LIMIT 1) code"
        " FROM incidents i JOIN assets a ON a.id=i.asset_id"
        " WHERE i.status IN ('open','resolving') ORDER BY i.opened_at",
    )
    out = []
    for r in rows:
        d = dict(r)
        d["label"] = _label(r["code"]) if r["code"] else None
        d["minutes_down"] = _minutes(r["opened_at"], clock.now_iso())
        out.append(d)
    return jsonify({"incidents": out})


def _range():
    since = request.args.get("since") or clock.plus_seconds(-24 * 3600)
    until = request.args.get("until") or clock.now_iso()
    return since, until


@bp.get("/api/downtime")
def downtime():
    since, until = _range()
    latest_reason = (
        "(SELECT ir.code FROM incident_reasons ir WHERE ir.incident_id=i.id"
        " ORDER BY ir.id DESC LIMIT 1)")
    by_reason = db.query(
        f"SELECT COALESCE({latest_reason},'unattributed') code, COUNT(*) n,"
        f" COALESCE(SUM(i.duration_s),0) secs FROM incidents i"
        f" WHERE i.status='resolved' AND i.opened_at>=? AND i.opened_at<?"
        f" GROUP BY code ORDER BY secs DESC", (since, until))
    by_shift = db.query(
        "SELECT COALESCE(shift,'-') shift, COUNT(*) n, COALESCE(SUM(duration_s),0) secs"
        " FROM incidents WHERE status='resolved' AND opened_at>=? AND opened_at<?"
        " GROUP BY shift ORDER BY shift", (since, until))
    by_asset = db.query(
        "SELECT a.asset_ref, COUNT(*) n, COALESCE(SUM(i.duration_s),0) secs"
        " FROM incidents i JOIN assets a ON a.id=i.asset_id"
        " WHERE i.status='resolved' AND i.opened_at>=? AND i.opened_at<?"
        " GROUP BY a.asset_ref ORDER BY secs DESC LIMIT 15", (since, until))

    def shape(rows, key):
        return [{key: r[key], "count": r["n"], "minutes": round((r["secs"] or 0) / 60, 1),
                 **({"label": _label(r["code"])} if key == "code" else {})} for r in rows]

    return jsonify({
        "range": {"since": since, "until": until},
        "by_reason": shape(by_reason, "code"),
        "by_shift": shape(by_shift, "shift"),
        "by_asset": shape(by_asset, "asset_ref"),
    })


@bp.get("/api/response-times")
def response_times():
    since, until = _range()
    rows = db.query(
        "SELECT t.owner_role, t.first_notified_at, t.attended_at, t.closed_at, i.resolved_at"
        " FROM tickets t JOIN incidents i ON i.id=t.incident_id"
        " WHERE t.status='closed' AND t.opened_at>=? AND t.opened_at<?", (since, until))
    agg: dict[str, dict] = {}
    for r in rows:
        role = r["owner_role"]
        a = agg.setdefault(role, {"role": role, "count": 0,
                                  "notified_to_resolved": [], "notified_to_attended": []})
        a["count"] += 1
        ntr = _minutes(r["first_notified_at"], r["resolved_at"])
        if ntr is not None:
            a["notified_to_resolved"].append(ntr)
        nta = _minutes(r["first_notified_at"], r["attended_at"])
        if nta is not None:
            a["notified_to_attended"].append(nta)

    def summarize(vals):
        if not vals:
            return {"n": 0, "avg": None, "max": None}
        return {"n": len(vals), "avg": round(sum(vals) / len(vals), 1), "max": round(max(vals), 1)}

    out = []
    for a in agg.values():
        out.append({
            "role": a["role"], "count": a["count"],
            "notified_to_resolved": summarize(a["notified_to_resolved"]),
            "notified_to_attended": summarize(a["notified_to_attended"]),
        })
    return jsonify({
        "range": {"since": since, "until": until},
        "by_role": out,
        "note": "Phase 1 has no acknowledgement at the asset, so notified_to_attended is "
                "empty by design; notified_to_resolved is a proxy. Response vs repair time "
                "separates once Phase-2 panels populate attended_at.",
    })


@bp.get("/api/events/<entity>/<int:entity_id>")
def event_timeline(entity, entity_id):
    if entity not in ("incident", "ticket", "escalation"):
        return jsonify({"error": "unknown entity"}), 400
    return jsonify({"entity": entity, "entity_id": entity_id,
                    "events": events.timeline(entity, entity_id)})


@bp.get("/api/reasons")
def reasons():
    cfg = _cfg()
    return jsonify({"reasons": [
        {"code": c["code"], "label": cfg.label(c["code"], "en"),
         "ticketable": bool(c.get("ticketable")), "owner": c.get("owner"),
         "expected_minutes": c.get("expected_minutes")}
        for c in cfg.codes]})
