"""Routing: reason code -> role -> person, shift-aware.

Assignment happens without anyone choosing (design defect #1: "no owner"). The role
on duty *right now* is looked up from the shift calendar, so a 3am fault reaches
whoever is actually on nights, not a name hardcoded months ago.

"owner" is a reserved role that resolves to the ticket's own owner_role.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import clock, config


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
            owner_role: str | None = None) -> list[Recipient]:
    """People to notify for a role at the shift in effect at `when_iso` (default now)."""
    if role == "owner":
        role = owner_role or role
    when = clock.parse(when_iso) if when_iso else clock.now()
    shift = clock.resolve_shift(when, cfg.shifts)

    recipients: list[Recipient] = []
    for pid in cfg.role_person_ids(role, shift):
        person = cfg.person(pid)
        if not person:
            continue
        channel, address = _channel_for(person, pid)
        recipients.append(
            Recipient(person_id=pid, name=person.get("name", pid),
                      channel=channel, address=address)
        )
    return recipients


def current_shift(cfg: "config.Config", when_iso: str | None = None) -> str | None:
    when = clock.parse(when_iso) if when_iso else clock.now()
    return clock.resolve_shift(when, cfg.shifts)
