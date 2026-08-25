"""Background workers.

The design runs the poller, ticker and outbox as async tasks inside one process. On
Flask (WSGI) they run as background daemon threads inside the same process instead —
still "exactly one instance runs" (Rule 3), which at 44 looms and a handful of users is
simpler and entirely adequate. Each worker exposes the same tick() the tests call
directly; the thread just calls it on a cadence and records a heartbeat so /health can
prove liveness.
"""

from __future__ import annotations

import logging
import os
import threading

from . import clock, config, db
from .core import outbox, poller, ticker
from .sources import build_source

log = logging.getLogger("ops.workers")


class _Worker(threading.Thread):
    def __init__(self, name: str, fn, interval: float, supervisor: "Supervisor"):
        super().__init__(name=name, daemon=True)
        self.fn = fn
        self.interval = interval
        self.sup = supervisor
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.fn()
                self.sup.beat(self.name)
            except Exception:  # a worker must never die on a transient error
                log.exception("worker %s tick failed", self.name)
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


class Supervisor:
    def __init__(self) -> None:
        self.cfg: config.Config | None = None
        self.source = None
        self._workers: list[_Worker] = []
        self._beats: dict[str, str] = {}
        self._last_poll: str | None = None
        self._lock = threading.Lock()
        self._started = False

    def beat(self, name: str) -> None:
        with self._lock:
            self._beats[name] = clock.now_iso()

    def _poll_tick(self) -> None:
        poller.poll_once(self.cfg, self.source)
        with self._lock:
            self._last_poll = clock.now_iso()

    def start(self, cfg: "config.Config") -> None:
        if self._started:
            return
        self.cfg = cfg
        self.source = build_source(cfg)
        try:
            poller.ensure_discovered_assets(cfg, self.source)
            self.source.seed()
        except Exception:
            log.exception("source init failed (will keep polling)")

        # cadences default to the design values; env overrides help demos/tests run fast
        poll_seconds = int(os.environ.get("OPS_POLL_SECONDS", cfg.source_setting("poll_seconds", 30)))
        ticker_seconds = int(os.environ.get("OPS_TICKER_SECONDS", 30))
        outbox_seconds = int(os.environ.get("OPS_OUTBOX_SECONDS", 5))
        self._workers = [
            _Worker("poller", self._poll_tick, poll_seconds, self),
            _Worker("ticker", lambda: ticker.tick(cfg), ticker_seconds, self),
            _Worker("outbox", lambda: outbox.drain(cfg), outbox_seconds, self),
        ]
        # Seed a heartbeat for every worker before starting them. Without this a worker
        # that raises on its very first tick and every tick after never enters _beats at
        # all — and /health iterates _beats, so it cannot appear in `stale` and `ok`
        # stays true. The most complete failure available was the one the health check
        # could not see.
        started_at = clock.now_iso()
        with self._lock:
            for w in self._workers:
                self._beats.setdefault(w.name, started_at)
        for w in self._workers:
            w.start()
        self._started = True
        log.info("workers started: poller(%ss) ticker(30s) outbox(5s)", poll_seconds)

    def stop(self) -> None:
        for w in self._workers:
            w.stop()
        self._started = False

    def health(self) -> dict:
        with self._lock:
            beats = dict(self._beats)
            last_poll = self._last_poll
        return {
            "started": self._started,
            "workers": beats,
            "last_poll_at": last_poll,
            "outbox_queued": outbox.depth(),
            # Queue depth alone IMPROVES when delivery fails — a channel failing 100% of
            # the time drains to zero and looks idle. The count of pages that gave up is
            # the metric that actually tracks "somebody was not called".
            "outbox_failed_1h": outbox.failed_since(3600),
            "escalations_wedged": _wedged_count(),
        }


def _wedged_count() -> int:
    """Escalations the ticker could not fire and parked. Any non-zero value means a fault
    exists that nobody will be paged about until a human looks."""
    try:
        from . import db
        r = db.query_one("SELECT COUNT(*) n FROM escalations WHERE status='wedged'")
        return int(r["n"]) if r else 0
    except Exception:
        return 0


SUPERVISOR = Supervisor()
