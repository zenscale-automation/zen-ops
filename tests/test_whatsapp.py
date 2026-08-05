"""Meta WhatsApp Cloud API notifier: template vs free-form, and number normalisation."""

from datetime import timedelta

from app import clock, db
from app.notifiers.whatsapp import WhatsAppNotifier, normalise_msisdn


def _configured(monkeypatch, cfg):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456")
    return WhatsAppNotifier(cfg)


def test_msisdn_normalisation_bridges_config_and_meta():
    """routing.yaml carries '+91 90000 00001'; Meta sends '919000000001'."""
    for raw in ("+919000000001", "+91 90000 00001", "91-90000-00001", "919000000001"):
        assert normalise_msisdn(raw) == "919000000001"


def test_outside_the_window_it_must_be_a_template(cfg, monkeypatch):
    """Business-initiated messages are template-only. Sending free text here gets a
    'Re-engagement message' rejection from Meta and the fault goes unreported."""
    n = _configured(monkeypatch, cfg)
    body = n.build("+919000000005", {"type": "reason_prompt", "text": "Loom 5 stopped"})
    assert body["type"] == "template"
    assert body["to"] == "919000000005", "the '+' must be stripped for Meta"
    assert body["template"]["name"] == "ops_core_alert"
    params = body["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["Loom 5 stopped"], \
        "the whole rendered body rides in ONE variable, so reasons.yaml stays editable"


def test_inside_the_window_it_switches_to_free_text(cfg, monkeypatch):
    """A reply opens a 24h service window: free-form is allowed and is not billed."""
    n = _configured(monkeypatch, cfg)
    db.execute("INSERT INTO inbound_raw(channel, received_at, body, sender)"
               " VALUES ('whatsapp', ?, '1', '919000000005')", (clock.now_iso(),))
    body = n.build("+919000000005", {"type": "escalation", "text": "Still down"})
    assert body["type"] == "text" and body["text"]["body"] == "Still down"


def test_window_expires_after_24_hours(cfg, monkeypatch):
    n = _configured(monkeypatch, cfg)
    db.execute("INSERT INTO inbound_raw(channel, received_at, body, sender)"
               " VALUES ('whatsapp', ?, '1', '919000000005')", (clock.now_iso(),))
    assert n.build("+919000000005", {"text": "x"})["type"] == "text"

    clock.CLOCK.set_virtual(clock.now() + timedelta(hours=25))
    assert n.build("+919000000005", {"text": "x"})["type"] == "template", \
        "past 24h the window is shut and only a template is accepted"


def test_a_different_persons_window_does_not_leak(cfg, monkeypatch):
    n = _configured(monkeypatch, cfg)
    db.execute("INSERT INTO inbound_raw(channel, received_at, body, sender)"
               " VALUES ('whatsapp', ?, '1', '919000000005')", (clock.now_iso(),))
    assert n.build("+919000000006", {"text": "x"})["type"] == "template"


def test_unconfigured_falls_back_to_the_log_notifier(cfg, monkeypatch):
    for var in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"):
        monkeypatch.delenv(var, raising=False)
    n = WhatsAppNotifier(cfg)
    assert n.configured is False
    assert n.send("+919000000005", {"text": "hello"}).startswith("log-")
