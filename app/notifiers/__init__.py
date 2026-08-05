"""Notifier registry. get_notifier(cfg, channel) returns a cached notifier for the
channel named on an outbox row. Unknown/panel channels resolve to the log notifier.
"""

from __future__ import annotations

from .gchat import GChatNotifier
from .log import LogNotifier
from .whatsapp import WhatsAppNotifier

_cache: dict = {}


def get_notifier(cfg, channel: str):
    key = channel
    if key in _cache:
        return _cache[key]
    if channel == "whatsapp":
        n = WhatsAppNotifier(cfg)
    elif channel == "gchat":
        n = GChatNotifier(cfg)
    else:  # log | panel | anything else
        n = LogNotifier(cfg, via=channel)
    _cache[key] = n
    return n


def reset_cache() -> None:
    _cache.clear()
