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

import json
import logging

from .. import clock, config, db
from . import events, models, outbox, prompts, routing

log = logging.getLogger("ops.escalation")


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


def _minutes_to_next_step(ladder: list, rung: int) -> int:
    """How long the person actually has before the next thing happens.

    Derived from the ladder rather than a separate config key, so the promise in the
    message and the timer behind it cannot disagree. Past the last rung, a repeating
    ladder keeps asking on its own interval; a finite one has nothing after this, and
    saying so honestly beats inventing a deadline.
    """
    try:
        here = int(ladder[rung].get("after_minutes", 0))
    except (IndexError, TypeError, ValueError):
        return 0
    if rung + 1 < len(ladder):
        try:
            return max(1, int(ladder[rung + 1].get("after_minutes", 0)) - here)
        except (TypeError, ValueError):
            return 0
    last = ladder[-1] if ladder else {}
    every = last.get("repeat_every_minutes") if isinstance(last, dict) else None
    try:
        return max(1, int(float(every))) if every else 0
    except (TypeError, ValueError):
        return 0


def _schedule_next(c, cfg: "config.Config", esc_row, base_iso: str, ladder: list) -> None:
    nxt = esc_row["rung"] + 1
    if nxt < len(ladder):
        rung = ladder[nxt]
        due = clock.plus_minutes(rung["after_minutes"], base=clock.parse(base_iso))
        _insert_rung(
            c, ticket_id=esc_row["ticket_id"], incident_id=esc_row["incident_id"],
            rung=nxt, notify_role=rung["notify"], action=rung.get("action"),
            due_at=due, trigger="timer",
        )
        return

    # Past the last rung. If it declares repeat_every_minutes, keep asking on that
    # cadence for as long as the asset is still stopped.
    #
    # Without this the ladder simply runs out: the last person is told once, and if they
    # miss it — phones are not always looked at over machine noise — the fault is never
    # raised again. A loom that has been down nine hours would be as quiet as one that
    # has been down forty minutes, which is precisely the failure this system exists to
    # remove. The rung is re-scheduled from NOW rather than from the incident's start, so
    # the interval means what it says however long the fault has already run.
    last = ladder[-1] if ladder else None
    every = last.get("repeat_every_minutes") if isinstance(last, dict) else None
    if not every:
        return
    try:
        every = float(every)
    except (TypeError, ValueError):
        return
    if every <= 0:
        return
    due = clock.plus_minutes(every)
    _insert_rung(
        c, ticket_id=esc_row["ticket_id"], incident_id=esc_row["incident_id"],
        rung=esc_row["rung"], notify_role=last["notify"], action=last.get("action"),
        due_at=due, trigger="repeat",
    )


def set_eta(cfg: "config.Config", ticket_id: int, hours: int,
            actor: str = "unknown", at: str | None = None) -> dict:
    """The fixer names their deadline; all chasing stops for exactly that long.

    That is the whole bargain of the loop: answer, and the system leaves you alone until
    your own estimate runs out. Every pending rung for the ticket is cancelled and one
    eta_check row is scheduled at +hours. If the machine is still stopped when it fires,
    fire() counts the miss and asks again — and the misses are what the accountability
    report is built from.
    """
    at = at or clock.now_iso()
    due = clock.plus_minutes(hours * 60, base=clock.parse(at))
    with db.transaction() as c:
        ticket = c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if ticket is None or ticket["status"] == "closed":
            return {"ok": False, "why": "ticket is closed"}
        # A live estimate being replaced is a REVISION, not a fresh promise. A fitter
        # who opens a machine, finds it worse than he thought and says so is doing
        # exactly what the loop wants; silently discarding that and marking him a
        # defaulter hours later punishes the honest one and rewards whoever says 24.
        revised = bool(ticket["eta_due_at"]) and ticket["eta_due_at"] > (at or clock.now_iso())
        c.execute("UPDATE tickets SET eta_hours=?, eta_due_at=?, eta_by=? WHERE id=?",
                  (hours, due, actor, ticket_id))
        c.execute("UPDATE escalations SET status='cancelled'"
                  " WHERE ticket_id=? AND status='pending'", (ticket_id,))
        _insert_rung(c, ticket_id=ticket_id, incident_id=None, rung=0,
                     notify_role=ticket["owner_role"], action="eta_check",
                     due_at=due, trigger="eta")
        events.log(c, "ticket", ticket_id, models.K_ETA_SET, actor=actor,
                   detail={"hours": hours, "due_at": due, "revised": revised,
                           "replaces_hours": ticket["eta_hours"] if revised else None},
                   department=cfg.department, at=at)
    return {"ok": True, "hours": hours, "due_at": due, "revised": revised}


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

# A question is only a question once it reaches a phone. PickyAssist answers 100 the
# moment a message is QUEUED and Meta's verdict lands seconds later, so "we sent it" and
# "they got it" are different facts — and the delivery-report webhook is what tells them
# apart.
_ASKS = ("reason_prompt", "eta_request")


def _has_reason(c, incident_id: int) -> bool:
    row = c.execute("SELECT COUNT(*) n FROM incident_reasons WHERE incident_id=?",
                    (incident_id,)).fetchone()
    return bool(row and row["n"])


def _ask_reached_anyone(c, esc_row) -> bool:
    """Is there a question about this incident/ticket that reached them, or is on its way?

    False only when every ask we sent is known to have died — which is the case that
    must not produce a reminder, because there is no answer to chase.
    """
    tag = (f"tkt:{esc_row['ticket_id']}:" if esc_row["ticket_id"]
           else f"inc:{esc_row['incident_id']}:")
    rows = c.execute(
        "SELECT status, delivery_status, sent_at, payload FROM outbox"
        " WHERE dedupe_key LIKE ?", (tag + "%",)).fetchall()
    for r in rows:
        try:
            payload = json.loads(r["payload"]) or {}
        except (TypeError, ValueError):
            continue
        if payload.get("type") not in _ASKS:
            continue
        # Only a DEFINITE failure means nobody was asked. Anything else — delivered,
        # read, an interim receipt, or still on its way — is a question that is either
        # with them or about to be, and re-asking on top of it just doubles up.
        #
        # Asking the opposite question ("is it definitely delivered?") is what jammed
        # the loop: a report of 'submitted' is neither a success nor a failure, so a row
        # stuck there satisfied no branch and every reminder became another "how many
        # hours?", hourly, forever.
        if r["status"] != "failed" and r["delivery_status"] != "failed":
            return True
    return False


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

    # Two different clocks, deliberately separated.
    #
    # base_iso schedules the ladder: a ticket's rungs are measured from when the TICKET
    # opened, because that is when the repair became somebody's job.
    #
    # down_since is what the message SAYS, and it must be when the LOOM stopped. A
    # ticket opens the moment the supervisor answers the reason question, which is at
    # least twenty minutes after the stop — so measuring the sentence from the ticket
    # told a fitter "still down, 25 min" about a loom that had been dead for 47, and the
    # reason prompt he had received twenty minutes earlier said 20. Two messages about
    # one loom, and the downtime went UP by five. The number is the one thing on that
    # message anybody acts on.
    down_since = incident["opened_at"] if incident else base_iso

    # If the asset has resumed (or is inside its resolve grace), don't page anyone.
    if incident is None or incident["status"] in ("resolving", "resolved"):
        c.execute("UPDATE escalations SET status='cancelled' WHERE id=?", (esc_row["id"],))
        return

    asset = c.execute("SELECT * FROM assets WHERE id=?", (incident["asset_id"],)).fetchone()
    asset_ref = asset["asset_ref"] if asset else incident["asset_id"]

    recipients = routing.resolve(cfg, esc_row["notify_role"], when_iso=now,
                                 owner_role=owner_role)
    action = esc_row["action"]
    # Captured before the eta_check branch below rewrites both `action` and `esc_row`:
    # the scheduling of the NEXT rung depends on what this rung originally was.
    was_eta_check = action == "eta_check"

    # Never chase somebody about a question they never received.
    #
    # The later rungs of a ladder are reminders: "still down, no reason given", "please
    # step in". Every one of them assumes the first rung's question arrived. When it did
    # not — the send failed, or Meta dropped it — those reminders read as an accusation
    # about a message the person has never seen, and no amount of chasing can produce an
    # answer to a question nobody was asked. Ask again instead, at whatever rung the
    # ladder has reached.
    if not action and not _ask_reached_anyone(c, esc_row):
        if is_ticket:
            action = "ask_eta"
        elif not _has_reason(c, incident["id"]):
            action = "ask_reason"
        if action:
            log.warning(
                "%s %s: no question has reached anyone yet — asking again instead of "
                "sending a reminder about an answer that was never requested",
                "ticket" if is_ticket else "incident",
                esc_row["ticket_id"] or esc_row["incident_id"])

    # An expired estimate. The machine is still stopped (the resumed check above already
    # returned), so the promise was missed: count it — this row is the defaulter metric —
    # and start the cycle again by asking for a fresh estimate. The re-ask says plainly
    # that the previous one lapsed; pretending it is a new question would throw away the
    # one fact that matters.
    eta_generation = 0
    if action == "eta_check" and is_ticket:
        eta_generation = (ticket["eta_misses"] or 0) + 1
        c.execute("UPDATE tickets SET eta_misses=eta_misses+1 WHERE id=?", (ticket["id"],))
        events.log(c, "ticket", ticket["id"], models.K_ETA_MISSED, actor="system",
                   detail={"promised_hours": ticket["eta_hours"],
                           "promised_by": ticket["eta_by"],
                           "promised_at": ticket["eta_due_at"]},
                   department=cfg.department, at=now)
        action = "ask_eta"
        esc_row = dict(esc_row, action="ask_eta")

    # Build the payload once; recipients differ only by address.
    if action == "ask_eta" and is_ticket:
        missed = ticket["eta_hours"] if esc_row["trigger"] == "eta" else None
        text = prompts.render_eta(cfg, asset_ref, cfg.label(code, "en"),
                                  missed_hours=missed)
        base_payload = {
            "type": "eta_request",
            "text": text,
            "asset_ref": asset_ref,
            "asset_label": asset_ref.replace("_", " ").title(),
            "reason_label": cfg.label(code, "en"),
            "opened_at": incident["opened_at"],
            "ticket_id": ticket["id"],
            "incident_id": incident["id"],
            "rung": esc_row["rung"],
        }
        event_kind = models.K_NOTIFIED
    elif action == "ask_reason":
        # What the message PROMISES has to come from the same place as what the timers
        # DO. It used to read reasons.yaml's reprompt_after_minutes while the schedule
        # came from escalation.yaml's unknown ladder, so the two drifted the moment
        # either was tuned — and the supervisor was told "no reply in 15 minutes" while
        # the system actually waited 25. A message that lies about its own deadline
        # teaches people to ignore the deadline.
        gap = _minutes_to_next_step(ladder, esc_row["rung"])
        text = prompts.render(cfg, asset_ref, incident["opened_at"],
                              reprompt_after_minutes=gap)
        base_payload = {
            "type": "reason_prompt",
            "text": text,
            "asset_ref": asset_ref,
            # A WhatsApp template takes its variables SEPARATELY — the rendered `text`
            # above cannot be passed as one parameter, because template parameters may
            # not contain newlines. So the pieces travel too, and the notifier assembles
            # whichever form its provider needs.
            "asset_label": asset_ref.replace("_", " ").title(),
            "opened_at": incident["opened_at"],
            "reprompt_minutes": int(gap),
            "incident_id": incident["id"],
            "rung": esc_row["rung"],
            "options": prompts.options(cfg),
        }
        event_kind = models.K_PROMPTED
    else:
        minutes = max(1, round((clock.now() - clock.parse(down_since)).total_seconds() / 60))
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
            "asset_label": asset_disp,
            "reason_label": (cfg.label(code, "en") if code else "Not yet reported"),
            "minutes_down": minutes,
            "opened_at": down_since,
            "incident_id": incident["id"],
            "ticket_id": esc_row["ticket_id"],
            "rung": esc_row["rung"],
            "reason_code": code,
        }
        event_kind = models.K_NOTIFIED if esc_row["rung"] == 0 else models.K_ESCALATED

    # Queue one message per recipient (or a single 'log' message if nobody resolved).
    unrouted = not recipients
    if unrouted:
        # Keep the synthetic recipient so the message is still written somewhere rather
        # than vanishing — but do not let this be recorded as a page. See event_kind below.
        recipients = [routing.Recipient(person_id="unrouted", name="unrouted",
                                        channel="log", address="unrouted")]
        log.error("escalation %s for role '%s' resolved to NOBODY — no one has been "
                  "called for this fault", esc_row["id"], esc_row["notify_role"])
    entity_tag = f"tkt:{esc_row['ticket_id']}" if is_ticket else f"inc:{esc_row['incident_id']}"
    for rcpt in recipients:
        payload = dict(base_payload, to_name=rcpt.name, to_role=esc_row["notify_role"])
        # The re-ask after a lapsed estimate lands on rung 0 AGAIN, so a key built from
        # the rung alone collides with the original ask and INSERT IGNORE silently drops
        # the message — the enforcement sentence never reaches a phone, and nothing
        # anywhere records that it did not. The miss generation makes each cycle's ask
        # its own message while restarts still dedupe correctly within a cycle.
        gen = f":miss{eta_generation}" if eta_generation else ""
        # The repeat past the last rung has the same trap with a different trigger: it
        # re-fires the SAME rung number every cycle, so from the second cycle on the key
        # collides with the previous reminder and the repeat is swallowed — while the
        # event log still says ESCALATED. The escalation row id is new each cycle but
        # stable across crash-retries of one cycle, which is exactly what a dedupe
        # generation has to be.
        if esc_row.get("trigger") == "repeat":
            gen += f":rep{esc_row['id']}"
        if action and not esc_row["action"]:
            # This rung was a reminder and became a re-ask, so it must carry its own key
            # — the reminder's key may already exist and INSERT IGNORE would drop the
            # question silently, which is the exact failure this branch exists to fix.
            gen += ":reask"
        dedupe = f"{entity_tag}:rung:{esc_row['rung']}{gen}:{rcpt.person_id}"
        outbox.enqueue(c, rcpt.channel, rcpt.address, payload, dedupe, at=now)

    # Mark fired, log, and set first_notified_at on the ticket's first page.
    c.execute("UPDATE escalations SET status='fired', fired_at=? WHERE id=?", (now, esc_row["id"]))
    if is_ticket and ticket["first_notified_at"] is None and action != "ask_reason":
        c.execute("UPDATE tickets SET first_notified_at=? WHERE id=?", (now, ticket["id"]))

    entity = "ticket" if is_ticket else "incident"
    entity_id = esc_row["ticket_id"] if is_ticket else esc_row["incident_id"]
    if unrouted:
        event_kind = models.K_UNROUTED
    events.log(c, entity, entity_id, event_kind, actor="system",
               detail={"rung": esc_row["rung"], "role": esc_row["notify_role"],
                       "recipients": [r.person_id for r in recipients],
                       "trigger": esc_row["trigger"]},
               department=cfg.department, at=now)

    # After a lapsed estimate the ladder restarts from NOW, not from when the ticket
    # opened. Measuring the next rungs from an opening that is hours in the past makes
    # every one of them due immediately: the re-ask went out, and rungs 1 and 2 fired on
    # the next two ticks — three messages inside ninety seconds, two of them reading as
    # a brand-new fault because only the eta_check carries the missed hours.
    next_base = now if was_eta_check else base_iso
    _schedule_next(c, cfg, esc_row, next_base, ladder)
