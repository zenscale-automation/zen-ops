"""Notifier registry. get_notifier(cfg, channel) returns a cached notifier for the
channel named on an outbox row. Unknown/panel channels resolve to the log notifier.

There is no global "send / do not send" switch. Who is reached is decided in one place —
the roster, edited through the dashboard — and a message queued for a person is a
message sent to that person. A switch that silently turned every send into a log line
made "is anybody actually being called?" a question you had to answer by reading
configuration instead of by reading the roster.
"""

from __future__ import annotations

from .gchat import GChatNotifier
from .log import LogNotifier
from .whatsapp import WhatsAppNotifier

_cache: dict = {}


def get_notifier(cfg, channel: str):
    if channel in _cache:
        return _cache[channel]
    if channel == "whatsapp":
        n = WhatsAppNotifier(cfg)
    elif channel == "gchat":
        n = GChatNotifier(cfg)
    else:  # log | panel | anything else
        n = LogNotifier(cfg, via=channel)
    _cache[channel] = n
    return n


def reset_cache() -> None:
    _cache.clear()
