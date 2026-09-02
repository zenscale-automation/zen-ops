"""WhatsApp notifier — PickyAssist (WhatsApp Official, application 121).

Outbound only, to individuals, never a group — a group is the disease the system
replaces. Two send modes, and choosing between them correctly is the whole job:

  * Outside the service window WhatsApp accepts ONLY a pre-approved template.
    PickyAssist reports this as status 802, "Not Contactable 24 Hours Session Expired".
  * Inside the window — the 24 hours after that person last messaged us — free text is
    allowed. Supervisors reply to the first prompt of a shift, which opens the window for
    everything else that shift.

Two templates are needed, not one, because the two messages have different shapes:
the numbered reason question, and the "loom X is down, please attend" page. Both are
approved with their variables baked in as `{{n}}`; this module supplies the values.

Why the values and not the rendered text: a WhatsApp template parameter may not contain
newline characters, tabs, or more than four consecutive spaces — Meta rejects it with
"Param text cannot have new-line/tab characters". `prompts.render` produces a multi-line
body, so it cannot be passed as one variable, however convenient that would be. The
consequence is that the reason list lives inside the approved template rather than in
reasons.yaml, and changing which reasons appear needs a new template approved. That is
why the admin API refuses to edit the prompt list.

If nothing is configured this falls back to the log notifier, so Phase 1 runs with zero
credentials.

PickyAssist returns HTTP 200 with an error body, so `raise_for_status()` is worse than
useless here: it never fires, and a failed send would be recorded as delivered.
"""

from __future__ import annotations

import os
import re

import requests

from .. import clock, db
from .base import PermanentSendError
from .log import LogNotifier

SERVICE_WINDOW_HOURS = 24

PUSH_PATH = "/push"
STATUS_ACCEPTED = 100
# Failures that will fail identically on every retry. The outbox drains serially, so
# retrying one of these eight times occupies the queue for ~10 minutes behind a message
# that cannot succeed, while real pages wait behind it.
PERMANENT_STATUSES = {
    401: "authentication failed — check PICKYASSIST_TOKEN and the IP allowlist",
    801: "the WhatsApp account is not active on the PickyAssist side",
    802: "no open 24-hour session with this person, and this was sent as free text — "
         "it needed an approved template",
}


def normalise_msisdn(value: str) -> str:
    """Digits only. PickyAssist reports the sender as "919000000001" and wants the same
    on the way out; routing.yaml carries "+91 90000 00001". Comparing or sending raw
    strings silently fails to match every time."""
    return re.sub(r"\D", "", value or "")


class WhatsAppNotifier:
    def __init__(self, cfg=None):
        self.cfg = cfg
        self.base = os.environ.get("PICKYASSIST_BASE_URL",
                                   "https://app.pickyassist.com/api/v2").rstrip("/")
        self.token = os.environ.get("PICKYASSIST_TOKEN", "")
        # 121 = WhatsApp Official Managed, which is what this account is provisioned for.
        # 8 and 101 exist for other PickyAssist plans and return 801 here.
        self.application = os.environ.get("PICKYASSIST_APPLICATION", "121")
        self.prompt_template = os.environ.get("PICKYASSIST_PROMPT_TEMPLATE_ID", "")
        self.escalation_template = os.environ.get("PICKYASSIST_ESCALATION_TEMPLATE_ID", "")
        # The hours-estimate question. Free text covers it ONLY while the person asked is
        # the person who just replied — true with a one-person roster, false the moment a
        # supervisor's answer routes a ticket to a fitter who has never messaged us. A
        # cold first ask with no template is a permanent 802 and the fitter is never
        # asked, so this template is required before the roster grows past one.
        self.eta_template = os.environ.get("PICKYASSIST_ETA_TEMPLATE_ID", "")
        self.language = os.environ.get("PICKYASSIST_TEMPLATE_LANG", "en")
        self._fallback = LogNotifier(cfg, via="whatsapp")

    @property
    def configured(self) -> bool:
        return bool(self.token)

    # --- service window ------------------------------------------------------

    def window_open(self, recipient: str) -> bool:
        """True if this person messaged us within the last 24h, so free text is allowed.

        Errs closed: any doubt and we send a template, which always works. Guessing the
        other way earns an 802 and the fault goes unreported.
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

    # --- template variables --------------------------------------------------

    def _minutes_down(self, payload: dict) -> str:
        """Recomputed at send time, not frozen at enqueue time. The payload is built
        inside the ticker transaction and may then sit in the outbox through a retry
        ladder; a frozen number can be minutes stale by the time it is read on a phone."""
        if payload.get("minutes_down") is not None:
            return str(payload["minutes_down"])
        opened = payload.get("opened_at")
        if not opened:
            return "0"
        try:
            secs = (clock.now() - clock.parse(opened)).total_seconds()
            return str(max(1, round(secs / 60)))
        except Exception:
            return "0"

    def _asset_label(self, payload: dict) -> str:
        # cfg.asset_type is the department's own word — loom, vat, machine. Hardcoding
        # one here would put weaving's vocabulary in the mouth of every other department.
        generic = getattr(self.cfg, "asset_type", "asset") if self.cfg else "asset"
        return (payload.get("asset_label")
                or (payload.get("asset_ref") or "").replace("_", " ").title()
                or f"A {generic}")

    def template_for(self, payload: dict) -> tuple[str, list, list]:
        """(template_id, body variables, header variables) for this payload type.

        Header values travel SEPARATELY as template_header, never prepended to the
        body. Meta counts parameters per component and rejects a merged list with
        132000; PickyAssist counts distinct variables and rejects it from the other
        side. Verified against the live template: only the reason question has a
        header, carrying the asset name.

        The order is the order the variables appear in the approved template. There are
        no names — get the order wrong and the message reads plausibly and says something
        untrue, which is worse than failing.
        """
        if payload.get("type") == "eta_request":
            # {{1}} asset, {{2}} reason. Falls back to the escalation template ("please
            # attend") when unapproved — degraded but delivered, and a numeric reply to
            # it still lands as the estimate, because reply routing keys on the payload
            # type we STORED, not on which template carried it.
            template = self.eta_template or self.escalation_template
            if template == self.eta_template and template:
                return template, [
                    self._asset_label(payload),                       # {{1}} Loom 91
                    payload.get("reason_label") or "a fault",         # {{2}} Electrical fault
                ], []
        if payload.get("type") == "reason_prompt":
            # The gap travels IN THE PAYLOAD, computed by escalation.fire from the ladder
            # that scheduled this prompt — one source for the timing and the promise.
            # The fallback recomputes from the same ladder rather than consulting a
            # second config key that could disagree with it.
            reprompt = payload.get("reprompt_minutes")
            if reprompt is None and self.cfg is not None:
                from ..core.escalation import _minutes_to_next_step
                ladder = self.cfg.unknown_ladder
                first_ask = next((i for i, r in enumerate(ladder)
                                  if r.get("action") == "ask_reason"), 0)
                reprompt = _minutes_to_next_step(ladder, first_ask)
            return self.prompt_template, [
                self._asset_label(payload),          # body {{1}} Loom 91
                self._minutes_down(payload),         # body {{2}} 17
                str(int(reprompt or 0)),             # body {{3}} 25
            ], [self._asset_label(payload)]          # header: "Loom 91 stopped"
        # Everything else — escalation pages AND the hours-estimate question. The estimate
        # question virtually always rides the free-text path (build() checks the window
        # first, and it is sent seconds after the person's own reply, which opens it);
        # this template shape is only the out-of-window fallback, where "still down,
        # please attend" is the best an approved template can say. Their next reply
        # reopens the window and the follow-up ask goes as the real question.
        return self.escalation_template, [
            self._asset_label(payload),              # {{1}} Weaving Loom 91
            payload.get("reason_label") or "Not yet reported",   # {{2}} Electrical fault
            self._minutes_down(payload),             # {{3}} 32
        ], []

    # --- payload shaping -----------------------------------------------------

    def _text_body(self, to: str, payload: dict) -> dict:
        entry = {"number": to, "message": payload.get("text") or ""}
        ref = self._reference(payload)
        if ref:
            entry["reference_number"] = ref
        return {"token": self.token, "application": str(self.application), "data": [entry]}

    def _template_body(self, to: str, payload: dict) -> dict:
        template_id, values, header = self.template_for(payload)
        entry = {"number": to, "template_message": values, "language": self.language}
        if header:
            entry["template_header"] = header
        return {
            "token": self.token,
            "application": int(self.application),
            "template_id": template_id,
            "language": self.language,
            "data": [entry],
        }

    @staticmethod
    def _reference(payload: dict) -> str:
        """Our own id echoed back on delivery reports, so a PickyAssist row can be tied
        to the incident it came from without guessing."""
        inc, rung = payload.get("incident_id"), payload.get("rung")
        return f"inc:{inc}:rung:{rung}" if inc is not None else ""

    def build(self, recipient: str, payload: dict) -> dict:
        to = normalise_msisdn(recipient)
        template_id, _, _ = self.template_for(payload)
        # Inside the 24-hour service window free text is allowed and reads better;
        # outside it, only an approved template is delivered at all. No approved template
        # for this message type yet? Free text is the only thing left to try. It fails
        # with 802 outside a window, which is at least a loud, attributable failure
        # rather than sending nothing.
        if not template_id or self.window_open(recipient):
            return self._text_body(to, payload)
        return self._template_body(to, payload)

    # --- send ----------------------------------------------------------------

    def send(self, recipient: str, payload: dict) -> str:
        if not self.configured:
            return self._fallback.send(recipient, payload)

        resp = requests.post(f"{self.base}{PUSH_PATH}",
                             json=self.build(recipient, payload),
                             headers={"Content-Type": "application/json"},
                             timeout=15)
        # Deliberately not raise_for_status(): PickyAssist answers 200 with an error body,
        # so the HTTP status tells you almost nothing. The body's `status` is the truth.
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            raise RuntimeError(
                f"PickyAssist returned unparseable body (HTTP {resp.status_code}): "
                f"{resp.text[:300]}")

        status = data.get("status")
        if status != STATUS_ACCEPTED:
            detail = PERMANENT_STATUSES.get(status)
            message = (f"PickyAssist rejected the send: status={status} "
                       f"{data.get('message', '')}".strip())
            if detail:
                raise PermanentSendError(f"{message} — {detail}")
            raise RuntimeError(message)

        # status 100 means ACCEPTED, not delivered. Never raise past this point: the send
        # has already happened, and raising would make the outbox send it a second time.
        items = data.get("data") or []
        msg_id = items[0].get("msg_id") if items and isinstance(items[0], dict) else None
        return str(msg_id or data.get("push_id") or "pa-accepted")[:128]


# The registry and tests refer to WhatsAppNotifier; this alias is for anyone reading
# call sites and wondering which provider is behind it.
PickyAssistNotifier = WhatsAppNotifier
