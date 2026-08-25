"""Typed views over the core tables. The DB layer returns dict rows (PyMySQL
DictCursor); these dataclasses give the domain code readable field access. They are
plain data — no behaviour, no persistence — matching the doc's `core/models.py`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Asset:
    id: str
    department: str
    asset_ref: str
    label: str | None
    active: int

    @classmethod
    def from_row(cls, r) -> "Asset":
        return cls(r["id"], r["department"], r["asset_ref"], r["label"], r["active"])


@dataclass
class Incident:
    id: int
    asset_id: str
    department: str
    opened_at: str
    resolved_at: str | None
    duration_s: int | None
    shift: str | None
    condition: str | None
    status: str
    resolve_due_at: str | None

    @classmethod
    def from_row(cls, r) -> "Incident":
        return cls(
            r["id"], r["asset_id"], r["department"], r["opened_at"], r["resolved_at"],
            r["duration_s"], r["shift"], r["condition"], r["status"], r["resolve_due_at"],
        )


@dataclass
class Ticket:
    id: int
    incident_id: int
    department: str
    code: str
    owner_role: str
    opened_at: str
    first_notified_at: str | None
    attended_at: str | None
    attended_by: str | None
    diagnosis: str | None
    closed_at: str | None
    close_reason: str | None
    reopen_count: int
    status: str

    @classmethod
    def from_row(cls, r) -> "Ticket":
        return cls(
            r["id"], r["incident_id"], r["department"], r["code"], r["owner_role"],
            r["opened_at"], r["first_notified_at"], r["attended_at"], r["attended_by"],
            r["diagnosis"], r["closed_at"], r["close_reason"], r["reopen_count"], r["status"],
        )


@dataclass
class Escalation:
    id: int
    ticket_id: int | None
    incident_id: int | None
    rung: int
    notify_role: str
    action: str | None
    due_at: str
    fired_at: str | None
    status: str
    trigger: str | None

    @classmethod
    def from_row(cls, r) -> "Escalation":
        return cls(
            r["id"], r["ticket_id"], r["incident_id"], r["rung"], r["notify_role"],
            r["action"], r["due_at"], r["fired_at"], r["status"], r["trigger"],
        )


# Event kinds (append-only log)
K_OPENED = "opened"
K_REASON_SET = "reason_set"
K_NOTIFIED = "notified"
K_ESCALATED = "escalated"
K_ATTENDED = "attended"
K_CLOSED = "closed"
K_REOPENED = "reopened"
K_RESOLVED = "resolved"
K_PROMPTED = "prompted"
# A rung fired but resolved to nobody. Deliberately NOT K_NOTIFIED: the event log's whole
# premise is that it is the record of what happened, and writing "notified" when nobody
# was notified corrupts the one table you would reach for afterwards to find out why
# nobody came.
K_UNROUTED = "unrouted"
