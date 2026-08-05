"""Notifier registry. get_notifier(cfg, channel) returns a cached notifier for the
channel named on an outbox row. Unknown/panel channels resolve to the log notifier.

In SHADOW MODE (cfg.shadow_mode, the default) every channel resolves to the log
notifier regardless of credentials: the full pipeline runs, every message that would
have been sent is written to logs/notifications.log, and nothing leaves the box. This
is a single explicit switch rather than a side effect of blank credential fields, so
"is anything actually being sent?" has one answer you can read off /health.
"""

from __future__ import annotations

from .gchat import GChatNotifier
from .log import LogNotifier
from .whatsapp import WhatsAppNotifier

_cache: dict = {}


def get_notifier(cfg, channel: str):
    shadow = bool(getattr(cfg, "shadow_mode", True))
    key = f"{channel}:{'shadow' if shadow else 'live'}"
    if key in _cache:
        return _cache[key]
    if shadow:
        n = LogNotifier(cfg, via=channel)
    elif channel == "whatsapp":
        n = WhatsAppNotifier(cfg)
    elif channel == "gchat":
        n = GChatNotifier(cfg)
    else:  # log | panel | anything else
        n = LogNotifier(cfg, via=channel)
    _cache[key] = n
    return n


def reset_cache() -> None:
    _cache.clear()
