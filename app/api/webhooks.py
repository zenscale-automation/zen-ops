"""Inbound webhooks — Phase-1 reply capture.

POST /webhook/whatsapp  — a supervisor's numbered reply to a reason prompt.
POST /webhook/gchat     — a Google Chat card action / reply.

Both write the message verbatim to inbound_raw FIRST, then parse (reply parsing will be
wrong at first; without the raw record those incidents cannot be reconstructed). The
reply is matched to the incident whose prompt it answers, the reason is set, and the
lifecycle converges inside core/incidents — the same path a Phase-2 panel would take.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

from flask import Blueprint, current_app, jsonify, request

from .. import clock, db
from ..core import incidents, prompts

bp = Blueprint("webhooks", __name__)


# --- signature verification ------------------------------------------------

def _verify(secret_env: str) -> bool:
    secret = os.environ.get(secret_env, "")
    if not secret:
        return True  # no secret configured (dev) => accept
    sig = request.headers.get("X-Signature", "")
    expected = hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


# --- reply -> incident matching -------------------------------------------

def _person_by_contact(cfg, channel: str, address: str) -> str | None:
    field = "whatsapp" if channel == "whatsapp" else "gchat_space"
    for pid, person in cfg.people.items():
        if person.get(field) and person[field] == address:
            return pid
    return None


def _incident_open_no_reason(incident_id: int) -> bool:
    r = db.query_one(
        "SELECT i.status, (SELECT COUNT(*) FROM incident_reasons ir"
        " WHERE ir.incident_id=i.id) rc FROM incidents i WHERE i.id=?",
        (incident_id,),
    )
    return bool(r and r["status"] in ("open", "resolving") and r["rc"] == 0)


def _find_incident(cfg, channel: str, address: str, context_msg_id: str | None,
                   asset_ref: str | None) -> int | None:
    # 1) explicit reply-to a known outbound message
    if context_msg_id:
        row = db.query_one("SELECT payload FROM outbox WHERE provider_msg_id=?",
                           (context_msg_id,))
        if row:
            iid = (json.loads(row["payload"]) or {}).get("incident_id")
            if iid and _incident_open_no_reason(iid):
                return iid
    # 2) explicit asset reference in the reply
    if asset_ref:
        aid = incidents.asset_id_for(cfg, asset_ref)
        row = db.query_one(
            "SELECT id FROM incidents WHERE asset_id=? AND status IN ('open','resolving')"
            " ORDER BY id DESC LIMIT 1",
            (aid,),
        )
        if row and _incident_open_no_reason(row["id"]):
            return row["id"]
    # 3) most recent reason prompt sent to this sender, still unanswered
    rows = db.query(
        "SELECT payload FROM outbox WHERE recipient=? AND channel=?"
        " ORDER BY id DESC LIMIT 20",
        (address, channel),
    )
    for r in rows:
        p = json.loads(r["payload"]) or {}
        if p.get("type") == "reason_prompt" and p.get("incident_id"):
            if _incident_open_no_reason(p["incident_id"]):
                return p["incident_id"]
    return None


def _handle(channel: str, sender: str, text: str, context_msg_id: str | None,
            asset_ref: str | None):
    cfg = current_app.config["OPS_CFG"]
    # record verbatim FIRST
    raw_id = db.execute(
        "INSERT INTO inbound_raw(channel, received_at, body) VALUES (?,?,?)",
        (channel, clock.now_iso(), request.get_data(as_text=True) or text),
    )

    incident_id = _find_incident(cfg, channel, sender, context_msg_id, asset_ref)
    parsed = prompts.parse(cfg, text)
    result = {"matched": False, "incident_id": incident_id, "code": None, "raw_id": raw_id}

    if incident_id and parsed:
        code, subcode = parsed
        actor = _person_by_contact(cfg, channel, sender) or sender
        incidents.set_reason(cfg, incident_id, code, subcode=subcode, method="reply", actor=actor)
        db.execute("UPDATE inbound_raw SET matched_incident_id=? WHERE id=?",
                   (incident_id, raw_id))
        result.update(matched=True, code=code, subcode=subcode, actor=actor)
    return jsonify(result), 200


@bp.post("/webhook/whatsapp")
def whatsapp():
    if not _verify("WHATSAPP_WEBHOOK_SECRET"):
        return jsonify({"error": "bad signature"}), 401
    data = request.get_json(silent=True) or {}
    sender = data.get("from") or data.get("sender") or ""
    text = str(data.get("text") or data.get("body") or "").strip()
    context_msg_id = (data.get("context") or {}).get("message_id") or data.get("context_id")
    asset_ref = data.get("asset_ref")
    return _handle("whatsapp", sender, text, context_msg_id, asset_ref)


@bp.post("/webhook/gchat")
def gchat():
    if not _verify("GCHAT_WEBHOOK_SECRET"):
        return jsonify({"error": "bad signature"}), 401
    data = request.get_json(silent=True) or {}
    sender = data.get("space") or data.get("from") or ""
    # a card button click carries its value; a typed message carries text
    text = str(
        (data.get("action") or {}).get("value")
        or data.get("text")
        or ""
    ).strip()
    asset_ref = data.get("asset_ref")
    return _handle("gchat", sender, text, None, asset_ref)
