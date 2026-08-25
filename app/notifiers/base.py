"""Notifier contract — department-blind. A notifier turns an outbox row into a
delivered message and returns a provider message id, or raises on failure so the
outbox retries. Notifiers know nothing about incidents, reasons, or escalation; they
only render `payload['text']` (plus optional structured extras) to a recipient.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class PermanentSendError(Exception):
    """A delivery failure that will fail identically on every retry.

    A bad API key, a deactivated account, or a free-text message sent outside the
    24-hour window are not transient. The outbox drains SERIALLY, so retrying one of
    these eight times with backoff occupies the queue for roughly ten minutes behind a
    message that cannot succeed — while a real page for a different loom waits behind it.

    Lives here rather than in the provider module so app/core stays provider-blind: the
    outbox needs to recognise the category without importing anyone's SDK.
    """


@runtime_checkable
class Notifier(Protocol):
    def send(self, recipient: str, payload: dict) -> str:
        """Deliver payload to recipient. Return a provider message id. Raise on failure."""
        ...
