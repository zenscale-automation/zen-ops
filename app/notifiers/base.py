"""Notifier contract — department-blind. A notifier turns an outbox row into a
delivered message and returns a provider message id, or raises on failure so the
outbox retries. Notifiers know nothing about incidents, reasons, or escalation; they
only render `payload['text']` (plus optional structured extras) to a recipient.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    def send(self, recipient: str, payload: dict) -> str:
        """Deliver payload to recipient. Return a provider message id. Raise on failure."""
        ...
