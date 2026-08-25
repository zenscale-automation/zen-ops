"""WhatsApp notifier — Meta WhatsApp Cloud API, direct (no BSP).

Outbound only, to individuals, never a group — a group is the disease the system
replaces. Two send modes, and picking between them correctly is the whole job:

  * Outside a service window, Meta accepts ONLY a pre-approved template. ops-core uses
    a single generic UTILITY template whose one variable carries the fully-rendered
    message body. That is deliberate: bake the numbered reason list into template text
    and every edit to reasons.yaml needs a fresh Meta approval, which would quietly
    undo the promise that a Shingora engineer can change a reason code and restart.
    One template, approved once, and the list stays in YAML where it belongs.

  * Inside a service window — the 24 hours after the person last messaged us — free
    text is allowed and is not billed. Supervisors reply to the first prompt of a
    shift, which opens the window for everything else that shift.

If nothing is configured this falls back to the log notifier, so Phase 1 runs with zero
credentials. In shadow mode the registry never constructs this class at all.
"""

from __future__ import annotations

import os
import re

import requests

from .. import clock, db
from .log import LogNotifier

SERVICE_WINDOW_HOURS = 24


def normalise_msisdn(value: str) -> str:
    """Digits only. Meta reports `from` as "919000000001"; routing.yaml carries
    "+919000000001". Comparing raw strings silently fails to match every time."""
    return re.sub(r"\D", "", value or "")


class WhatsAppNotifier:
    def __init__(self, cfg=None):
        self.cfg = cfg
        self.base = os.environ.get("WHATSAPP_GRAPH_BASE", "https://graph.facebook.com")
        self.api_version = os.environ.get("WHATSAPP_API_VERSION", "v21.0")
        self.token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
        self.phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self.template_name = os.environ.get("WHATSAPP_TEMPLATE_NAME", "ops_core_alert")
        self.template_lang = os.environ.get("WHATSAPP_TEMPLATE_LANG", "en")
        self._fallback = LogNotifier(cfg, via="whatsapp")

    @property
    def configured(self) -> bool:
        return bool(self.token and self.phone_number_id)

    # --- service window ------------------------------------------------------
    def window_open(self, recipient: str) -> bool:
        """True if this person messaged us within the last 24h, so free text is allowed.

        Errs closed: any doubt and we send a template, which always works. Guessing the
        other way gets the message rejected by Meta and the fault goes unreported.
        """
        digits = normalise_msisdn(recipient)
        if not digits:
            return False
        try:
            cutoff = clock.plus_seconds(-SERVICE_WINDOW_HOURS * 3600)
            row = db.query_one(
                "SELECT MAX(received_at) last_at FROM inbound_raw"
                " WHERE channel='whatsapp' AND sender=? AND received_at>=?",
                (digits, cutoff),
            )
            return bool(row and row["last_at"])
        except Exception:
            return False

    # --- payload shaping -----------------------------------------------------
    def _free_text(self, to: str, text: str) -> dict:
        return {"messaging_product": "whatsapp", "recipient_type": "individual",
                "to": to, "type": "text",
                "text": {"preview_url": False, "body": text}}

    def _template(self, to: str, text: str) -> dict:
        """One variable, carrying the whole rendered body. See the module docstring for
        why the reason list is not baked into the template."""
        return {
            "messaging_product": "whatsapp", "recipient_type": "individual",
            "to": to, "type": "template",
            "template": {
                "name": self.template_name,
                "language": {"code": self.template_lang},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": text}],
                }],
            },
        }

    def build(self, recipient: str, payload: dict) -> dict:
        to = normalise_msisdn(recipient)
        text = payload.get("text") or ""
        if self.window_open(recipient):
            return self._free_text(to, text)
        return self._template(to, text)

    # --- send ----------------------------------------------------------------
    def send(self, recipient: str, payload: dict) -> str:
        if not self.configured:
            return self._fallback.send(recipient, payload)
        url = f"{self.base}/{self.api_version}/{self.phone_number_id}/messages"
        resp = requests.post(
            url, json=self.build(recipient, payload),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code >= 400:
            # Surface Meta's own error text — "template does not exist" and "re-engagement
            # message" (window closed) are the two you will actually hit, and the generic
            # HTTPError hides both. Raising here lets the outbox retry with backoff.
            raise RuntimeError(
                f"WhatsApp send failed {resp.status_code}: {resp.text[:400]}")
        data = resp.json() if resp.content else {}
        try:
            return data["messages"][0]["id"]          # wamid.XXXX
        except (KeyError, IndexError, TypeError):
            return "wa-sent"
