"""The weaving adapter's real /data semantics: rpm-derived stop/resume, machine->asset_ref
mapping, exclusion, and cursor advance — exercised with a stubbed page fetch (no HTTP)."""

from app import clock, db
from app.sources.base import IncidentOpened, IncidentResolved
from app.sources.weaving_loom_api import WeavingLoomApiSource


class StubSource(WeavingLoomApiSource):
    """Feeds scripted /data rows instead of hitting the network."""
    rows: list = []
    to: str = "cursor-0"

    def _fetch_page(self, since):
        return list(self.rows), self.to, False


def _row(machine, rpm, ts="2026-08-05T09:00:00.000"):
    return {"ts": ts, "device": "esp32-01", "machine": machine, "rpm": rpm,
            "weft": 1, "warp": 1, "rpm_raw_sensor": 1 if rpm > 0 else 0, "ExtData": 1}


def test_rpm_zero_opens_and_resume_resolves(cfg):
    s = StubSource(cfg)
    # baseline: everything running -> seed sets state, emits nothing
    s.rows = [_row("3", 300.0), _row("4", 300.0), _row("99_test", 300.0)]
    s.to = "t0"
    s.seed()
    assert s._cursor == "t0"

    # loom 3 drops to rpm 0 -> IncidentOpened for loom_3 (machine->asset_ref mapping)
    s.rows = [_row("3", 0.0), _row("4", 300.0)]
    s.to = "t1"
    ev = s.poll(clock.now_iso())
    assert len(ev) == 1 and isinstance(ev[0], IncidentOpened) and ev[0].asset_ref == "loom_3"
    assert s._cursor == "t1"   # cursor advanced to the response `to`

    # still stopped -> no duplicate event
    assert s.poll(clock.now_iso()) == []

    # rpm returns -> IncidentResolved
    s.rows = [_row("3", 297.5), _row("4", 300.0)]
    ev = s.poll(clock.now_iso())
    assert len(ev) == 1 and isinstance(ev[0], IncidentResolved) and ev[0].asset_ref == "loom_3"


def test_excluded_machine_never_emits(cfg):
    s = StubSource(cfg)
    s.rows = [_row("99_test", 300.0)]
    s.seed()
    s.rows = [_row("99_test", 0.0)]   # the test loom stops
    assert s.poll(clock.now_iso()) == []   # excluded by source.yaml -> ignored


def test_discover_maps_machines_to_asset_refs(cfg):
    s = StubSource(cfg)
    s.rows = [_row("7", 300.0), _row("12", 0.0), _row("99_test", 300.0)]
    refs = {a["asset_ref"] for a in s.discover_assets()}
    assert refs == {"loom_7", "loom_12"}   # excluded loom_99_test dropped


# --- regression tests for the adapter audit -------------------------------------
# Each of these FAILED against the original adapter. They are written against the
# shipped departments/weaving/source.yaml, not a synthetic config, so they break if
# somebody edits the thresholds back.

from datetime import datetime, timedelta  # noqa: E402

_T0 = datetime(2026, 8, 5, 9, 0, 0)


def _arow(machine, secs, rpm, weft=1, warp=1):
    return {"ts": (_T0 + timedelta(seconds=secs)).isoformat(timespec="milliseconds"),
            "device": "esp32-01", "machine": str(machine), "weft": weft, "warp": warp,
            "rpm": rpm, "rpm_raw_sensor": 1 if rpm > 0 else 0, "ExtData": ""}


def _asource(cfg, pages):
    """pages: callable(since) -> (rows, to, truncated)"""
    from app.sources.weaving_loom_api import WeavingLoomApiSource
    src = WeavingLoomApiSource(cfg)
    src._fetch_page = pages
    return src


def _aserve(rows, page_size=500):
    state = {"now": None, "calls": 0}

    def pages(since):
        state["calls"] += 1
        vis = [r for r in rows if state["now"] is None or r["ts"] <= state["now"]]
        if not vis:
            return [], since, False
        if since is None:
            cut = (datetime.fromisoformat(vis[-1]["ts"]) - timedelta(minutes=1)).isoformat(
                timespec="milliseconds")
            sel = [r for r in vis if r["ts"] > cut]
        else:
            sel = [r for r in vis if r["ts"] > since]
        page = sel[:page_size]
        return page, (page[-1]["ts"] if page else since), len(sel) > page_size

    return pages, state


def test_stop_uses_rpm_threshold_not_zero(cfg):
    """A halted loom coasts at single-digit rpm. `rpm <= 0` never fires on it."""
    rows = [_arow(5, s, 300.0) for s in range(60)] + [_arow(5, s, 12.0) for s in range(60, 180)]
    pages, _ = _aserve(rows)
    src = _asource(cfg, pages)
    src._last = {"loom_5": "RUNNING"}
    src._cursor = rows[0]["ts"]
    ev = src.poll("2026-08-05T04:00:00+00:00")
    assert [e.asset_ref for e in ev] == ["loom_5"]
    assert ev[0].condition == "STOPPED"


def test_thread_break_stops_a_turning_loom(cfg):
    """weft low with rpm 250: production has stopped even though the machine turns."""
    rows = [_arow(7, s, 300.0) for s in range(60)]
    rows += [_arow(7, s, 250.0, weft=0) for s in range(60, 180)]
    pages, _ = _aserve(rows)
    src = _asource(cfg, pages)
    src._last = {"loom_7": "RUNNING"}
    src._cursor = rows[0]["ts"]
    ev = src.poll("2026-08-05T04:00:00+00:00")
    assert [e.asset_ref for e in ev] == ["loom_7"]
    assert ev[0].condition == "THREAD_STOP"


def test_machine_that_stops_reporting_goes_offline(cfg):
    """A dead edge device must not read as a healthy loom."""
    p1 = [_arow(9, s, 300.0) for s in range(30)] + [_arow(11, s, 300.0) for s in range(30)]
    p2 = [_arow(11, s, 300.0) for s in range(30, 400)]
    pages, state = _aserve(sorted(p1 + p2, key=lambda r: r["ts"]))
    src = _asource(cfg, pages)
    src._cursor = p1[0]["ts"]

    state["now"] = p1[-1]["ts"]
    assert src.poll("2026-08-05T04:00:00+00:00") == []      # both reporting

    state["now"] = p2[-1]["ts"]                             # loom 9 silent 370s
    ev = src.poll("2026-08-05T04:00:00+00:00")
    assert [(e.asset_ref, e.condition) for e in ev] == [("loom_9", "OFFLINE")]


def test_deep_backlog_resyncs_to_head_instead_of_crawling(cfg):
    """After an outage the cursor is hours stale. Replaying it would emit old
    transitions stamped `now`. The adapter must jump to the head instead."""
    rows = []
    for m in (13, 15):
        rows += [_arow(m, s, 300.0) for s in range(0, 10800, 2)]
        rows += [_arow(m, 10800 + s, 0.0) for s in range(60)]
    rows.sort(key=lambda r: r["ts"])
    pages, state = _aserve(rows, page_size=50)
    src = _asource(cfg, pages)
    src._last = {"loom_13": "RUNNING", "loom_15": "RUNNING"}
    src._cursor = rows[0]["ts"]
    ev = src.poll("2026-08-05T04:00:00+00:00")
    assert src._cursor == rows[-1]["ts"], "cursor should be at the head after resync"
    assert sorted(e.asset_ref for e in ev) == ["loom_13", "loom_15"]


def test_offline_ages_on_elapsed_time_not_on_incoming_rows(cfg, monkeypatch):
    """The dashboard's nowLive() lesson: if offline aging is anchored on the newest ts
    in the CURRENT poll, a feed that goes completely silent freezes the clock and no
    loom ever ages into OFFLINE. Age must advance on elapsed wall time."""
    from datetime import timezone
    from app import clock as appclock

    rows = [_arow(21, s, 300.0) for s in range(10)] + [_arow(23, s, 300.0) for s in range(10)]
    rows.sort(key=lambda r: r["ts"])
    pages, state = _aserve(rows)
    src = _asource(cfg, pages)
    src._cursor = rows[0]["ts"]

    state["now"] = rows[-1]["ts"]
    assert src.poll("2026-08-05T04:00:00+00:00") == []      # both reporting
    assert src._server_now is not None                      # server clock anchored

    # The feed now returns nothing at all — not one row, for either loom.
    state["now"] = "2026-08-05T00:00:00.000"
    base = appclock.CLOCK.now()
    assert src.poll("2026-08-05T04:00:00+00:00") == []      # under the threshold

    # 10 minutes of wall time pass with the feed still dark.
    appclock.CLOCK.set_virtual(base + timedelta(minutes=10))
    ev = src.poll("2026-08-05T04:00:00+00:00")
    # Every known machine dark at once = data-path fault: logged loudly, NOT 2 incidents
    # that fleet-classify would bury as a power failure.
    assert ev == []
    assert src._now_live() - src._server_now >= timedelta(minutes=10), \
        "the clock must keep advancing while the feed is dark"


def test_single_dead_esp32_still_raises_offline_while_others_report(cfg):
    """One ESP32 serves two looms. Its pair going dark must page someone, even though
    the rest of the shed is fine."""
    p1 = [_arow(m, s, 300.0) for m in (31, 33, 35) for s in range(20)]
    p2 = [_arow(35, s, 300.0) for s in range(20, 400)]     # 31 and 33 share a dead node
    pages, state = _aserve(sorted(p1 + p2, key=lambda r: r["ts"]))
    src = _asource(cfg, pages)
    src._cursor = p1[0]["ts"]

    state["now"] = sorted(p1, key=lambda r: r["ts"])[-1]["ts"]
    assert src.poll("2026-08-05T04:00:00+00:00") == []

    state["now"] = p2[-1]["ts"]
    ev = src.poll("2026-08-05T04:00:00+00:00")
    assert sorted((e.asset_ref, e.condition) for e in ev) == [
        ("loom_31", "OFFLINE"), ("loom_33", "OFFLINE")]


def test_loom_already_stopped_at_startup_opens_an_incident(cfg):
    """A loom that is down when ops-core starts must not be amnestied. Previously seed()
    recorded it as STOPPED, so the first poll saw no transition and no incident ever
    opened — the loom stayed invisible until it ran and stopped again, and every restart
    forgave whatever went down during the outage."""
    rows = [_arow(91, s, 300.0) for s in range(60)]      # 91 running throughout
    rows += [_arow(93, s, 0.0) for s in range(60)]       # 93 stopped throughout
    rows.sort(key=lambda r: r["ts"])
    pages, state = _aserve(rows)
    src = _asource(cfg, pages)

    state["now"] = _arow(0, 29, 0)["ts"]                 # seed sees the first 30s
    src.seed()
    assert src._last.get("loom_91") == "RUNNING"
    assert "loom_93" not in src._last, "a stopped loom must stay unset so the diff fires"

    state["now"] = _arow(0, 59, 0)["ts"]                 # 30s later, both still reporting
    ev = src.poll("2026-08-05T04:00:00+00:00")
    assert [(e.asset_ref, e.condition) for e in ev] == [("loom_93", "STOPPED")]


def test_restart_does_not_reopen_an_incident_that_is_already_open(cfg):
    """Rule 1: an in-flight incident resumes across a restart rather than duplicating."""
    from app.core import incidents
    rows = [_arow(93, s, 0.0) for s in range(120)]
    pages, state = _aserve(rows)

    first = _asource(cfg, pages)
    state["now"] = _arow(0, 29, 0)["ts"]
    first.seed()
    from app.core import poller
    state["now"] = _arow(0, 59, 0)["ts"]
    poller.apply_events(cfg, first.poll("2026-08-05T04:00:00+00:00"))
    before = db.query_one("SELECT COUNT(*) n FROM incidents")["n"]
    assert before == 1

    restarted = _asource(cfg, pages)          # fresh process, same database
    state["now"] = _arow(0, 89, 0)["ts"]
    restarted.seed()
    assert restarted._last.get("loom_93") == "STOPPED", "existing incident forces STOPPED"
    state["now"] = _arow(0, 119, 0)["ts"]
    poller.apply_events(cfg, restarted.poll("2026-08-05T04:00:00+00:00"))
    assert db.query_one("SELECT COUNT(*) n FROM incidents")["n"] == before
