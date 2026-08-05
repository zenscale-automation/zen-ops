"""Source contract. Everything department-specific collapses into an adapter that emits
two events into the core; the core cannot tell a loom poll from an ERP query from a
human report apart, which is the entire point (design doc 4.4).

The weaving source polls the loom API and diffs machine state. Its `poll()` returns the
transitions since the last poll; the poller worker applies them to the core. Resolution
is a *source* responsibility — weaving gets it free because the loom restarts; other
departments may need a manual close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class IncidentOpened:
    asset_ref: str            # department-scoped, e.g. "loom_23"
    condition: str            # what state the asset entered, e.g. "STOPPED"
    at: str                   # UTC ISO8601
    context: dict = field(default_factory=dict)


@dataclass
class IncidentResolved:
    asset_ref: str
    at: str


@runtime_checkable
class Source(Protocol):
    department: str

    def seed(self) -> None:
        """Initialise last-known state (e.g. from open incidents) so a restart does not
        re-open or spuriously resolve."""
        ...

    def discover_assets(self) -> list[dict]:
        """Return [{asset_ref, label}] the source knows about, if it can enumerate."""
        ...

    def poll(self, now_iso: str) -> list:
        """Return a list of IncidentOpened / IncidentResolved since the last poll."""
        ...
