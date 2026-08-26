"""Live transaction log for a pilot run.

Tails the database — outbox, inbound_raw, events — and appends one human-readable line
per transaction to logs/pilot_run.log, labelling every outbound message by what it IS
and whether the person watching should reply. Written for running a live end-to-end
test against a real phone: the tester needs to know, the moment a message lands,
whether it is the fault question (reply with a button), the hours question (reply with
a number), or a reminder (no reply expected).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, db  # noqa: E402

LOG = Path(__file__).resolve().parent.parent / "logs" / "pilot_run.log"

cfg = config.load()
db.init(cfg.db_params(), cfg.table_prefix)

def emit(line):
    stamp = time.strftime("%H:%M:%S")
    text = f"[{stamp}] {line}"
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text, flush=True)

def label_outbound(payload):
    t = payload.get("type")
    if t == "reason_prompt":
        return ("FAULT QUESTION -> your phone  *** REPLY: tap a fault button or type "
                "its number ***")
    if t == "eta_request":
        return ("HOURS QUESTION -> your phone  *** REPLY: a number of hours, e.g. 1 *** "
                "(if it looks like the 'Attention... please attend' reminder on your "
                "phone, that is the placeholder template — still reply with hours)")
    if t == "escalation":
        return "REMINDER -> your phone  (no reply needed)"
    return f"message ({t}) -> recipient"

last_out = db.query_one("SELECT COALESCE(MAX(id),0) m FROM outbox")["m"]
last_in = db.query_one("SELECT COALESCE(MAX(id),0) m FROM inbound_raw")["m"]
last_ev = db.query_one("SELECT COALESCE(MAX(id),0) m FROM events")["m"]
failed_seen = set()

emit("=== pilot watch started (outbox/inbound/events from here on) ===")

EV_TEXT = {
    "opened": "incident OPENED",
    "reason_set": "REASON RECORDED",
    "notified": "notification recorded",
    "escalated": "ESCALATED",
    "prompted": "prompt recorded",
    "eta_set": "HOURS PROMISE RECORDED",
    "eta_missed": "PROMISE MISSED — counted against the promiser",
    "resolved": "incident RESOLVED (machine running again)",
    "closed": "ticket CLOSED",
    "reopened": "incident REOPENED (false restart)",
    "unrouted": "!!! NOBODY TO CALL !!!",
}

while True:
    try:
        for r in db.query(
                "SELECT * FROM outbox WHERE id>? ORDER BY id", (last_out,)):
            last_out = r["id"]
            p = json.loads(r["payload"]) or {}
            emit(f"QUEUED  {label_outbound(p)}  [msg queued to {r['recipient']}]")

        for r in db.query(
                "SELECT * FROM inbound_raw WHERE id>? ORDER BY id", (last_in,)):
            last_in = r["id"]
            try:
                body = json.loads(r["body"]) or {}
                text = body.get("message_in_raw") or body.get("message-in") or ""
            except Exception:
                text = (r["body"] or "")[:60]
            emit(f"REPLY RECEIVED from {r['sender']}: \"{text}\"")
        for r in db.query(
                "SELECT * FROM events WHERE id>? ORDER BY id", (last_ev,)):
            last_ev = r["id"]
            kind = EV_TEXT.get(r["kind"], r["kind"])
            detail = ""
            try:
                d = json.loads(r["detail"]) or {}
                bits = []
                if d.get("code"):
                    bits.append(f"fault={d['code']}")
                if d.get("hours") is not None:
                    bits.append(f"promised={d['hours']}h")
                if d.get("promised_hours") is not None:
                    bits.append(f"had promised={d['promised_hours']}h")
                if d.get("recipients"):
                    bits.append("to=" + ",".join(map(str, d["recipients"])))
                if d.get("close_reason"):
                    bits.append(f"why={d['close_reason']}")
                detail = ("  [" + " ".join(bits) + "]") if bits else ""
            except Exception:
                pass
            emit(f"EVENT   {r['entity']} {r['entity_id']}: {kind}{detail}")
        # delivery verdicts arrive by webhook AFTER acceptance — failures get their
        # own loud line the moment the provider reports one (rows are UPDATEd in
        # place, so track which ids we have already shouted about)
        for r in db.query(
                "SELECT id, recipient, delivery_error FROM outbox"
                " WHERE delivery_status='failed'"):
            if r["id"] not in failed_seen:
                failed_seen.add(r["id"])
                emit(f"!!! DELIVERY FAILED to {r['recipient']}: "
                     f"{r['delivery_error'] or '(no reason given)'} — that message "
                     f"NEVER ARRIVED on the phone !!!")
    except Exception as exc:  # never die mid-run; the log is the whole point
        emit(f"(watcher hiccup: {exc.__class__.__name__}: {exc})")
        time.sleep(5)
    time.sleep(2)
