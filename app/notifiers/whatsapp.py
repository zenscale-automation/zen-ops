"""WhatsApp notifier via a Business Solution Provider (BSP).

Outbound only, to individuals or a bot thread — never a group (a group is the disease
the system replaces). Messages go out with numbered reply options; the reply comes back
through /webhook/whatsapp. If no BSP is configured (WHATSAPP_BSP_BASE_URL /
WHATSAPP_BSP_TOKEN unset), this transparently falls back to the log notifier so Phase 1
runs with zero credentials — the message is written to logs/notifications.log exactly as
it would be sent.
"""

from __future__ import annotations

import os

import requests

from .log import LogNotifier


class WhatsAppNotifier:
    def __init__(self, cfg=None):
        self.base_url = os.environ.get("WHATSAPP_BSP_BASE_URL", "").rstrip("/")
        self.token = os.environ.get("WHATSAPP_BSP_TOKEN", "")
        self._fallback = LogNotifier(cfg, via="whatsapp")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _build_message(self, recipient: str, payload: dict) -> dict:
        """Shape a BSP-style payload. Reason prompts carry numbered quick-reply buttons;
        other messages are plain text. Adjust field names to your specific BSP."""
        if payload.get("type") == "reason_prompt" and payload.get("options"):
            buttons = [{"id": str(o["n"]), "title": o["label"][:20]}
                       for o in payload["options"][:10]]
            return {
                "to": recipient,
                "type": "interactive",
                "text": payload.get("text", ""),
                "quick_replies": buttons,
            }
        return {"to": recipient, "type": "text", "text": payload.get("text", "")}

    def send(self, recipient: str, payload: dict) -> str:
        if not self.configured:
            return self._fallback.send(recipient, payload)
        resp = requests.post(
            f"{self.base_url}/messages",
            json=self._build_message(recipient, payload),
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        try:
            return data["messages"][0]["id"]
        except (KeyError, IndexError, TypeError):
            return data.get("id", "wa-sent")
