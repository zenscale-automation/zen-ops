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
    lines = [f"{label} has been stopped for {minutes} minutes.", "", "Reply with the reason:"]
    for o in options(cfg):
        lines.append(f"  {o['n']}  {o['label']}")
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
