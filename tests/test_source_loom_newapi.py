"""Conformance tests for the MOVED loom API (https://3.6.46.119/api).

The existing test fake in test_source_loom.py diverges from the real server in two
ways that hide live bugs:
  1. rows carry INT weft/warp; the real feed sends JSON floats (0.0/1.0)
  2. `since` is treated as EXCLUSIVE (ts > since); the real server is INCLUSIVE (ts >= since)

These tests model the server as it actually behaves (verified against the live API).
"""
from datetime import datetime, timedelta

from app import clock
from app.sources.base import IncidentOpened
from app.sources.weaving_loom_api import WeavingLoomApiSource

_T0 = datetime(2026, 8, 5, 9, 0, 0)


def _frow(machine, secs, rpm, weft=1.0, warp=1.0):
    """A row in the REAL new-API shape: every numeric is a float, machine is a str."""
    return {"ts": (_T0 + timedelta(seconds=secs)).isoformat(timespec="milliseconds"),
            "device": "esp32-01", "machine": str(machine),
            "weft": float(weft), "warp": float(warp), "rpm": float(rpm),
            "rpm_raw_sensor": 1.0 if rpm > 0 else 0.0, "ExtData": 1.0}


def _serve_inclusive(rows, page_size=5000):
    """The REAL server: ts >= since, and `to` is the last row's ts."""
    state = {"now": None}

    def pages(since):
        vis = [r for r in rows if state["now"] is None or r["ts"] <= state["now"]]
        if not vis:
            return [], since, False
        if since is None:
            cut = (datetime.fromisoformat(vis[-1]["ts"]) - timedelta(minutes=1)
                   ).isoformat(timespec="milliseconds")
            sel = [r for r in vis if r["ts"] >= cut]
        else:
            sel = [r for r in vis if r["ts"] >= since]      # INCLUSIVE
        page = sel[:page_size]
        return page, (page[-1]["ts"] if page else since), len(sel) > page_size

    return pages, state


def _src(cfg, pages):
    s = WeavingLoomApiSource(cfg)
    s._fetch_page = pages
    return s


def test_float_weft_is_recognised_as_a_thread_break(cfg):
    """weft arrives as 0.0, not 0. A break on a loom still turning must open THREAD_STOP."""
    rows = [_frow(7, s, 300.0) for s in range(60)]
    rows += [_frow(7, s, 250.0, weft=0.0) for s in range(60, 180)]
    pages, state = _serve_inclusive(rows)
    state["now"] = rows[-1]["ts"]
    src = _src(cfg, pages)
    src._last = {"loom_7": "RUNNING"}
    src._cursor = rows[0]["ts"]
    ev = src.poll(clock.now_iso())
    assert [e.asset_ref for e in ev] == ["loom_7"]
    assert ev[0].condition == "THREAD_STOP"


def test_float_thread_break_is_labelled_thread_stop_not_stopped(cfg):
    """A break that ALSO drops rpm must still be labelled THREAD_STOP, not STOPPED."""
    rows = [_frow(8, s, 300.0) for s in range(60)]
    rows += [_frow(8, s, 0.0, warp=0.0) for s in range(60, 180)]
    pages, state = _serve_inclusive(rows)
    state["now"] = rows[-1]["ts"]
    src = _src(cfg, pages)
    src._last = {"loom_8": "RUNNING"}
    src._cursor = rows[0]["ts"]
    ev = src.poll(clock.now_iso())
    assert [(e.asset_ref, e.condition) for e in ev] == [("loom_8", "THREAD_STOP")]


def test_float_thread_break_does_not_resolve_an_open_incident(cfg):
    """A loom mid-thread-break that is still turning must NOT read as running."""
    rows = [_frow(9, s, 300.0, weft=0.0) for s in range(60)]
    pages, state = _serve_inclusive(rows)
    state["now"] = rows[-1]["ts"]
    src = _src(cfg, pages)
    src._last = {"loom_9": "STOPPED"}
    src._cursor = rows[0]["ts"]
    assert src.poll(clock.now_iso()) == [], "a live thread break must not self-resolve"


def test_dead_feed_ages_into_offline_despite_inclusive_since(cfg, monkeypatch):
    """`since` is INCLUSIVE: a dead feed keeps replaying its final boundary row, so every
    machine looks like it 'reported this poll' and never ages. The fleet-dark guard must
    still be reached (it logs and returns [] by design) rather than being bypassed."""
    from app import clock as appclock
    rows = [_frow(m, s, 300.0) for m in (21, 23) for s in range(30)]
    rows.sort(key=lambda r: r["ts"])
    pages, state = _serve_inclusive(rows)
    state["now"] = rows[-1]["ts"]
    src = _src(cfg, pages)
    src._cursor = rows[0]["ts"]
    assert src.poll(clock.now_iso()) == []
    assert src._server_now is not None

    base = appclock.CLOCK.now()
    appclock.CLOCK.set_virtual(base + timedelta(minutes=30))
    src.poll(clock.now_iso())
    # After 30 minutes of a totally dead feed, no machine may still read as RUNNING.
    assert not any(v == "RUNNING" for v in src._last.values()), (
        "feed dead 30min but adapter still reports RUNNING: %r" % (src._last,))
