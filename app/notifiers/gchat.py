"""Google Chat notifier — space or DM cards.

Used where a role's recipient is a Chat space (e.g. the yarn store desk) rather than a
personal WhatsApp number. Falls back to the log notifier when no webhook base is
configured, so it never blocks Phase 1.
"""

from __future__ import annotations

import os

import requests

from .log import LogNotifier


class GChatNotifier:
    def __init__(self, cfg=None):
        self.base_url = os.environ.get("GCHAT_WEBHOOK_BASE_URL", "").rstrip("/")
        self._fallback = LogNotifier(cfg, via="gchat")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _build_card(self, payload: dict) -> dict:
        text = payload.get("text", "")
        card = {"text": text}
        if payload.get("type") == "reason_prompt" and payload.get("options"):
            opts = "  ".join(f"[{o['n']}] {o['label']}" for o in payload["options"])
            card = {"text": text + "\n" + opts}
        return card

    def send(self, recipient: str, payload: dict) -> str:
        if not self.configured:
            return self._fallback.send(recipient, payload)
        # recipient is a space id like "spaces/AAAA..."; POST a message into it.
        resp = requests.post(
            f"{self.base_url}/v1/{recipient}/messages",
            json=self._build_card(payload),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return data.get("name", "gchat-sent")
