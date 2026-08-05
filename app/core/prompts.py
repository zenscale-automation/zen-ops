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
            "label_hi": (c.get("label", {}) or {}).get("hi") if isinstance(c.get("label"), dict) else None,
        })
    # 'Other' always last
    n += 1
    opts.append({"n": n, "code": cfg.other_code, "label": "Other", "label_hi": None})
    return opts


def render(cfg: "config.Config", asset_ref: str, opened_at_iso: str,
           reprompt_after_minutes: float | None = None) -> str:
    """The WhatsApp prompt text. Framed as help arriving, not a threat (doc 3.4)."""
    minutes = max(1, round((clock.now() - clock.parse(opened_at_iso)).total_seconds() / 60))
    label = asset_ref.replace("_", " ").title()
    lines = [f"{label} has been stopped for {minutes} minutes.", "", "Reply with the reason:"]
    for o in options(cfg):
        lines.append(f"  {o['n']}  {o['label']}")
    rep = reprompt_after_minutes if reprompt_after_minutes is not None else cfg.reprompt_after_minutes
    lines += [
        "",
        "We will notify the right person straight away. If there is no reply "
        f"in {int(rep)} minutes this goes to the shift in-charge.",
    ]
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
    # exact label match (en)
    low = t.lower()
    for o in opts:
        if o["label"].lower() == low:
            return o["code"], None
    # code match
    for o in opts:
        if o["code"].lower() == low:
            return o["code"], None
    return None
