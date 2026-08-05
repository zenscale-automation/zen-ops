"""Outbox: dedupe (at-least-once safety), draining, and retry backoff."""

from app import clock, db
from app.core import outbox


def _enqueue(key, channel="log", recipient="+91X", payload=None):
    with db.transaction() as c:
        return outbox.enqueue(c, channel, recipient, payload or {"type": "t", "text": "hi"}, key)


def test_dedupe_key_prevents_duplicate(cfg):
    assert _enqueue("tkt:1:rung:0:ravi_k") is True
    assert _enqueue("tkt:1:rung:0:ravi_k") is False   # same key -> ignored
    n = db.query_one("SELECT COUNT(*) n FROM outbox WHERE dedupe_key=?", ("tkt:1:rung:0:ravi_k",))["n"]
    assert n == 1


def test_drain_marks_sent(cfg):
    _enqueue("k1")
    summary = outbox.drain(cfg)
    assert summary["sent"] == 1
    row = db.query_one("SELECT status, provider_msg_id FROM outbox WHERE dedupe_key='k1'")
    assert row["status"] == "sent" and row["provider_msg_id"]


def test_failed_delivery_retries_with_backoff(cfg, monkeypatch):
    class Boom:
        def send(self, recipient, payload):
            raise RuntimeError("provider down")

    import app.notifiers as notifiers
    monkeypatch.setattr(notifiers, "get_notifier", lambda cfg, ch: Boom())

    _enqueue("k2")
    summary = outbox.drain(cfg)
    assert summary["retried"] == 1 and summary["sent"] == 0
    row = db.query_one("SELECT status, attempts, next_try_at FROM outbox WHERE dedupe_key='k2'")
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["next_try_at"] > clock.now_iso()   # scheduled into the future
