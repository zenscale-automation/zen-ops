"""Escalation engine: ladder advance + recurrence override.

A pending escalation is a ROW with a due_at timestamp and a status (Rule 1 — timers are
rows, never in-memory countdowns), so a restart mid-incident resumes the countdown
instead of silently dropping it. The same table and the same fire() path serve two
kinds of timer:

  * the pre-ticket "unknown" ladder (action: ask_reason) — attached to an incident,
  * the per-reason ladder once a ticket exists — attached to the ticket.

The ticker calls due() then fire() for each; both "advance the same way — write the
next rung, queue the next notification".
"""

from __future__ import annotations

from .. import clock, config
from . import events, models, outbox, prompts, routing


# --- scheduling ------------------------------------------------------------

def _insert_rung(c, *, ticket_id, incident_id, rung, notify_role, action, due_at,
                 trigger) -> int:
    cur = c.execute(
        "INSERT INTO escalations"
        "(ticket_id, incident_id, rung, notify_role, action, due_at, fired_at,"
        " status, `trigger`) VALUES (?,?,?,?,?,?,NULL,'pending',?)",
        (ticket_id, incident_id, rung, notify_role, action, due_at, trigger),
    )
    return cur.lastrowid


def schedule_unknown(c, cfg: "config.Config", incident_row) -> None:
    """Start the Phase-1 'unknown' ladder against an incident with no reason yet."""
    ladder = cfg.unknown_ladder
    if not ladder:
        return
    r0 = ladder[0]
    due = clock.plus_minutes(r0["after_minutes"], base=clock.parse(incident_row["opened_at"]))
    _insert_rung(
        c, ticket_id=None, incident_id=incident_row["id"], rung=0,
        notify_role=r0["notify"], action=r0.get("action"), due_at=due, trigger="timer",
    )


def compute_start(c, cfg: "config.Config", asset_id: str, code: str) -> tuple[int, str]:
    """Recurrence override: same asset + same code, threshold occurrences within
    window_hours -> jump straight to a higher rung. Returns (start_rung, trigger)."""
    rec = cfg.recurrence
    if not rec or rec.get("action") != "skip_to_rung":
        return 0, "timer"
    window_h = float(rec.get("window_hours", 8))
    threshold = int(rec.get("threshold", 3))
    since = clock.plus_seconds(-window_h * 3600)
    row = c.execute(
        "SELECT COUNT(*) n FROM tickets t JOIN incidents i ON i.id=t.incident_id"
        " WHERE i.asset_id=? AND t.code=? AND t.opened_at>=?",
        (asset_id, code, since),
    ).fetchone()
    prior = row["n"] if row else 0
    # `prior` ALREADY includes the ticket just inserted by incidents._open_ticket —
    # that INSERT and this SELECT share one cursor inside one transaction. Adding 1
    # here counted the current occurrence twice and fired the override a fault early.
    if prior >= threshold:  # this occurrence is the Nth
        return int(rec.get("rung", 0)), "recurrence"
    return 0, "timer"


def start_ladder(c, cfg: "config.Config", ticket_row, start_rung: int = 0,
                 trigger: str = "timer") -> None:
    """Create the first pending rung for a ticket's reason ladder."""
    ladder = cfg.ladder_for(ticket_row["code"])
    if not ladder:
        return
    start_rung = min(start_rung, len(ladder) - 1)
    rung = ladder[start_rung]
    if trigger == "recurrence":
        due = clock.now_iso()  # escalate immediately, don't wait out the timer
    else:
        due = clock.plus_minutes(rung["after_minutes"], base=clock.parse(ticket_row["opened_at"]))
    _insert_rung(
        c, ticket_id=ticket_row["id"], incident_id=None, rung=start_rung,
        notify_role=rung["notify"], action=rung.get("action"), due_at=due, trigger=trigger,
    )


def _schedule_next(c, cfg: "config.Config", esc_row, base_iso: str, ladder: list) -> None:
    nxt = esc_row["rung"] + 1
    if nxt >= len(ladder):
        return
    rung = ladder[nxt]
    due = clock.plus_minutes(rung["after_minutes"], base=clock.parse(base_iso))
    _insert_rung(
        c, ticket_id=esc_row["ticket_id"], incident_id=esc_row["incident_id"], rung=nxt,
        notify_role=rung["notify"], action=rung.get("action"), due_at=due, trigger="timer",
    )


# --- cancellation ----------------------------------------------------------

def cancel_for_incident(c, incident_id: int) -> None:
    c.execute(
        "UPDATE escalations SET status='cancelled'"
        " WHERE incident_id=? AND status='pending'",
        (incident_id,),
    )


def cancel_for_ticket(c, ticket_id: int) -> None:
    c.execute(
        "UPDATE escalations SET status='cancelled'"
        " WHERE ticket_id=? AND status='pending'",
        (ticket_id,),
    )


# --- firing ----------------------------------------------------------------

def fire(c, cfg: "config.Config", esc_row) -> None:
    """Act on one due escalation: queue its notification(s), mark it fired, and write
    the next rung. Runs inside the ticker's transaction."""
    now = clock.now_iso()
    is_ticket = esc_row["ticket_id"] is not None

    if is_ticket:
        ticket = c.execute("SELECT * FROM tickets WHERE id=?", (esc_row["ticket_id"],)).fetchone()
        if ticket is None or ticket["status"] == "closed":
            c.execute("UPDATE escalations SET status='cancelled' WHERE id=?", (esc_row["id"],))
            return
        incident = c.execute("SELECT * FROM incidents WHERE id=?", (ticket["incident_id"],)).fetchone()
        ladder = cfg.ladder_for(ticket["code"])
        base_iso = ticket["opened_at"]
        owner_role = ticket["owner_role"]
        code = ticket["code"]
    else:
        incident = c.execute("SELECT * FROM incidents WHERE id=?", (esc_row["incident_id"],)).fetchone()
        ladder = cfg.unknown_ladder
        base_iso = incident["opened_at"] if incident else None
        owner_role = None
        code = None
        ticket = None

    # If the asset has resumed (or is inside its resolve grace), don't page anyone.
    if incident is None or incident["status"] in ("resolving", "resolved"):
        c.execute("UPDATE escalations SET status='cancelled' WHERE id=?", (esc_row["id"],))
        return

    asset = c.execute("SELECT * FROM assets WHERE id=?", (incident["asset_id"],)).fetchone()
    asset_ref = asset["asset_ref"] if asset else incident["asset_id"]

    recipients = routing.resolve(cfg, esc_row["notify_role"], when_iso=now,
                                 owner_role=owner_role,
                                 for_prompt=(esc_row["action"] == "ask_reason"))
    action = esc_row["action"]

    # Build the payload once; recipients differ only by address.
    if action == "ask_reason":
        text = prompts.render(cfg, asset_ref, incident["opened_at"])
        base_payload = {
            "type": "reason_prompt",
            "text": text,
            "asset_ref": asset_ref,
            "incident_id": incident["id"],
            "rung": esc_row["rung"],
            "options": prompts.options(cfg),
        }
        event_kind = models.K_PROMPTED
    else:
        minutes = max(1, round((clock.now() - clock.parse(base_iso)).total_seconds() / 60))
        asset_disp = asset_ref.replace("_", " ").title()
        if code:
            reason_label = cfg.label(code, "en")
            if esc_row["rung"] == 0:
                text = f"{asset_disp} down — {reason_label}. Please attend ({minutes} min so far)."
            else:
                text = (f"{asset_disp} still down — {reason_label}, {minutes} min. "
                        f"Please step in.")
        else:
            text = (f"{asset_disp} has been down {minutes} min with no reason given. "
                    f"Please check.")
        base_payload = {
            "type": "escalation",
            "text": text,
            "asset_ref": asset_ref,
            "incident_id": incident["id"],
            "ticket_id": esc_row["ticket_id"],
            "rung": esc_row["rung"],
            "reason_code": code,
        }
        event_kind = models.K_NOTIFIED if esc_row["rung"] == 0 else models.K_ESCALATED

    # Queue one message per recipient (or a single 'log' message if nobody resolved).
    if not recipients:
        recipients = [routing.Recipient(person_id="unrouted", name="unrouted",
                                        channel="log", address="unrouted")]
    entity_tag = f"tkt:{esc_row['ticket_id']}" if is_ticket else f"inc:{esc_row['incident_id']}"
    for rcpt in recipients:
        payload = dict(base_payload, to_name=rcpt.name, to_role=esc_row["notify_role"])
        dedupe = f"{entity_tag}:rung:{esc_row['rung']}:{rcpt.person_id}"
        outbox.enqueue(c, rcpt.channel, rcpt.address, payload, dedupe, at=now)

    # Mark fired, log, and set first_notified_at on the ticket's first page.
    c.execute("UPDATE escalations SET status='fired', fired_at=? WHERE id=?", (now, esc_row["id"]))
    if is_ticket and ticket["first_notified_at"] is None and action != "ask_reason":
        c.execute("UPDATE tickets SET first_notified_at=? WHERE id=?", (now, ticket["id"]))

    entity = "ticket" if is_ticket else "incident"
    entity_id = esc_row["ticket_id"] if is_ticket else esc_row["incident_id"]
    events.log(c, entity, entity_id, event_kind, actor="system",
               detail={"rung": esc_row["rung"], "role": esc_row["notify_role"],
                       "recipients": [r.person_id for r in recipients],
                       "trigger": esc_row["trigger"]},
               department=cfg.department, at=now)

    _schedule_next(c, cfg, esc_row, base_iso, ladder)
