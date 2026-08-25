"""Weaving source adapter: the Shingora Loom Data API.

The loom API does NOT expose a "current status" endpoint. It is a cursor-paged
time-series: GET /data?since=<ts> returns one row per machine per ~0.2s window
(~5 rows/sec/machine), newest timestamp echoed back as `to`. "Stopped" is derived —
a row's `rpm` of 0 means the loom is stopped; rpm > 0 means it is running.

This adapter:
  * authenticates with X-API-Key,
  * streams forward by advancing `since` to the previous response's `to` (paging
    immediately whenever `truncated` is true),
  * reduces each poll to the latest row per machine and diffs that against the
    last-known state, emitting IncidentOpened / IncidentResolved on transitions,
  * maps the loom's `machine` number (e.g. "93") to an asset_ref ("loom_93").

Loom timestamps are the server's local time with no timezone suffix; they are used
only as an opaque, monotonic cursor and for "latest row" comparison — never mixed into
storage. Incident times use ops-core's own UTC clock (Rule 4), so there is no timezone
ambiguity in the database.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import requests

from .. import config, db
from .base import IncidentOpened, IncidentResolved

log = logging.getLogger("ops.source.loom")


def _num(value) -> float | None:
    """Loom numerics may arrive as int, float, or numeric string. None if not numeric.

    The API now sends JSON numbers that may be FLOATS: weft/warp/ExtData are logically
    0/1 but arrive as 0.0/1.0. str(0.0) != str(0), so every comparison must be numeric.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value) -> datetime | None:
    """Loom timestamps are naive server-local ISO strings. Parsed ONLY to measure an
    interval against another loom timestamp — never compared to ops-core's UTC clock."""
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


class WeavingLoomApiSource:
    def __init__(self, cfg: "config.Config"):
        self.cfg = cfg
        self.department = cfg.department
        s = cfg.source.get("settings", {}) or {}
        # env override wins so the same config points at the real API or the mock
        self.base_url = (os.environ.get("LOOM_API_BASE_URL") or s.get("base_url", "")).rstrip("/")
        self.api_key = cfg.source_api_key()
        self.poll_seconds = int(s.get("poll_seconds", 30))
        self.data_path = s.get("data_path", "/data")
        self.health_path = s.get("health_path", "/health")
        self.machine_field = s.get("machine_field", "machine")
        self.ts_field = s.get("ts_field", "ts")
        self.asset_ref_prefix = s.get("asset_ref_prefix", "loom_")
        self.stop_when = s.get("stop_when", {"field": "rpm", "lt": 40})
        self.resolve_when = s.get("resolve_when", {"field": "rpm", "gte": 40})
        # A weft/warp break stops production even while the machine still turns; the
        # loom dashboard treats a low thread signal as rpm 0. Same rule here.
        self.thread_stop = s.get("thread_stop", {}) or {}
        self.thread_fields = list(self.thread_stop.get("fields", []) or [])
        self.thread_low = self.thread_stop.get("low_value", 0)
        self.limit = s.get("limit")
        self.max_pages = int(s.get("max_pages", 50))
        # A machine that stops appearing in the feed is a third state: the edge device
        # or the network is down, not the loom. Silence must not read as "running".
        self.offline_after_seconds = int(s.get("offline_after_seconds", 180) or 0)
        self.expected_count = int(s.get("expected_count", 0) or 0)
        self.open_stopped_at_start = bool(s.get("open_incidents_for_stopped_at_start", True))
        assets = cfg.source.get("assets", {}) or {}
        self.exclude = set(assets.get("exclude", []) or [])
        self.discover = bool(assets.get("discover", True))
        self._cursor: str | None = None
        self._last: dict[str, str] = {}       # asset_ref -> STOPPED | RUNNING | OFFLINE
        self._last_seen: dict[str, str] = {}  # asset_ref -> newest server ts observed
        self._server_now: datetime | None = None   # newest ts the feed has ever produced
        self._sync_wall: datetime | None = None    # ops-core UTC when we saw it

    # --- helpers ---------------------------------------------------------------
    def _ref(self, machine: str) -> str:
        return f"{self.asset_ref_prefix}{machine}"

    def _excluded(self, machine: str) -> bool:
        return machine in self.exclude or self._ref(machine) in self.exclude

    @staticmethod
    def _match(row: dict, cond: dict) -> bool:
        field = cond.get("field")
        val = row.get(field)
        if val is None:
            return False
        for op in ("equals", "ne"):
            if op in cond:
                a, b = _num(val), _num(cond[op])
                eq = (a == b) if (a is not None and b is not None) \
                    else (str(val) == str(cond[op]))
                return eq if op == "equals" else not eq
        try:
            num = float(val)
        except (TypeError, ValueError):
            return False
        if "lt" in cond:
            return num < float(cond["lt"])
        if "lte" in cond:
            return num <= float(cond["lte"])
        if "gt" in cond:
            return num > float(cond["gt"])
        if "gte" in cond:
            return num >= float(cond["gte"])
        return False

    def _thread_broken(self, row: dict) -> bool:
        """weft/warp low => production has stopped even if the machine is still turning.

        Polarity is config, not code: `low_value` says which value means 'broken'.
        VERIFY THIS AGAINST LIVE DATA before trusting it (see source.yaml).
        """
        for f in self.thread_fields:
            val = row.get(f)
            if val is None:
                continue
            n, low = _num(val), _num(self.thread_low)
            if n is not None and low is not None:
                if n == low:
                    return True
            elif str(val) == str(self.thread_low):
                return True
        return False

    def _is_stopped(self, row: dict) -> bool:
        return self._thread_broken(row) or self._match(row, self.stop_when)

    def _is_running(self, row: dict) -> bool:
        # Deliberately not the negation of _is_stopped: a thread break outranks rpm,
        # so a loom turning at 300 rpm with weft low is stopped, not running.
        return not self._thread_broken(row) and self._match(row, self.resolve_when)

    # --- transport (overridden by the fixture source for replay) ---------------
    def _fetch_page(self, since: str | None) -> tuple[list[dict], str | None, bool]:
        params: dict = {}
        if since:
            params["since"] = since
        if self.limit:
            params["limit"] = self.limit
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        resp = requests.get(f"{self.base_url}{self.data_path}", params=params,
                            headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("rows", []), data.get("to"), bool(data.get("truncated", False))

    def _collect(self, since: str | None) -> tuple[dict[str, dict], str | None]:
        """Page from `since` to the head, returning the latest row per machine and the
        final `to` cursor.

        If the backlog is still truncated after max_pages we are too far behind to walk
        forward — after an outage that can be millions of rows. ops-core monitors the
        *present*; replaying history would emit hours-old transitions stamped `now`, with
        wrong durations and wrong shift attribution. So we abandon the cursor and resync
        to the head (`since=None` returns the last-minute snapshot). The gap is skipped
        deliberately, and logged.
        """
        latest: dict[str, dict] = {}
        cursor = since
        final_to = since
        truncated = False
        for _ in range(self.max_pages):
            rows, to, truncated = self._fetch_page(cursor)
            for row in rows:
                machine = str(row.get(self.machine_field))
                if self._excluded(machine):
                    continue
                ts = row.get(self.ts_field, "")
                prev = latest.get(machine)
                if prev is None or ts >= prev.get(self.ts_field, ""):
                    latest[machine] = row
            if to:
                final_to = to
                cursor = to
            if not truncated:
                break

        if truncated and since is not None:
            log.warning(
                "loom API backlog still truncated after %d pages (cursor %s) — "
                "resyncing to head; the intervening history is skipped by design",
                self.max_pages, since,
            )
            return self._collect(None)

        return latest, final_to

    # --- source protocol -------------------------------------------------------
    def seed(self) -> None:
        """Establish the cursor and the current per-machine state.

        A loom that is ALREADY STOPPED when ops-core starts is the whole point of the
        system in miniature: it is down and nobody has been told. Marking it STOPPED here
        would make the first poll see no transition, so no incident would ever open — the
        loom would stay invisible until it ran and stopped again. Worse, every service
        restart would amnesty whatever went down during the outage, which is exactly the
        window where catching up matters most.

        So a stopped loom with no open incident is left UNSET, and the first poll's
        ordinary diff opens the incident. Machines that already have an open/resolving
        incident are forced to STOPPED, so a restart resumes them rather than duplicating
        (Rule 1). ops-core cannot know how long a loom was down before it started
        watching, so the incident is stamped at first poll — durations measure observed
        downtime, not total downtime.
        """
        stopped_at_seed: list[str] = []
        try:
            latest, to = self._collect(None)  # no `since` => server defaults to ~last minute
            for machine, row in latest.items():
                ref = self._ref(machine)
                if self._is_stopped(row):
                    stopped_at_seed.append(ref)
                    if not self.open_stopped_at_start:
                        self._last[ref] = "STOPPED"   # legacy behaviour: stay quiet
                else:
                    self._last[ref] = "RUNNING"
                ts = row.get(self.ts_field, "")
                if ts:
                    self._last_seen[ref] = ts
                    self._note_server_ts(ts)
            if self.expected_count and len(latest) < self.expected_count:
                log.warning(
                    "loom API reported %d machines at seed, expected %d — the missing "
                    "ones are invisible to monitoring until they transmit",
                    len(latest), self.expected_count,
                )
            if to:
                self._cursor = to
        except Exception:
            log.exception("loom API seed failed (worker will keep polling)")
        # Anything with an incident already open is resumed, not re-opened (Rule 1).
        already_open = set()
        for r in db.query(
            "SELECT a.asset_ref FROM incidents i JOIN assets a ON a.id=i.asset_id"
            " WHERE i.department=? AND i.status IN ('open','resolving')",
            (self.department,),
        ):
            self._last[r["asset_ref"]] = "STOPPED"
            already_open.add(r["asset_ref"])

        fresh = [r for r in stopped_at_seed if r not in already_open]
        if fresh and self.open_stopped_at_start:
            log.warning(
                "%d loom(s) already stopped at start with no open incident (%s) — "
                "incidents will open on the first poll. Duration is measured from now; "
                "ops-core cannot know how long they were down before it started.",
                len(fresh), ", ".join(sorted(fresh)),
            )
        elif fresh:
            log.warning(
                "%d loom(s) already stopped at start (%s) — NOT opening incidents "
                "(open_incidents_for_stopped_at_start is false in source.yaml). They "
                "stay invisible until they run and stop again.",
                len(fresh), ", ".join(sorted(fresh)),
            )

    def discover_assets(self) -> list[dict]:
        if not self.discover:
            return []
        latest, _ = self._collect(None)
        out = []
        for machine in latest:
            out.append({"asset_ref": self._ref(machine), "label": f"Loom {machine}"})
        return out

    def poll(self, now_iso: str | None = None) -> list:
        from .. import clock
        now_iso = now_iso or clock.now_iso()
        latest, to = self._collect(self._cursor)
        if to:
            self._cursor = to

        events: list = []
        # `since` is INCLUSIVE, so a stalled feed keeps replaying its final boundary row.
        # Such a row is a repeat, not fresh evidence: if it is older than the offline
        # threshold it must not drive state, or an OFFLINE loom is resurrected as RUNNING
        # on every poll. Anchor on the clock as it stood BEFORE this page was folded in.
        now_live = self._now_live()

        for machine, row in latest.items():
            ref = self._ref(machine)
            prev = self._last.get(ref)
            ts = row.get(self.ts_field, "")
            if ts:
                if ts >= self._last_seen.get(ref, ""):
                    self._last_seen[ref] = ts
                self._note_server_ts(ts)

            if self.offline_after_seconds and now_live is not None and ts:
                sampled = _parse_ts(ts)
                if sampled is not None and \
                        (now_live - sampled).total_seconds() >= self.offline_after_seconds:
                    continue   # stale repeat: let _offline_events own this asset

            if self._is_stopped(row):
                condition = "THREAD_STOP" if self._thread_broken(row) else "STOPPED"
                # OFFLINE already has an incident open for this asset; don't double-open.
                if prev not in ("STOPPED", "OFFLINE"):
                    events.append(IncidentOpened(asset_ref=ref, condition=condition,
                                                 at=now_iso, context={"row": row}))
                self._last[ref] = "STOPPED"
            elif self._is_running(row):
                if prev in ("STOPPED", "OFFLINE"):
                    events.append(IncidentResolved(asset_ref=ref, at=now_iso))
                self._last[ref] = "RUNNING"

        events.extend(self._offline_events(latest, now_iso))
        return events

    def _now_live(self) -> datetime | None:
        """The server's clock, advanced by wall time elapsed since the last row arrived.

        Lifted from the loom dashboard's `nowLive()`, and for its stated reason: anchor
        offline aging on the newest ts the feed has produced, then add locally-measured
        *elapsed* time. If instead you anchor on the newest ts in the current poll, a
        fleet that goes completely silent freezes the clock and no loom ever ages into
        OFFLINE — the failure goes undetected precisely when it is total. Only elapsed
        deltas are added to a server-supplied timestamp, so server/ops-core clock skew
        and the loom API's naive local timestamps never enter the arithmetic.
        """
        if self._server_now is None:
            return None
        from .. import clock
        elapsed = max(0.0, (clock.now() - self._sync_wall).total_seconds())
        return self._server_now + timedelta(seconds=elapsed)

    def _note_server_ts(self, ts: str) -> None:
        parsed = _parse_ts(ts)
        if parsed is None:
            return
        if self._server_now is None or parsed > self._server_now:
            from .. import clock
            self._server_now = parsed
            self._sync_wall = clock.now()

    def _offline_events(self, latest: dict, now_iso: str) -> list:
        """A machine that has stopped appearing in the feed is OFFLINE — the edge device
        or the network is down. Without this the loom silently drops out of monitoring:
        no rows means no transition means no incident, and the fleet looks healthy while
        nobody can see it. (One ESP32 serves two looms, so a dead node takes out a pair.)
        """
        if not self.offline_after_seconds:
            return []
        now_live = self._now_live()
        if now_live is None:
            return []   # nothing has ever been seen; nothing to age

        stale: list[str] = []
        for ref, seen_ts in self._last_seen.items():
            # NOTE: presence in `latest` is NOT proof of freshness. `since` is INCLUSIVE,
            # so a dead feed keeps replaying its final boundary row and every machine
            # appears in every page forever. Age on the timestamp, never on presence.
            seen = _parse_ts(seen_ts)
            if seen is None:
                continue
            if (now_live - seen).total_seconds() >= self.offline_after_seconds:
                stale.append(ref)

        if not stale:
            return []

        # Every known machine dark at once is a data-path fault, not 44 shed faults.
        # Opening 44 incidents would trip the fleet-stop auto_classify and be filed as a
        # power failure, paging nobody — the worst possible outcome. Log it loudly
        # instead and let the API watchdog own the alarm.
        if len(stale) >= len(self._last_seen):
            log.error(
                "loom API feed is dark: all %d known machines have produced no rows for "
                "%ds. This is a data-path fault (edge server or the API), not a shed "
                "fault — no incidents raised.",
                len(stale), self.offline_after_seconds,
            )
            # Suppress the *incidents*, but never keep claiming the looms are RUNNING:
            # the state must not lie while the feed is dark.
            for ref in stale:
                self._last[ref] = "OFFLINE"
            return []

        events: list = []
        for ref in stale:
            if self._last.get(ref) != "OFFLINE":
                age = int((now_live - _parse_ts(self._last_seen[ref])).total_seconds())
                log.warning("loom %s offline: no data for %ds", ref, age)
                events.append(IncidentOpened(asset_ref=ref, condition="OFFLINE",
                                             at=now_iso,
                                             context={"last_seen": self._last_seen[ref]}))
            self._last[ref] = "OFFLINE"
        return events
