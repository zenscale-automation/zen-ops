"""Shared pytest fixtures. Each test runs against the MySQL database with a clean
schema and a fixed virtual clock (shift A, away from a boundary unless a test moves it).
"""

from datetime import datetime, timezone

import pytest

from app import clock, config, db

# 04:00 UTC == 09:30 IST -> shift A, far from any changeover boundary
BASE = datetime(2026, 8, 5, 4, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def cfg():
    c = config.load()
    db.init(c.db_params(), c.table_prefix)
    db.reset_all()
    db.migrate()
    clock.CLOCK.set_virtual(BASE)
    yield c
    clock.CLOCK.real()
