"""The numbered reason menu.

One source of truth for two consumers: the escalation engine that *sends* the prompt
and the webhook that *parses* the reply. Never free text — free text is unparseable and
is socially an invitation to explain, which produces "machine problem, informed
maintenance" forty times (design doc 3.4).
"""

from __future__ import annotations

from .. import clock, config

# The catch-all comes from config (cfg.other_code). It used to be the constant
# "weaving.other" — a domain code sitting in the department-blind core, which a second
# department would have inherited silently.


def _norm(value: str) -> str:
    """Lowercase, letters and digits only. Button text arrives with whatever spacing and
    punctuation the approved template happens to use."""
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def options(cfg: "config.Config") -> list[dict]:
    """Numbered options 1..N for the prompt. Order is file order in reasons.yaml, so
    the digit->code mapping is stable. 'Other' is always the final option."""
    opts: list[dict] = []
    n = 0
    for c in cfg.prompt_codes():
        if c.get("code") == cfg.other_code:
            continue
        n += 1
        opts.append({
            "n": n,
            "code": c["code"],
            "label": cfg.label(c["code"], "en"),
        })
    # 'Other' always last
    n += 1
    opts.append({"n": n, "code": cfg.other_code, "label": "Other"})
    return opts


def render(cfg: "config.Config", asset_ref: str, opened_at_iso: str,
           reprompt_after_minutes: float | None = None) -> str:
    """The WhatsApp prompt text. Framed as help arriving, not a threat (doc 3.4)."""
    minutes = max(1, round((clock.now() - clock.parse(opened_at_iso)).total_seconds() / 60))
    label = asset_ref.replace("_", " ").title()
    number = asset_ref.split("_")[-1]
    # The loom number goes in the reply instruction, not just the greeting: with two
    # looms stopped, two of these sit on one screen and a bare "1" is a coin toss.
    lines = [f"{label} has been stopped for {minutes} minutes.", "",
             f"Reply with the loom number and the reason, e.g. {number} 1:"]
    for o in options(cfg):
        lines.append(f"  {number} {o['n']}  {o['label']}")
    # Computed by the caller from the ladder that will actually run — the single source
    # for every timing. None means no follow-up is scheduled, and the message says so by
    # promising nothing rather than inventing a number.
    rep = int(reprompt_after_minutes or 0)
    if rep > 0:
        lines += ["", "We will notify the right person straight away. If there is no "
                      f"reply in {rep} minutes we will ask again."]
    else:
        # A finite ladder that has run out. Promising a follow-up that will not happen
        # is worse than saying nothing.
        lines += ["", "We will notify the right person straight away."]
    return "\n".join(lines)


def parse(cfg: "config.Config", text: str) -> tuple[str, str | None] | None:
    """Parse a reply into (code, subcode). Accepts a leading digit ('2', '2 done'),
    or a case-insensitive exact label match. Returns None if nothing matches."""
    if not text:
        return None
    t = text.strip()
    opts = options(cfg)
    # leading digit
    token = t.split()[0]
    if token.isdigit():
        n = int(token)
        for o in opts:
            if o["n"] == n:
                return o["code"], None
        return None
    # Label match, normalised. The text that comes back from a tapped button is whatever
    # the APPROVED TEMPLATE says, which is not necessarily what reasons.yaml says — the
    # template is frozen at Meta and the YAML is not, so they drift. Normalising removes
    # spacing and punctuation differences ("Beam Change/Gating" vs "Beam change /
    # gaiting"); genuine wording differences need an explicit alias, below.
    low = _norm(t)
    for o in opts:
        if _norm(o["label"]) == low:
            return o["code"], None
    # Explicit aliases, for when the template wording and the config wording genuinely
    # differ. Without these a supervisor taps a button and the system silently records
    # nothing, then asks them again.
    for c in cfg.codes:
        for alias in (c.get("prompt_aliases") or []):
            if _norm(str(alias)) == low:
                return c["code"], None
    # code match
    for o in opts:
        if o["code"].lower() == t.strip().lower():
            return o["code"], None
    return None


# --- saying WHICH loom you mean ---------------------------------------------------

PASS_WORDS = ("pass", "not me", "notme", "someone else", "handover", "hand over")


def split_asset(cfg: "config.Config", text: str) -> tuple[str | None, str]:
    """Pull a leading loom number off a reply: "91 1" -> ("loom_91", "1").

    Two looms stopping within ten minutes is routine in a weaving shed, and both
    questions then sit on one screen looking identical. Naming the loom is the only
    thing a person can type that removes the ambiguity, and everything they would
    naturally reach for — "91 1", "loom 91 electrical" — used to parse as option 91 and
    be discarded as unreadable.
    """
    t = (text or "").strip()
    if not t:
        return None, t
    # The word people say for a machine is the configured ref prefix without its
    # separator, so this layer stays department-blind: a spinning hall calling them
    # "frame_" accepts "frame 12 1" with no code change here.
    prefix = (cfg.source.get("settings", {}) or {}).get("asset_ref_prefix", "")
    if not prefix:
        return None, t
    spoken = prefix.rstrip("_-").lower()
    parts = t.split()
    first = parts[0].lower().strip(":.-")
    if spoken and first == spoken and len(parts) > 1:
        parts = parts[1:]
        first = parts[0].lower().strip(":.-")
    if not first.isdigit() or len(parts) < 2:
        return None, t
    return f"{prefix}{int(first)}", " ".join(parts[1:]).strip()


def is_pass(text: str) -> bool:
    """"Not my job." The one sentence a person most needs and could not say."""
    return _norm(text) in tuple(_norm(w) for w in PASS_WORDS)


# --- the fixer's time estimate ---------------------------------------------------

ETA_MAX_HOURS = 24
# Capped at a day, for two reasons. A longer number is not an estimate, it is parking —
# and the WhatsApp free-text window is 24 hours from the person's last message, so any
# re-ask inside the cap can still be sent as plain text rather than needing a template.


def render_eta(cfg: "config.Config", asset_ref: str, reason_label: str,
               missed_hours: int | None = None) -> str:
    """The question that closes the loop: the fixer names their own deadline.

    The snooze that follows is the bargain — answer, and the system stops nagging you
    for exactly that long. `missed_hours` set means a previous estimate expired with the
    machine still stopped; saying so plainly is the enforcement, and pretending it is a
    fresh question would waste the one fact that matters.
    """
    label = asset_ref.replace("_", " ").title()
    if missed_hours is not None:
        head = (f"{label} is STILL stopped — the {missed_hours} hour"
                f"{'s' if missed_hours != 1 else ''} estimated for "
                f"{reason_label} have passed.")
    else:
        head = f"{label} has stopped: {reason_label}."
    return "\n".join([
        head,
        "",
        "How many hours will the fix take?",
        f"Reply with a number (1 = under an hour, up to {ETA_MAX_HOURS}).",
        "We will not chase you again until that time is up.",
        "",
        "Not your job? Reply PASS.",
        "Need longer later? Send a new number any time.",
    ])


def parse_eta(text: str) -> int | None:
    """An hours estimate: a bare number, 1..ETA_MAX_HOURS. None if it is not one.

    Deliberately strict — this shares an inbox with the numbered reason menu, so an
    ambiguous parse here would eat replies meant for something else. Anything that is
    not just a number is not an estimate.
    """
    t = (text or "").strip()
    if not t or not t.replace(".", "", 1).isdigit():
        return None
    try:
        hours = int(float(t))
    except ValueError:
        return None
    if hours < 1:
        hours = 1              # "0.5" etc: under an hour is the floor, per the prompt
    return hours if hours <= ETA_MAX_HOURS else None


# --- what the system says back ---------------------------------------------------
#
# Until now it said nothing. A person tapped a button, or typed a number, and got
# silence — which is indistinguishable from the message never arriving, from the reply
# being unparseable, and from the system being down. Someone who answers correctly and
# is then chased anyway concludes it is not listening and stops answering, and every
# number downstream is then measuring noise.
#
# These are free text, never templates: the person has just messaged us, so their
# 24-hour service window is open by definition and no approval is needed.

def ack_reason(asset_ref: str, reason_label: str, ticketed: bool) -> str:
    label = asset_ref.replace("_", " ").title()
    if ticketed:
        return f"Got it. {label} — {reason_label}. Sent to the engineer."
    return f"Got it. {label} — {reason_label}. Nothing more needed from you."


def ack_eta(asset_ref: str, hours: int, due_at_iso: str, revised: bool = False) -> str:
    label = asset_ref.replace("_", " ").title()
    when = clock.format_ist(due_at_iso)
    head = "Updated." if revised else "OK."
    return (f"{head} {label} — {hours} hour{'s' if hours != 1 else ''}, until {when}. "
            f"We will not chase you before then.")


def ack_unparsed(cfg: "config.Config", text: str, asset_ref: str | None) -> str:
    """The reply nobody could read. Repeating the menu costs one message and saves the
    re-ask, the escalation, and the person's belief that answering does anything."""
    said = (text or "").strip()
    if len(said) > 30:
        said = said[:30] + "…"
    label = (asset_ref or "").replace("_", " ").title()
    where = f"{label} — reply" if label else "Reply"
    menu = "  ".join(f"{o['n']} {o['label']}" for o in options(cfg))
    return "\n".join([f'Sorry, did not understand "{said}".', f"{where} with a number only:",
                       menu])


def ack_already_running(asset_ref: str) -> str:
    label = asset_ref.replace("_", " ").title()
    return f"{label} is running again — no answer needed. Thanks."


def ack_already_answered(asset_ref: str, reason_label: str | None) -> str:
    label = asset_ref.replace("_", " ").title()
    if reason_label:
        return f"Already recorded: {label} — {reason_label}."
    return f"Already recorded for {label}."
