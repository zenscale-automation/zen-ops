"""Shared pytest fixtures. Each test runs against the MySQL database with a clean
schema and a fixed virtual clock (shift A, away from a boundary unless a test moves it).
"""

from datetime import datetime, timezone

import pytest

from app import clock, config, db

# 04:00 UTC == 09:30 IST -> shift A, far from any changeover boundary
BASE = datetime(2026, 8, 5, 4, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _keep_the_network_out(monkeypatch):
    """No test may send a real message.

    ops-core has no "do not really send" switch — a message queued for a person is a
    message sent to that person, decided by the roster alone. That makes keeping the
    suite off the wire a property of the SUITE, which is where it belongs: the tests run
    against a real database with the live box's .env, so without this every drain would
    put a WhatsApp message on somebody's actual phone. Tests that exercise the provider
    itself construct their notifier directly and stub requests.post.
    """
    from app import notifiers
    from app.notifiers.log import LogNotifier

    notifiers.reset_cache()
    monkeypatch.setattr(notifiers, "get_notifier",
                        lambda cfg, channel: LogNotifier(cfg, via=channel))
    yield
    notifiers.reset_cache()


@pytest.fixture()
def cfg():
    c = config.load()
    db.init(c.db_params(), c.table_prefix)
    db.reset_all()
    db.migrate()
    clock.CLOCK.set_virtual(BASE)
    yield c
    clock.CLOCK.real()
