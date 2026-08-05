"""Routing: reason code -> role -> person, shift-aware.

Assignment happens without anyone choosing (design defect #1: "no owner"). The role
on duty *right now* is looked up from the shift calendar, so a 3am fault reaches
whoever is actually on nights, not a name hardcoded months ago.

"owner" is a reserved role that resolves to the ticket's own owner_role.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import clock, config

log = logging.getLogger("ops.routing")


@dataclass
class Recipient:
    person_id: str
    name: str
    channel: str      # whatsapp | gchat | log
    address: str      # phone number, space id, or the person id (log)


def _channel_for(person: dict, person_id: str) -> tuple[str, str]:
    if person.get("whatsapp"):
        return "whatsapp", person["whatsapp"]
    if person.get("gchat_space"):
        return "gchat", person["gchat_space"]
    return "log", person_id


def resolve(cfg: "config.Config", role: str, when_iso: str | None = None,
            owner_role: str | None = None, for_prompt: bool = False) -> list[Recipient]:
    """People to notify for a role at the shift in effect at `when_iso` (default now).

    `for_prompt` marks the "why is this asset stopped?" question, which must always go
    to whoever can actually see it. route_all_to_default deliberately does NOT apply to
    prompts: sending the question to a manager who cannot see the shed floor would get
    no useful answer, and the whole flow stalls waiting for one.
    """
    if role == "owner":
        role = owner_role or role
    when = clock.parse(when_iso) if when_iso else clock.now()
    shift = clock.resolve_shift(when, cfg.shifts)

    if cfg.route_all_to_default and cfg.default_owner and not for_prompt:
        # Pilot mode: one named person catches everything, whatever the reason says.
        return _as_recipients(cfg, [cfg.default_owner])

    recipients = _as_recipients(cfg, cfg.role_person_ids(role, shift))
    if recipients:
        return recipients

    # Nobody on this role for this shift. Falling through to an empty list means the
    # fault is assigned to no one and nobody is told — which is defect #1 recreated
    # inside the fix for it. The default owner is the backstop.
    if cfg.default_owner:
        log.warning("role '%s' resolved to nobody on shift %s — falling back to "
                    "default_owner '%s'", role, shift, cfg.default_owner)
        return _as_recipients(cfg, [cfg.default_owner])
    log.error("role '%s' resolved to nobody on shift %s and no default_owner is set — "
              "this notification has no recipient", role, shift)
    return []


def _as_recipients(cfg: "config.Config", person_ids) -> list[Recipient]:
    out: list[Recipient] = []
    for pid in person_ids:
        person = cfg.person(pid)
        if not person:
            continue
        channel, address = _channel_for(person, pid)
        out.append(Recipient(person_id=pid, name=person.get("name", pid),
                             channel=channel, address=address))
    return out


def current_shift(cfg: "config.Config", when_iso: str | None = None) -> str | None:
    when = clock.parse(when_iso) if when_iso else clock.now()
    return clock.resolve_shift(when, cfg.shifts)
