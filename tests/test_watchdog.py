"""The alarm that fires when nothing else can.

Every other alert in this system travels over WhatsApp. On 26 August the pilot spent a
full day with Meta rejecting every single message (131037) while incidents opened,
ladders fired and the outbox filled with rows marked sent — a plant where nobody was
being called, wearing the exact face of a plant where nobody needed calling. These tests
pin the two properties that make the second channel worth having:

  * the FIRST undelivered message raises the alarm immediately, not one interval later,
  * and the alarm repeats on a cadence without ever double-posting, because "you were
    told twice" and "you were told nothing" are both failures.
"""
import json
from datetime import timedelta

import pytest

from app import clock, config, db
from app.core import watchdog


@pytest.fixture()
def alerting(cfg):
    """cfg with the watchdog on a 30-minute cadence and the digest disabled, so the two
    behaviours are tested apart from each other."""
    cfg.alerts = {"send_watchdog": {"enabled": True, "channel": "whatsapp",
                                    "repeat_minutes": 30,
                                    "assume_delivered_after_minutes": 15},
                  "daily_digest": {"enabled": False}}
    return cfg


def _msg(status, *, at=None, delivery=None, error=None, channel="whatsapp"):
    at = at or clock.now_iso()
    db.execute(
        "INSERT INTO outbox(channel, recipient, payload, dedupe_key, attempts,"
        " next_try_at, sent_at, provider_msg_id, status, delivery_status,"
        " delivery_error) VALUES (?,?,?,?,1,?,?,?,?,?,?)",
        (channel, "+919000000005", '{"type":"escalation"}', f"k:{at}:{status}:{error}",
         at, at, f"p{at}", status, delivery, error))


def _alerts():
    return [json.loads(r["payload"])["text"] for r in db.query(
        "SELECT payload FROM outbox WHERE channel='gchat' ORDER BY id")]


# --- is the send path actually reaching phones ----------------------------------

def test_a_message_that_only_reached_the_provider_is_not_success(alerting):
    # PickyAssist answers 100 in milliseconds; Meta rejects seconds later. Treating
    # 'sent' as proof of arrival is exactly the blindness this exists to remove.
    _msg("sent")
    st = watchdog.send_path_state(alerting)
    assert st["last_success_at"] is None, \
        "a send with no delivery report yet must not count as delivery"


def test_a_confirmed_delivery_counts_and_an_old_unreported_send_does_too(alerting):
    _msg("sent", delivery="delivered")
    assert watchdog.send_path_state(alerting)["last_success_at"]

    db.execute("DELETE FROM outbox")
    _msg("sent", at=clock.plus_seconds(-3600))       # older than the 15-min grace
    assert watchdog.send_path_state(alerting)["last_success_at"], \
        "if reports stop arriving entirely the alarm must not latch on forever"


def test_the_first_undelivered_message_raises_the_alarm_immediately(alerting):
    _msg("failed", error="(#131037) display name needs approval")
    out = watchdog.check_send_path(alerting)

    assert out["down"] is True and out["interval"] == 0 and out["posted"] is True
    text = _alerts()[0]
    assert "DOWN" in text
    assert "131037" in text, "the provider's own reason belongs in the alert"


def test_the_same_outage_is_not_reported_twice_in_one_interval(alerting):
    _msg("failed", error="boom")
    watchdog.check_send_path(alerting)
    for _ in range(5):                                  # five more ticker passes
        clock.CLOCK.set_virtual(clock.now() + timedelta(minutes=4))
        watchdog.check_send_path(alerting)
    assert len(_alerts()) == 1, "one alarm per interval, however often the ticker runs"


def test_it_repeats_on_the_configured_cadence(alerting):
    _msg("failed", error="boom")
    watchdog.check_send_path(alerting)

    clock.CLOCK.set_virtual(clock.now() + timedelta(minutes=31))
    watchdog.check_send_path(alerting)
    clock.CLOCK.set_virtual(clock.now() + timedelta(minutes=31))
    watchdog.check_send_path(alerting)

    texts = _alerts()
    assert len(texts) == 3
    assert "still down" in texts[1] and "min" in texts[1], \
        "a repeat should say how long it has been broken, not repeat the first message"


def test_the_outage_clock_runs_from_the_first_failure_not_the_latest(alerting):
    _msg("failed", at=clock.plus_seconds(-3600), error="first")
    _msg("failed", error="latest")
    out = watchdog.check_send_path(alerting)
    # An hour of failures already elapsed: this is interval 2 of an ongoing outage, and
    # counting from the newest failure would restart the clock on every retry.
    assert out["interval"] == 2
    assert out["failed"] == 2, "the count says how many people were not reached"


def test_recovery_is_announced_once_and_then_forgotten(alerting):
    _msg("failed", error="boom")
    watchdog.check_send_path(alerting)

    clock.CLOCK.set_virtual(clock.now() + timedelta(minutes=10))
    _msg("sent", delivery="delivered")
    out = watchdog.check_send_path(alerting)
    assert out.get("recovered") is True
    assert "working again" in _alerts()[-1]

    before = len(_alerts())
    clock.CLOCK.set_virtual(clock.now() + timedelta(minutes=40))
    watchdog.check_send_path(alerting)
    assert len(_alerts()) == before, "an all-clear is news once, not every 30 minutes"


def test_a_failed_alert_does_not_count_as_the_plant_being_down(alerting):
    # The alerts themselves live in the same outbox. If a Chat post fails, that must not
    # register as WhatsApp being broken — or the watchdog reports on its own injuries.
    _msg("failed", channel="gchat", error="chat is down")
    assert watchdog.send_path_state(alerting)["down"] is False


def test_a_disabled_watchdog_stays_silent(alerting):
    alerting.alerts["send_watchdog"]["enabled"] = False
    _msg("failed", error="boom")
    assert watchdog.check_send_path(alerting) == {"skipped": "disabled"}
    assert _alerts() == []


# --- the daily digest ------------------------------------------------------------

@pytest.fixture()
def digesting(cfg):
    cfg.alerts = {"daily_digest": {"enabled": True, "at": "06:15", "window_hours": 24},
                  "send_watchdog": {"enabled": False}}
    return cfg


def test_the_digest_goes_out_once_a_day_however_often_the_ticker_runs(digesting):
    # The virtual clock sits at 09:30 IST — past the 06:15 send time.
    assert watchdog.check_daily_digest(digesting)["sent"] is True
    for _ in range(10):
        clock.CLOCK.set_virtual(clock.now() + timedelta(minutes=30))
        watchdog.check_daily_digest(digesting)
    assert len(_alerts()) == 1

    clock.CLOCK.set_virtual(clock.now() + timedelta(days=1))
    watchdog.check_daily_digest(digesting)
    assert len(_alerts()) == 2, "the next day gets its own digest"


def test_nothing_goes_out_before_the_configured_time(digesting):
    digesting.alerts["daily_digest"]["at"] = "23:59"
    assert watchdog.check_daily_digest(digesting)["sent"] is False
    assert _alerts() == []


def test_the_digest_names_what_is_stopped_right_now(digesting):
    from app.core import incidents
    with db.transaction() as c:
        incidents.ensure_asset(c, digesting, "loom_91")
    incidents.open_incident(digesting, "loom_91", "STOPPED",
                            at=clock.plus_seconds(-45 * 60))

    watchdog.check_daily_digest(digesting)
    text = _alerts()[0]
    assert "Loom 91" in text and "45 min" in text
    assert "no reason given yet" in text, \
        "a stop nobody has explained is the thing the reader needs to see"


def test_the_digest_says_so_when_the_questions_are_not_reaching_anyone(digesting):
    digesting.alerts["send_watchdog"] = {"enabled": True, "channel": "whatsapp",
                                         "repeat_minutes": 30}
    _msg("failed", error="(#131037)")
    watchdog.check_daily_digest(digesting)
    assert "sending is down" in _alerts()[0].lower()


# --- configuration ---------------------------------------------------------------

def test_a_broken_cadence_is_refused_at_config_time(cfg):
    cfg.alerts = {"send_watchdog": {"enabled": True, "repeat_minutes": 0}}
    with pytest.raises(config.ConfigError) as e:
        config.validate(cfg)
    assert "repeat_minutes" in str(e.value)


def test_a_broken_digest_time_is_refused_at_config_time(cfg):
    cfg.alerts = {"daily_digest": {"enabled": True, "at": "quarter past six"}}
    with pytest.raises(config.ConfigError) as e:
        config.validate(cfg)
    assert "HH:MM" in str(e.value)
