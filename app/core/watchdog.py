"""Alerts about ops-core itself, over a channel that is not WhatsApp.

Everything else in this system reports on the plant. This module reports on the system,
to a Google Chat space, and it exists because of a specific failure the pilot made
undeniable: for most of one day Meta rejected every message with error 131037, and the
looms, the tickets and the escalation ladders all carried on perfectly — opening
incidents, firing rungs, marking messages sent. A plant where nobody is being called
looks exactly like a plant where nobody needs calling. The only difference is on a
phone that never buzzed.

Two things go out:

  * The send-path watchdog. The FIRST message that fails to reach a phone raises the
    alarm immediately, then repeats on a fixed cadence until sending works again.
    Waiting out an interval before the first warning is time spent believing people are
    being paged when they are not.
  * The daily digest — what is running right now and what the last 24 hours cost —
    so the numbers arrive without anyone remembering to open a dashboard.

Both are queued through the outbox rather than posted directly, which buys three things
for free: the UNIQUE dedupe key makes "one alert per interval" true across restarts and
across two workers, failures retry with backoff, and every alert ever raised is on the
record next to the messages it was raised about.
"""

from __future__ import annotations

import logging
import os

from .. import clock, db
from . import outbox

log = logging.getLogger("ops.watchdog")

# The recipient recorded on an alert row. The Google Chat incoming webhook carries its
# own destination in the URL, so this is a label for humans reading the outbox, not an
# address anything is looked up by.
SPACE = "ops-alerts"

_warned_unconfigured = False


def _destination_configured() -> bool:
    """Is there anywhere for an alert to go?

    Queuing alarms with no destination is worse than not raising them: the rows pile up
    in the outbox looking like messages that were sent, which is the exact confusion
    this module exists to end. When Chat is not set up the watchdog says so once and
    stays quiet.
    """
    global _warned_unconfigured
    if os.environ.get("GCHAT_WEBHOOK_URL") or os.environ.get("GCHAT_WEBHOOK_BASE_URL"):
        return True
    if not _warned_unconfigured:
        _warned_unconfigured = True
        log.warning("alerts are enabled but no Google Chat destination is configured "
                    "(set GCHAT_WEBHOOK_URL) — nothing will be raised when the send "
                    "path breaks")
    return False


# --- small state helpers ---------------------------------------------------------

def _state(name: str) -> str | None:
    r = db.query_one("SELECT value FROM alert_state WHERE name=?", (name,))
    return r["value"] if r else None


def _set_state(c, name: str, value: str | None) -> None:
    if value is None:
        c.execute("DELETE FROM alert_state WHERE name=?", (name,))
        return
    c.execute(
        "INSERT INTO alert_state(name, value, updated_at) VALUES (?,?,?)"
        " ON DUPLICATE KEY UPDATE value=VALUES(value), updated_at=VALUES(updated_at)",
        (name, value, clock.now_iso()),
    )


def _enqueue(text: str, dedupe: str) -> bool:
    with db.transaction() as c:
        return outbox.enqueue(c, "gchat", SPACE, {"type": "alert", "text": text}, dedupe)


# --- send-path watchdog ----------------------------------------------------------

def send_path_state(cfg) -> dict:
    """Is the outbound channel actually reaching phones? Returns
    {down, down_since, last_failure_at, last_success_at, last_error}.

    'Sent' is not the question. The provider accepts a message in milliseconds and Meta
    rejects it seconds later, out of band — so success here means a delivery report came
    back, or enough time passed that one would have.
    """
    conf = cfg.send_watchdog
    channel = conf.get("channel", "whatsapp")
    grace = clock.plus_seconds(-60 * float(conf.get("assume_delivered_after_minutes", 15)))

    # A row that failed on send never reaches sent_at; one killed by a delivery report
    # has next_try_at stamped at the moment the report landed. COALESCE picks whichever
    # exists so both kinds of death are on the same timeline.
    fail = db.query_one(
        "SELECT MAX(COALESCE(next_try_at, sent_at)) t FROM outbox"
        " WHERE channel=? AND status='failed'", (channel,))
    last_failure_at = fail["t"] if fail else None

    ok = db.query_one(
        "SELECT MAX(sent_at) t FROM outbox WHERE channel=? AND status='sent'"
        " AND (delivery_status IN ('delivered','read') OR"
        "      (delivery_status IS NULL AND sent_at<=?))", (channel, grace))
    last_success_at = ok["t"] if ok else None

    down = bool(last_failure_at) and (
        not last_success_at or last_success_at < last_failure_at)

    down_since = last_error = None
    if down:
        # The start of THIS outage, not the newest failure in it: the alert cadence is
        # counted from when sending broke, so the timestamps in the messages read as one
        # continuous incident rather than restarting on every fresh failure.
        row = db.query_one(
            "SELECT MIN(COALESCE(next_try_at, sent_at)) t FROM outbox"
            " WHERE channel=? AND status='failed'"
            " AND COALESCE(next_try_at, sent_at) > COALESCE(?, '')",
            (channel, last_success_at))
        down_since = (row["t"] if row and row["t"] else last_failure_at)
        err = db.query_one(
            "SELECT delivery_error, recipient FROM outbox WHERE channel=?"
            " AND status='failed' ORDER BY id DESC LIMIT 1", (channel,))
        if err:
            last_error = err["delivery_error"]

    return {"down": down, "down_since": down_since, "last_failure_at": last_failure_at,
            "last_success_at": last_success_at, "last_error": last_error}


def _failed_count_since(channel: str, since: str) -> int:
    r = db.query_one(
        "SELECT COUNT(*) n FROM outbox WHERE channel=? AND status='failed'"
        " AND COALESCE(next_try_at, sent_at)>=?", (channel, since))
    return int(r["n"]) if r else 0


def check_send_path(cfg) -> dict:
    conf = cfg.send_watchdog
    if not conf.get("enabled", True):
        return {"skipped": "disabled"}
    if not _destination_configured():
        return {"skipped": "no destination"}

    every = max(1.0, float(conf.get("repeat_minutes", 30)))
    channel = conf.get("channel", "whatsapp")
    st = send_path_state(cfg)
    now = clock.now()

    if not st["down"]:
        was = _state("send_down_since")
        if not was:
            return {"down": False}
        # Recovered. Say so once, then forget — an all-clear nobody asked for beats a
        # silence that could equally mean "still broken, alarm stopped working".
        minutes = round((clock.parse(st["last_success_at"] or clock.now_iso())
                         - clock.parse(was)).total_seconds() / 60)
        text = (f"✅ *WhatsApp sending is working again.*\n"
                f"Down for about {minutes} min (since {clock.format_ist(was)} IST). "
                f"Messages queued during the outage were retried; anything that had "
                f"already given up is on the /report page as unanswered.")
        _enqueue(text, f"alert:send-up:{was}")
        with db.transaction() as c:
            _set_state(c, "send_down_since", None)
        return {"down": False, "recovered": True}

    down_since = st["down_since"]
    with db.transaction() as c:
        _set_state(c, "send_down_since", down_since)

    # Interval 0 fires the instant the first failure is seen; interval N follows every
    # `every` minutes after that. The interval number is part of the dedupe key, so a
    # restart, a second worker, or a tick that runs twice cannot double-post — and a
    # missed tick cannot skip an interval silently either, because the key for it is
    # still unclaimed.
    elapsed = (now - clock.parse(down_since)).total_seconds() / 60
    interval = int(elapsed // every)
    n_failed = _failed_count_since(channel, down_since)
    reason = st["last_error"] or "(no reason reported by the provider)"

    if interval == 0:
        head = "🔴 *WhatsApp sending is DOWN.*"
    else:
        head = (f"🔴 *WhatsApp still down — {round(elapsed)} min* "
                f"(since {clock.format_ist(down_since)} IST).")
    text = (f"{head}\n"
            f"{n_failed} message(s) have not reached anyone.\n"
            f"Provider says: {reason}\n"
            f"Faults are still being detected and recorded — nobody is being told about "
            f"them. Re-checking every {round(every)} min.")
    posted = _enqueue(text, f"alert:send-down:{down_since}:{interval}")
    return {"down": True, "down_since": down_since, "interval": interval,
            "posted": posted, "failed": n_failed}


# --- daily digest ----------------------------------------------------------------

def _digest_text(cfg, window_hours: float) -> str:
    since = clock.plus_seconds(-window_hours * 3600)
    now_ist = clock.format_ist(clock.now())

    # live: what is stopped right now, longest first
    open_rows = db.query(
        "SELECT a.asset_ref, i.opened_at, ir.code FROM incidents i"
        " JOIN assets a ON a.id=i.asset_id"
        " LEFT JOIN incident_reasons ir ON ir.incident_id=i.id"
        " WHERE i.status IN ('open','resolving') ORDER BY i.opened_at")
    total_assets = db.query_one(
        "SELECT COUNT(*) n FROM assets WHERE active=1")["n"]

    lines = [f"📋 *Daily status — {now_ist} IST*", ""]
    if open_rows:
        lines.append(f"*Stopped now: {len(open_rows)} of {total_assets} machines*")
        for r in open_rows[:10]:
            mins = round((clock.now() - clock.parse(r["opened_at"])).total_seconds() / 60)
            label = cfg.label(r["code"]) if r["code"] else "no reason given yet"
            lines.append(f"• {r['asset_ref'].replace('_', ' ').title()} — "
                         f"{mins} min, {label}")
        if len(open_rows) > 10:
            lines.append(f"• …and {len(open_rows) - 10} more")
    else:
        lines.append(f"*All {total_assets} machines running.*")

    # downtime over the window
    tot = db.query_one(
        "SELECT COUNT(*) n, COALESCE(SUM(duration_s),0) s FROM incidents"
        " WHERE opened_at>=? AND status='resolved'", (since,))
    lines += ["", f"*Last {round(window_hours)}h:* {tot['n']} stops, "
                  f"{round((tot['s'] or 0) / 60)} min of downtime"]

    worst = db.query(
        "SELECT a.asset_ref k, COUNT(*) n, COALESCE(SUM(i.duration_s),0) s"
        " FROM incidents i JOIN assets a ON a.id=i.asset_id"
        " WHERE i.opened_at>=? AND i.status='resolved'"
        " GROUP BY a.asset_ref ORDER BY s DESC LIMIT 3", (since,))
    for r in worst:
        lines.append(f"• {r['k'].replace('_', ' ').title()} — "
                     f"{round(r['s'] / 60)} min over {r['n']} stops")

    by_reason = db.query(
        "SELECT ir.code k, COUNT(*) n, COALESCE(SUM(i.duration_s),0) s"
        " FROM incidents i LEFT JOIN incident_reasons ir ON ir.incident_id=i.id"
        " WHERE i.opened_at>=? AND i.status='resolved'"
        " GROUP BY ir.code ORDER BY s DESC LIMIT 3", (since,))
    if by_reason:
        lines += ["", "*Biggest causes:*"]
        for r in by_reason:
            label = cfg.label(r["k"]) if r["k"] else "no reason given"
            lines.append(f"• {label} — {round(r['s'] / 60)} min over {r['n']} stops")

    # promises — the accountability half, kept short
    open_t = db.query_one(
        "SELECT COUNT(*) n FROM tickets WHERE status<>'closed'")["n"]
    missed = db.query_one(
        "SELECT COALESCE(SUM(eta_misses),0) n FROM tickets WHERE opened_at>=?",
        (since,))["n"]
    unanswered = db.query_one(
        "SELECT COUNT(*) n FROM tickets WHERE opened_at>=? AND eta_by IS NULL",
        (since,))["n"]
    lines += ["", f"*Repairs:* {open_t} open ticket(s), {missed} missed estimate(s), "
                  f"{unanswered} never estimated"]

    # and whether any of the above actually reached a human
    st = send_path_state(cfg)
    if st["down"]:
        lines += ["", f"⚠️ *WhatsApp sending is down since "
                      f"{clock.format_ist(st['down_since'])} IST* — the questions above "
                      f"are not reaching anyone."]
    return "\n".join(lines)


def check_daily_digest(cfg) -> dict:
    conf = cfg.daily_digest
    if not conf.get("enabled", True):
        return {"skipped": "disabled"}
    if not _destination_configured():
        return {"skipped": "no destination"}

    at = str(conf.get("at", "06:15"))
    try:
        hh, mm = clock._hhmm(at)
    except Exception:
        log.error("alerts.daily_digest.at is not HH:MM (%r) — no digest sent", at)
        return {"skipped": "bad time"}

    local = clock.to_ist(clock.now())
    scheduled = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if local < scheduled:
        return {"sent": False}
    # A digest is a morning briefing, not a backlog item. If the box was down at 06:15
    # and comes back at 07:00 the numbers are still worth reading; if it comes back at
    # 23:00 they are not, and posting them then would arrive as a second, confusing
    # "today" hours after the day it describes.
    late = (local - scheduled).total_seconds() / 60
    catch_up = float(conf.get("catch_up_minutes", 120))
    if late > catch_up:
        return {"sent": False, "skipped": f"{round(late)} min late"}

    # The date is the whole schedule: one row per local day, and the UNIQUE dedupe key
    # is what makes "once every day" true rather than "once per tick after 06:15".
    day = local.strftime("%Y-%m-%d")
    text = _digest_text(cfg, float(conf.get("window_hours", 24)))
    posted = _enqueue(text, f"alert:digest:{day}")
    return {"sent": posted, "day": day}


def check(cfg) -> dict:
    """Called by the ticker. Never raises: an alerting bug must not stop the plant's own
    escalations from firing."""
    out = {}
    for name, fn in (("send_path", check_send_path), ("digest", check_daily_digest)):
        try:
            out[name] = fn(cfg)
        except Exception:
            log.exception("watchdog %s check failed", name)
            out[name] = {"error": True}
    return out
