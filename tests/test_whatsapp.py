"""WhatsApp notifier tests — PickyAssist (application 121).

Two things carry the whole integration and both are silent when wrong:

  * Choosing template vs free text. Free text outside the 24-hour window is rejected
    with 802 and the fault goes unreported. There is no exception at the shed floor —
    just nobody being called.
  * Recognising a permanent failure. The outbox drains serially, so retrying a dead API
    key eight times blocks every other page for ten minutes.

The request shapes here are the ones verified against the live PickyAssist endpoint,
not the ones in their documentation, which disagrees with itself about the base URL.
"""
from datetime import timedelta

import pytest

from app import clock, db
from app.notifiers.base import PermanentSendError
from app.notifiers.whatsapp import WhatsAppNotifier, normalise_msisdn

PROMPT_TID = "VG7935"
ESC_TID = "VG8811"


@pytest.fixture()
def n(cfg, monkeypatch):
    monkeypatch.setenv("PICKYASSIST_TOKEN", "test-token")
    monkeypatch.setenv("PICKYASSIST_APPLICATION", "121")
    monkeypatch.setenv("PICKYASSIST_PROMPT_TEMPLATE_ID", PROMPT_TID)
    monkeypatch.setenv("PICKYASSIST_ESCALATION_TEMPLATE_ID", ESC_TID)
    return WhatsAppNotifier(cfg)


def _inbound(sender: str, at=None):
    db.execute("INSERT INTO inbound_raw(channel, received_at, body, sender)"
               " VALUES ('whatsapp', ?, 'ok', ?)", (at or clock.now_iso(), sender))


# --- number handling ------------------------------------------------------------

def test_msisdn_normalisation_bridges_config_and_provider():
    # routing.yaml carries "+91 90000 00001"; PickyAssist wants and reports "919000000001".
    for raw in ("+919000000001", "+91 90000 00001", "919000000001", "+91-90000-00001"):
        assert normalise_msisdn(raw) == "919000000001"


def test_the_number_sent_never_carries_a_plus(n):
    body = n.build("+919000000005", {"type": "reason_prompt", "asset_ref": "loom_91"})
    assert body["data"][0]["number"] == "919000000005"


# --- template vs free text ------------------------------------------------------

def test_outside_the_window_it_must_be_a_template(n):
    body = n.build("+919000000005", {"type": "reason_prompt", "asset_ref": "loom_91",
                                     "opened_at": clock.now_iso()})
    assert body["template_id"] == PROMPT_TID
    assert "message" not in body["data"][0]


def test_inside_the_window_it_switches_to_free_text(n, cfg):
    _inbound("919000000005")
    body = n.build("+919000000005", {"type": "escalation", "text": "Still down"})
    assert "template_id" not in body
    assert body["data"][0]["message"] == "Still down"


def test_window_expires_after_24_hours(n, cfg):
    _inbound("919000000005")
    assert "template_id" not in n.build("+919000000005", {"text": "x"})
    clock.CLOCK.set_virtual(clock.now() + timedelta(hours=25))
    assert "template_id" in n.build("+919000000005", {"text": "x"}), \
        "a stale window must fall back to a template, or the send is rejected with 802"


def test_one_persons_window_does_not_open_anothers(n, cfg):
    _inbound("919000000005")
    assert "template_id" in n.build("+919000000006", {"text": "x"})


def test_with_no_approved_template_it_still_tries_free_text(cfg, monkeypatch):
    # A loud 802 is better than sending nothing at all and calling it success.
    monkeypatch.setenv("PICKYASSIST_TOKEN", "t")
    monkeypatch.delenv("PICKYASSIST_PROMPT_TEMPLATE_ID", raising=False)
    body = WhatsAppNotifier(cfg).build("+919000000005",
                                       {"type": "reason_prompt", "text": "fallback"})
    assert body["data"][0]["message"] == "fallback"


# --- template variables ---------------------------------------------------------

def test_prompt_variables_are_positional_and_in_template_order(n, cfg):
    opened = clock.plus_seconds(-17 * 60)
    tid, values, header = n.template_for({"type": "reason_prompt", "asset_ref": "loom_91",
                                          "opened_at": opened, "reprompt_minutes": 15})
    assert tid == PROMPT_TID
    assert values == ["Loom 91", "17", "15"]
    assert header == ["Loom 91"], \
        "the approved template has a text header carrying the asset name"


def test_minutes_are_recomputed_at_send_time_not_at_enqueue(n, cfg):
    # The payload is built inside the ticker transaction and may sit in the outbox
    # through a retry ladder. A frozen number reads as a lie on the phone.
    opened = clock.plus_seconds(-5 * 60)
    payload = {"type": "reason_prompt", "asset_ref": "loom_91", "opened_at": opened}
    assert n.template_for(payload)[1][1] == "5"
    clock.CLOCK.set_virtual(clock.now() + timedelta(minutes=30))
    assert n.template_for(payload)[1][1] == "35"


def test_escalation_variables_name_the_reason(n, cfg):
    tid, values, header = n.template_for({"type": "escalation", "asset_label": "Loom 94",
                                          "reason_label": "Electrical fault",
                                          "minutes_down": 32})
    assert tid == ESC_TID
    assert values == ["Loom 94", "Electrical fault", "32"]
    assert header == [], "the escalation template has no header"


def test_an_escalation_with_no_reason_yet_still_reads_sensibly(n, cfg):
    _, values, _ = n.template_for({"type": "escalation", "asset_label": "Loom 94",
                                   "minutes_down": 12})
    assert values[1] == "Not yet reported"


# --- the wire format ------------------------------------------------------------

def test_the_token_travels_in_the_body_not_a_header(n, cfg):
    # The single most important difference from every other WhatsApp provider.
    body = n.build("+919000000005", {"text": "x"})
    assert body["token"] == "test-token"


def test_template_sends_use_an_integer_application(n, cfg):
    # PickyAssist's own examples show a string for free text and an int for templates.
    # Matching both exactly costs nothing and is the only shape verified to work.
    tmpl = n.build("+919000000005", {"type": "reason_prompt", "asset_ref": "loom_91"})
    assert tmpl["application"] == 121
    _inbound("919000000005")
    assert n.build("+919000000005", {"text": "x"})["application"] == "121"


def test_the_reference_ties_a_send_back_to_its_incident(n, cfg):
    _inbound("919000000005")
    body = n.build("+919000000005", {"text": "x", "incident_id": 412, "rung": 0})
    assert body["data"][0]["reference_number"] == "inc:412:rung:0"


# --- responses ------------------------------------------------------------------

class _Resp:
    def __init__(self, payload, code=200):
        self._p, self.status_code, self.content = payload, code, b"x"
        self.text = str(payload)

    def json(self):
        return self._p


def test_accepted_returns_the_per_recipient_message_id(n, cfg, monkeypatch):
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp(
        {"status": 100, "push_id": "7478630",
         "data": [{"msg_id": "9844217", "number": "919000000005"}]}))
    assert n.send("+919000000005", {"text": "x"}) == "9844217"


def test_accepted_with_no_message_id_still_returns_something_truthy(n, cfg, monkeypatch):
    # The outbox stores this and asserts it is non-empty. Raising here instead would
    # re-send a message that has already been accepted.
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp({"status": 100}))
    assert n.send("+919000000005", {"text": "x"})


@pytest.mark.parametrize("status", [401, 801, 802])
def test_known_failures_are_permanent_and_not_retried(n, cfg, monkeypatch, status):
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp(
        {"status": status, "message": "nope"}))
    with pytest.raises(PermanentSendError):
        n.send("+919000000005", {"text": "x"})


def test_an_unknown_status_is_retryable(n, cfg, monkeypatch):
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp({"status": 999}))
    with pytest.raises(RuntimeError) as e:
        n.send("+919000000005", {"text": "x"})
    assert not isinstance(e.value, PermanentSendError)


def test_an_error_body_behind_http_200_is_not_treated_as_success(n, cfg, monkeypatch):
    # PickyAssist answers 200 with an error body, so raise_for_status never fires.
    # Before this was handled, a rejected send was recorded as delivered and nobody
    # was paged and nobody knew.
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp({"status": 801}, code=200))
    with pytest.raises(Exception):
        n.send("+919000000005", {"text": "x"})


def test_unconfigured_falls_back_to_the_log_notifier(cfg, monkeypatch):
    monkeypatch.delenv("PICKYASSIST_TOKEN", raising=False)
    out = WhatsAppNotifier(cfg).send("+919000000005", {"text": "x"})
    assert out.startswith("log-")


def test_force_template_overrides_an_open_window(cfg, monkeypatch):
    """Meta blocks free-form sends from a number whose display name is unapproved
    (131037) while still delivering templates. Found live: PickyAssist accepts the
    free text, Meta drops it, and nothing on our side errors. The flag routes every
    send down the path that actually arrives."""
    monkeypatch.setenv("PICKYASSIST_TOKEN", "t")
    monkeypatch.setenv("PICKYASSIST_PROMPT_TEMPLATE_ID", PROMPT_TID)
    monkeypatch.setenv("PICKYASSIST_FORCE_TEMPLATE", "1")
    _inbound("919000000005")                        # window is open…
    n2 = WhatsAppNotifier(cfg)
    body = n2.build("+919000000005", {"type": "reason_prompt", "asset_ref": "loom_91"})
    assert body.get("template_id") == PROMPT_TID, "…but the template path must win"
