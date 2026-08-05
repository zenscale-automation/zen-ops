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
from ..notifiers.whatsapp import normalise_msisdn

bp = Blueprint("webhooks", __name__)


# --- signature verification ------------------------------------------------

def _verify(secret_env: str) -> bool:
    secret = os.environ.get(secret_env, "")
    if not secret:
        return True  # no secret configured (dev) => accept
    sig = request.headers.get("X-Signature", "")
    expected = hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _verify_meta() -> bool:
    """Meta signs every webhook with the APP SECRET as `X-Hub-Signature-256:
    sha256=<hex>` over the raw body. Note it is the app secret, not the verify token —
    the verify token is only used once, for the subscription handshake below."""
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not secret:
        return True  # unset (dev) => accept, same posture as _verify
    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[7:], expected)


def _extract_meta_messages(data: dict) -> list[dict]:
    """Flatten Meta's envelope to the messages we care about.

    entry[].changes[].value carries either `messages` (someone wrote to us) or
    `statuses` (delivery receipts for what we sent). Only the former is a reply; the
    latter must still return 200 or Meta retries and eventually disables the webhook.
    """
    out: list[dict] = []
    for entry in data.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for msg in value.get("messages", []) or []:
                out.append(msg)
    return out


def _meta_reply_text(msg: dict) -> str:
    """A reply arrives as plain text, a quick-reply button, or an interactive list
    selection depending on how the person answered. All three must parse."""
    kind = msg.get("type")
    if kind == "text":
        return str((msg.get("text") or {}).get("body") or "").strip()
    if kind == "button":
        return str((msg.get("button") or {}).get("text") or "").strip()
    if kind == "interactive":
        inter = msg.get("interactive") or {}
        for key in ("button_reply", "list_reply"):
            if key in inter:
                # the id is what we set when building the options; prefer it over the
                # display title, which is localised and may not parse to a digit
                return str(inter[key].get("id") or inter[key].get("title") or "").strip()
    return ""


# --- reply -> incident matching -------------------------------------------

def _person_by_contact(cfg, channel: str, address: str) -> str | None:
    """routing.yaml stores "+919000000001"; Meta sends "919000000001". Comparing the
    raw strings never matches, so every reply would be recorded against a phone number
    instead of a person."""
    field = "whatsapp" if channel == "whatsapp" else "gchat_space"
    want = normalise_msisdn(address) if channel == "whatsapp" else address
    for pid, person in cfg.people.items():
        have = person.get(field)
        if not have:
            continue
        if channel == "whatsapp":
            if normalise_msisdn(have) == want:
                return pid
        elif have == address:
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
        "SELECT payload FROM outbox WHERE (recipient=? OR recipient=?) AND channel=?"
        " ORDER BY id DESC LIMIT 20",
        (address, "+" + address if channel == "whatsapp" else address, channel),
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
    # record verbatim FIRST, with the sender normalised — this row is what opens the
    # 24-hour service window the notifier checks before choosing free text vs template
    stored_sender = normalise_msisdn(sender) if channel == "whatsapp" else sender
    raw_id = db.execute(
        "INSERT INTO inbound_raw(channel, received_at, body, sender) VALUES (?,?,?,?)",
        (channel, clock.now_iso(), request.get_data(as_text=True) or text, stored_sender),
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


@bp.get("/webhook/whatsapp")
def whatsapp_verify():
    """Meta's one-time subscription handshake: it GETs with a verify token of our
    choosing and expects hub.challenge echoed back as plain text."""
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and expected and token == expected:
        return challenge, 200, {"Content-Type": "text/plain"}
    return jsonify({"error": "verification failed"}), 403


@bp.post("/webhook/whatsapp")
def whatsapp():
    if not _verify_meta():
        return jsonify({"error": "bad signature"}), 401
    data = request.get_json(silent=True) or {}
    messages = _extract_meta_messages(data)
    if not messages:
        # delivery receipt, read receipt, or a status change. Acknowledge it — a non-200
        # makes Meta retry and eventually disable the subscription.
        return jsonify({"ignored": True}), 200

    result = None
    for msg in messages:
        sender = str(msg.get("from") or "")
        text = _meta_reply_text(msg)
        context_msg_id = (msg.get("context") or {}).get("id")
        result = _handle("whatsapp", sender, text, context_msg_id, None)
    return result


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
