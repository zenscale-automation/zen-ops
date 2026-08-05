"""Log notifier — the zero-credential default.

Writes every outbound message to logs/notifications.log (and the ops.notify logger)
instead of sending it anywhere. This is what runs until a real WhatsApp BSP / Google
Chat webhook is configured, so the whole system is exercisable end-to-end with nothing
external. It never fails, so the outbox always drains.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

from .. import clock

_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "notifications.log"
_lock = threading.Lock()
_logger = logging.getLogger("ops.notify")


class LogNotifier:
    def __init__(self, cfg=None, via: str = "log"):
        self.via = via  # label showing which channel this stood in for
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    def send(self, recipient: str, payload: dict) -> str:
        kind = payload.get("type", "message")
        name = payload.get("to_name", "")
        role = payload.get("to_role", "")
        text = payload.get("text", "")
        header = f"{clock.now_iso()}  via={self.via:<8} to={recipient} ({name}/{role})  [{kind}]"
        block = header + "\n    " + text.replace("\n", "\n    ") + "\n"
        with _lock:
            with _LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(block + "\n")
        _logger.info("notify via=%s to=%s [%s] %s", self.via, recipient, kind,
                     text.split(chr(10))[0])
        return f"log-{uuid.uuid4().hex[:12]}"
