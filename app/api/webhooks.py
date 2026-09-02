"""Inbound webhooks — Phase-1 reply capture.

POST /webhook/whatsapp  — a supervisor's numbered reply to a reason prompt.
POST /webhook/gchat     — a Google Chat card action / reply.

Both write the message verbatim to inbound_raw FIRST, then parse (reply parsing will be
wrong at first; without the raw record those incidents cannot be reconstructed). The
reply is matched to the incident whose prompt it answers, the reason is set, and the
lifecycle converges inside core/incidents — the same path a Phase-2 panel would take.
"""

from __future__ import annotations

import logging

import hashlib
import hmac
import json
import os

from flask import Blueprint, current_app, jsonify, request

from .. import clock, db
from ..core import escalation, events, incidents, outbox, prompts
from ..notifiers.whatsapp import normalise_msisdn

bp = Blueprint("webhooks", __name__)

log = logging.getLogger("ops.webhooks")


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


def _is_delivery_receipt(data: dict) -> bool:
    """A receipt for something WE sent, in a shape we recognise.

    Deliberately narrow. Anything not positively identified as a receipt is treated as a
    possible human reply and stored, because the cost of an extra row is nothing and the
    cost of losing somebody's answer is that they get asked again and then escalated
    about.
    """
    if not isinstance(data, dict):
        return False
    # Meta: entry[].changes[].value.statuses with no messages alongside.
    for entry in data.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            if value.get("statuses") and not value.get("messages"):
                return True
    # PickyAssist echoes our own outbound back with direction != 0.
    if "number" in data and str(data.get("direction", 0)) not in ("0", "None", ""):
        return True
    return False


def _pickyassist_message(data: dict) -> dict | None:
    """PickyAssist posts one flat message, not Meta's entry[].changes[].value envelope.

    Shape (verified against their documentation):
      {"number": "919446XXXXXX", "message-in": "2", "message_in_raw": "2",
       "type": 1, "application": 8, "unique-id": "70946012", "direction": 0}
    and for a tapped quick-reply button, additionally:
      {"interactive": {"type": 2, "id": "<the button id we sent>", "description": null}}

    Returns None for anything that is not an inbound message from a person — delivery
    receipts and echoes of our own outbound both arrive here too, and answering them
    as replies would set a reason nobody gave.
    """
    if not isinstance(data, dict) or "number" not in data:
        return None
    # direction 0 is inbound. Anything else is our own message coming back.
    if str(data.get("direction", 0)) not in ("0", "None", ""):
        return None

    interactive = data.get("interactive") or {}
    # A button tap carries the id we set when sending. Prefer it over the label: the
    # label is display text that may be translated or truncated, the id is ours.
    text = ""
    if isinstance(interactive, dict) and interactive.get("id") is not None:
        text = str(interactive["id"])
    if not text:
        text = str(data.get("message_in_raw") or data.get("message-in") or "")
    return {
        "sender": str(data.get("number") or ""),
        "text": text.strip(),
        "provider_id": str(data.get("unique-id") or ""),
        # WhatsApp attaches the id of the message being replied to, and PickyAssist
        # passes it through as context-msg-id — the same value we stored as
        # provider_msg_id when we sent the question. It turns "which question is this
        # answering?" from a guess into a lookup.
        "context_msg_id": str(data.get("context-msg-id") or "") or None,
    }


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


def _question_from_context(context_msg_id: str | None) -> dict | None:
    """The exact question this reply was attached to, or None if we do not recognise it.

    WhatsApp tells us which message a person replied to. When we sent that message we
    stored its provider id, so this is an exact join rather than the "most recent
    outstanding question" guess below — and the guess is genuinely ambiguous: the reason
    menu and the hours estimate are both answered with a bare number, and a supervisor
    with two stopped looms has two identical-looking questions on one screen.
    """
    if not context_msg_id:
        return None
    row = db.query_one("SELECT payload FROM outbox WHERE provider_msg_id=?",
                       (str(context_msg_id),))
    if not row:
        return None
    return json.loads(row["payload"]) or {}


def _answer_target(question: dict) -> dict | None:
    """What a reply to this specific question may still change — None once it is answered.

    This is where "only the first answer counts" is enforced. A second reply to a
    question that already has its answer must change nothing: not the original (the
    first answer is the record), and above all not some OTHER incident, which is what
    falling back to the most-recent-question guess would do.
    """
    kind = question.get("type")
    if kind == "eta_request" and question.get("ticket_id"):
        t = db.query_one("SELECT status, eta_due_at FROM tickets WHERE id=?",
                         (question["ticket_id"],))
        # Any number on an OPEN ticket counts, including one that replaces a promise
        # still running. Refusing it silently is what turned a fitter revising "2" to
        # "6" into a defaulter four hours later.
        if t and t["status"] != "closed":
            return {"kind": "eta", "ticket_id": question["ticket_id"]}
        return None
    if kind == "reason_prompt" and question.get("incident_id"):
        if _incident_open_no_reason(question["incident_id"]):
            return {"kind": "reason", "incident_id": question["incident_id"]}
        return None
    return None


def _last_question_to(sender: str, channel: str) -> dict | None:
    """The most recent outstanding question we asked this person, so a bare number in
    their reply answers THAT — the reason menu and the hours estimate are both numeric,
    and guessing between them by content alone is impossible.

    Most-recent-wins mirrors how the person experiences their own chat: the question at
    the bottom of the screen is the one they are answering.
    """
    # Only QUESTIONS are candidates. Scanning the last N messages of any kind meant a
    # live question aged out of the window as reminders piled on top of it: one ticket
    # here accumulated 170 hourly reminders above an unanswered estimate question, so a
    # perfectly good "6" matched nothing and the person kept being chased for the answer
    # they had just given.
    #
    # Same both-forms match as _find_incident strategy 3: the outbox stores the roster's
    # "+91..." form while providers report bare digits, and comparing them raw never hits.
    rows = db.query(
        "SELECT payload FROM outbox WHERE (recipient=? OR recipient=?) AND channel=?"
        " AND (payload LIKE ? OR payload LIKE ?)"
        " ORDER BY id DESC LIMIT 20",
        (sender, "+" + sender if channel == "whatsapp" else sender, channel,
         '%"eta_request"%', '%"reason_prompt"%'),
    )
    for r in rows:
        p = json.loads(r["payload"]) or {}
        if p.get("type") == "eta_request" and p.get("ticket_id"):
            t = db.query_one("SELECT status, eta_due_at FROM tickets WHERE id=?",
                             (p["ticket_id"],))
            # An open ticket can always take a number: a first estimate, or a revision
            # of one that is still running.
            if t and t["status"] != "closed":
                return {"kind": "eta", "ticket_id": p["ticket_id"]}
        if p.get("type") == "reason_prompt" and p.get("incident_id"):
            if _incident_open_no_reason(p["incident_id"]):
                return {"kind": "reason", "incident_id": p["incident_id"]}
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
    # 3) most recent reason prompt sent to this sender, still unanswered. Filtered to
    # prompts for the same reason as _last_question_to: reminders must not push a live
    # question out of the search window.
    rows = db.query(
        "SELECT payload FROM outbox WHERE (recipient=? OR recipient=?) AND channel=?"
        " AND payload LIKE ?"
        " ORDER BY id DESC LIMIT 20",
        (address, "+" + address if channel == "whatsapp" else address, channel,
         '%"reason_prompt"%'),
    )
    for r in rows:
        p = json.loads(r["payload"]) or {}
        if p.get("type") == "reason_prompt" and p.get("incident_id"):
            if _incident_open_no_reason(p["incident_id"]):
                return p["incident_id"]
    return None


def _record_raw(channel: str, body: str, sender: str = "") -> int:
    """Store the delivery verbatim, before anything decides whether it is interesting.

    Reply parsing will be wrong sometimes — a new provider, a changed shape, a reply we
    did not anticipate. Without the raw row those incidents cannot be reconstructed, and
    "the supervisor says they answered" becomes unanswerable. So this runs first, for
    every delivery, including the ones that turn out to be delivery receipts.

    The normalised sender is what opens the 24-hour service window the notifier checks
    before choosing free text over a template.
    """
    stored_sender = normalise_msisdn(sender) if channel == "whatsapp" else sender
    return db.execute(
        "INSERT INTO inbound_raw(channel, received_at, body, sender) VALUES (?,?,?,?)",
        (channel, clock.now_iso(), body, stored_sender),
    )


def _incident_resolved(incident_id) -> bool:
    if not incident_id:
        return False
    row = db.query_one("SELECT status FROM incidents WHERE id=?", (incident_id,))
    return bool(row and row["status"] == "resolved")


def _reason_label(cfg, asked: dict) -> str | None:
    iid = asked.get("incident_id")
    if not iid:
        return None
    row = db.query_one("SELECT code FROM incident_reasons WHERE incident_id=?"
                       " ORDER BY id LIMIT 1", (iid,))
    return cfg.label(row["code"]) if row else None


def _asset_of(*, incident_id=None, ticket_id=None) -> str:
    """The loom an answer was about, for saying it back to the person."""
    if ticket_id and not incident_id:
        row = db.query_one("SELECT incident_id FROM tickets WHERE id=?", (ticket_id,))
        incident_id = row["incident_id"] if row else None
    if not incident_id:
        return ""
    row = db.query_one("SELECT a.asset_ref FROM incidents i JOIN assets a ON a.id=i.asset_id"
                       " WHERE i.id=?", (incident_id,))
    return row["asset_ref"] if row else ""


def _say(channel: str, sender: str, text: str, raw_id: int | None) -> None:
    """Answer the person who just messaged us.

    Every reply used to be met with silence, which reads exactly like the message never
    arriving — so somebody who answers correctly and is then chased anyway concludes the
    system is not listening and stops answering. One line back is what makes the whole
    loop believable.

    Keyed on the inbound row id, so a provider that redelivers the same reply does not
    thank anybody twice.
    """
    if not text or raw_id is None:
        return
    address = sender
    if channel == "whatsapp" and not address.startswith("+"):
        address = "+" + address
    try:
        with db.transaction() as c:
            outbox.enqueue(c, channel, address, {"type": "ack", "text": text},
                           f"ack:{raw_id}")
    except Exception:      # an acknowledgement must never break recording the answer
        log.exception("could not queue an acknowledgement to %s", sender)


def _handle_pass(cfg, channel: str, sender: str, question, asset_ref, raw_id):
    """"Not me." Escalate now instead of waiting out the timer.

    A person who cannot take a job had no way to say so: they could stay silent and be
    chased, or answer dishonestly. Both are worse than telling the system to find
    somebody else, which is what this does — it brings the next rung forward to now, so
    the ladder moves on within one tick rather than after another 30 or 60 minutes.
    """
    ticket_id = incident_id = None
    if question:
        ticket_id = question.get("ticket_id")
        incident_id = question.get("incident_id")
    if not ticket_id and not incident_id and asset_ref:
        incident_id = _find_incident(cfg, channel, sender, None, asset_ref)
    if not ticket_id and not incident_id:
        _say(channel, sender,
             "Nothing is waiting on you right now — nothing to hand over.", raw_id)
        return jsonify({"matched": False, "reason": "pass with nothing pending",
                        "raw_id": raw_id}), 200

    now = clock.now_iso()
    where = "ticket_id=?" if ticket_id else "incident_id=?"
    with db.transaction() as c:
        n = c.execute(
            f"UPDATE escalations SET due_at=? WHERE {where} AND status='pending'"
            " AND due_at>?", (now, ticket_id or incident_id, now)).rowcount
        events.log(c, "ticket" if ticket_id else "incident",
                   ticket_id or incident_id, "passed",
                   actor=_person_by_contact(cfg, channel, sender) or sender,
                   detail={"brought_forward": n}, department=cfg.department, at=now)

    aref = asset_ref or _asset_of(incident_id=incident_id, ticket_id=ticket_id)
    label = (aref or "").replace("_", " ").title()
    _say(channel, sender,
         f"Passed on. {label} goes to the next person now." if label
         else "Passed on. It goes to the next person now.", raw_id)
    return jsonify({"matched": True, "kind": "pass", "ticket_id": ticket_id,
                    "incident_id": incident_id, "raw_id": raw_id}), 200


def _handle(channel: str, sender: str, text: str, context_msg_id: str | None,
            asset_ref: str | None, raw_id: int | None = None):
    cfg = current_app.config["OPS_CFG"]
    if raw_id is None:
        raw_id = _record_raw(channel, request.get_data(as_text=True) or text, sender)
    else:
        # The route already stored the body; attach the sender now that it is parsed, so
        # the service-window lookup can find it.
        stored_sender = normalise_msisdn(sender) if channel == "whatsapp" else sender
        if stored_sender:
            db.execute("UPDATE inbound_raw SET sender=? WHERE id=?", (stored_sender, raw_id))

    # A bare number answers whichever question this person was asked last. The hours
    # estimate is checked FIRST because it is the newer question by construction — it is
    # only ever sent after they have already answered the reason menu.
    stored_sender = normalise_msisdn(sender) if channel == "whatsapp" else sender

    # "91 1" — the person naming which loom they are answering about. Two looms
    # stopping within ten minutes is routine, and both questions then look identical on
    # one screen; this is the only thing they can type that removes the doubt. It is
    # stripped before parsing so the rest of the reply is just the answer.
    named_asset, text = prompts.split_asset(cfg, text)
    if named_asset:
        asset_ref = named_asset

    # If the person used WhatsApp's reply-to, we know EXACTLY which question this
    # answers and no guessing is needed or wanted.
    asked = _question_from_context(context_msg_id)
    if asked is not None:
        target = _answer_target(asked)
        if target is None:
            # We recognise the message and it is already answered (or its incident is
            # closed). Ignoring it is the whole point: the alternative — falling through
            # to "their most recent outstanding question" — would take a duplicate tap
            # on an old message and record it against an unrelated loom.
            log.info("reply from %s answers an already-answered question (%s) — ignored",
                     sender, asked.get("type"))
            # Say so rather than going quiet: from the phone, an ignored answer and a
            # lost answer look the same.
            aref = _asset_of(incident_id=asked.get("incident_id"),
                             ticket_id=asked.get("ticket_id"))
            if aref:
                if _incident_resolved(asked.get("incident_id")):
                    _say(channel, sender, prompts.ack_already_running(aref), raw_id)
                else:
                    _say(channel, sender,
                         prompts.ack_already_answered(aref, _reason_label(cfg, asked)),
                         raw_id)
            return jsonify({"matched": False, "reason": "already answered",
                            "raw_id": raw_id}), 200
        question = target
    else:
        question = _last_question_to(stored_sender, channel)

    # A loom named in the reply beats every inference. If they wrote "92 1" while
    # replying to loom 91's message, they mean 92.
    if named_asset:
        by_name = _find_incident(cfg, channel, sender, None, named_asset)
        if by_name and (not question or question.get("incident_id") != by_name):
            question = {"kind": "reason", "incident_id": by_name}

    if prompts.is_pass(text):
        return _handle_pass(cfg, channel, sender, question, asset_ref, raw_id)

    if question and question["kind"] == "eta":
        hours = prompts.parse_eta(text)
        if hours is not None:
            actor = _person_by_contact(cfg, channel, sender) or sender
            res = escalation.set_eta(cfg, question["ticket_id"], hours, actor=actor)
            if res.get("ok"):
                db.execute("UPDATE inbound_raw SET matched_ticket_id=? WHERE id=?",
                           (question["ticket_id"], raw_id))
                _say(channel, sender,
                     prompts.ack_eta(_asset_of(ticket_id=question["ticket_id"]),
                                     hours, res["due_at"],
                                     revised=bool(res.get("revised"))), raw_id)
                return jsonify({"matched": True, "kind": "eta",
                                "ticket_id": question["ticket_id"],
                                "hours": hours, "due_at": res["due_at"],
                                "raw_id": raw_id}), 200
        # Not a parseable estimate: fall through — it may be a reason for a DIFFERENT
        # incident, and eating it here would lose that answer.

    if question and question["kind"] == "reason":
        incident_id = question["incident_id"]
    else:
        incident_id = _find_incident(cfg, channel, sender, context_msg_id, asset_ref)
    parsed = prompts.parse(cfg, text)
    if text and not parsed:
        # Loud on purpose. An unparsed reply means a human answered and the system did
        # not hear them — they will be asked again, then their manager will be escalated
        # to, for a fault that was reported. When the first real reply arrives in a shape
        # we did not anticipate, this line is how anyone finds out.
        log.warning("unparsed %s reply from %s: %r — nobody's answer was recorded",
                    channel, sender, text[:120])
    result = {"matched": False, "incident_id": incident_id, "code": None, "raw_id": raw_id}

    if incident_id and parsed:
        code, subcode = parsed
        actor = _person_by_contact(cfg, channel, sender) or sender
        incidents.set_reason(cfg, incident_id, code, subcode=subcode, method="reply", actor=actor)
        db.execute("UPDATE inbound_raw SET matched_incident_id=? WHERE id=?",
                   (incident_id, raw_id))
        result.update(matched=True, code=code, subcode=subcode, actor=actor)
        _say(channel, sender,
             prompts.ack_reason(_asset_of(incident_id=incident_id), cfg.label(code),
                                bool(cfg.is_ticketable(code))), raw_id)
    elif text and not parsed:
        # The reply nobody could read. Repeating the menu costs one message and saves
        # the re-ask, the escalation, and the person's belief that answering works.
        _say(channel, sender,
             prompts.ack_unparsed(cfg, text, _asset_of(incident_id=incident_id)), raw_id)
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
    """Inbound WhatsApp, from whichever provider is in front of us.

    PickyAssist is what sends today; the Meta envelope is still parsed because the
    account exists and a stray delivery from it must not 500. Which one it is is decided
    by the shape of the body, not by configuration — a provider switch should not be able
    to silently disconnect replies, which is exactly what happened when the notifier was
    rewritten and this file was not.
    """
    if not _verify_meta():
        return jsonify({"error": "bad signature"}), 401
    data = request.get_json(silent=True) or {}
    if not data and request.form:
        data = request.form.to_dict()      # some providers post form-encoded

    # Record VERBATIM before anything decides the delivery is uninteresting — but not
    # delivery receipts, which arrive for every message we send and would bury the human
    # replies this table exists to preserve. The distinction that matters is RECOGNISED:
    # a receipt we understand is dropped, anything we do not understand is kept, because
    # an unparseable reply is precisely the case somebody will need to reconstruct.
    raw_id = None
    if not _is_delivery_receipt(data):
        raw_id = _record_raw("whatsapp", request.get_data(as_text=True) or "")

    picky = _pickyassist_message(data)
    if picky:
        return _handle("whatsapp", picky["sender"], picky["text"],
                       picky.get("context_msg_id"), None, raw_id=raw_id)

    messages = _extract_meta_messages(data)
    if not messages:
        # A delivery receipt, a read receipt, or an echo. Acknowledge it — a non-200
        # makes providers retry and eventually disable the subscription.
        return jsonify({"ignored": True, "raw_id": raw_id}), 200

    result = None
    for msg in messages:
        sender = str(msg.get("from") or "")
        text = _meta_reply_text(msg)
        context_msg_id = (msg.get("context") or {}).get("id")
        result = _handle("whatsapp", sender, text, context_msg_id, None, raw_id=raw_id)
    return result


@bp.post("/webhook/events")
def provider_events():
    """PickyAssist's Event Webhook — delivery reports (event_id 1).

    The push API answers 100 when a message is QUEUED; Meta's verdict lands here later.
    Without this, a message that dies after acceptance is invisible outside a web panel:
    the outbox says sent, health says ok, and nobody is called. A failure report flips
    the outbox row to 'failed', which the existing outbox_failed_1h health metric counts
    — so delivery breakage surfaces programmatically, not by someone reading a log.
    """
    data = request.get_json(silent=True) or {}
    if str(data.get("event_id")) != "1":
        return jsonify({"ignored": True}), 200

    updated = failed = 0
    for row in (data.get("data") or []):
        msg_id = str(row.get("msg_id") or "")
        if not msg_id:
            continue
        status = str(row.get("status"))
        # 1=Delivered, 3=Read count as delivered; 2=Failed, 5=Refunded as failed;
        # 0/4 are interim and recorded as-is without judgement.
        verdict = {"1": "delivered", "3": "read", "2": "failed",
                   "5": "failed", "4": "submitted", "0": "unknown"}.get(status, status)
        error = (str(row.get("error_message") or "")[:255]) or None
        # db.execute returns lastrowid (0 for UPDATEs) — rowcount needs the cursor.
        with db.transaction() as c:
            if verdict == "failed":
                # status='failed' is what outbox.failed_since counts, and next_try_at is
                # the timestamp that metric windows on — repurposed here as "failed at".
                n = c.execute(
                    "UPDATE outbox SET delivery_status=?, delivery_error=?,"
                    " status='failed', next_try_at=? WHERE provider_msg_id=?",
                    (verdict, error, clock.now_iso(), msg_id)).rowcount
                if n:
                    failed += 1
                    log.error("delivery FAILED for provider msg %s: %s — the recipient "
                              "did not get this message", msg_id,
                              error or "(no reason given)")
            else:
                n = c.execute(
                    "UPDATE outbox SET delivery_status=? WHERE provider_msg_id=?",
                    (verdict, msg_id)).rowcount
        updated += 1 if n else 0
    return jsonify({"ok": True, "matched": updated, "failed": failed}), 200


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
