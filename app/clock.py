"""Time. Two rules from the design doc live here:

  * Rule 4 — UTC in storage, IST at the edges. Everything persisted is UTC ISO-8601
    with a fixed width so string comparison on due_at is correct. Messages and the
    dashboard render IST.
  * Escalation timers only manifest over hours of wall-clock. To replay a recorded
    shift "a day in ten seconds" (section 10 / tests), the whole system reads *now*
    from one place — the CLOCK singleton — which the replay harness can drive
    virtually. Production uses the real wall clock.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - fallback if tzdata is unavailable
    IST = timezone(timedelta(hours=5, minutes=30))

UTC = timezone.utc
_FMT = "%Y-%m-%dT%H:%M:%S+00:00"  # fixed width => lexicographic order == chronological


class Clock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._virtual: datetime | None = None

    def now(self) -> datetime:
        with self._lock:
            if self._virtual is not None:
                return self._virtual
        return datetime.now(UTC)

    # --- replay controls (no-ops in production) ---
    def set_virtual(self, dt: datetime) -> None:
        with self._lock:
            self._virtual = dt.astimezone(UTC)

    def advance(self, seconds: float) -> None:
        with self._lock:
            if self._virtual is None:
                self._virtual = datetime.now(UTC)
            self._virtual = self._virtual + timedelta(seconds=seconds)

    def real(self) -> None:
        with self._lock:
            self._virtual = None

    @property
    def is_virtual(self) -> bool:
        return self._virtual is not None


CLOCK = Clock()


# --- formatting / parsing --------------------------------------------------

def now() -> datetime:
    return CLOCK.now()


def now_iso() -> str:
    return to_iso(CLOCK.now())


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_FMT)


def parse(iso: str) -> datetime:
    """Parse a stored UTC timestamp (tolerant of 'Z' and offset forms)."""
    s = iso.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def plus_seconds(seconds: float, base: datetime | None = None) -> str:
    return to_iso((base or CLOCK.now()) + timedelta(seconds=seconds))


def plus_minutes(minutes: float, base: datetime | None = None) -> str:
    return plus_seconds(minutes * 60, base)


# --- IST edges -------------------------------------------------------------

def to_ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


def format_ist(dt: datetime | str, fmt: str = "%d %b %Y %H:%M") -> str:
    if isinstance(dt, str):
        dt = parse(dt)
    return to_ist(dt).strftime(fmt) + " IST"


# --- shifts ----------------------------------------------------------------

def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def resolve_shift(dt: datetime, shifts: dict) -> str | None:
    """Return the shift letter (A/B/C...) for a UTC instant, using IST wall time.

    `shifts` maps letter -> "HH:MM-HH:MM" (local/IST). Windows may wrap midnight.
    """
    local = to_ist(dt)
    minute_of_day = local.hour * 60 + local.minute
    for letter, span in shifts.items():
        if letter == "timezone" or not isinstance(span, str) or "-" not in span:
            continue
        start_s, end_s = span.split("-")
        sh, sm = _hhmm(start_s)
        eh, em = _hhmm(end_s)
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= end:
            if start <= minute_of_day < end:
                return letter
        else:  # wraps midnight, e.g. 22:00-06:00
            if minute_of_day >= start or minute_of_day < end:
                return letter
    return None


def within_shift_boundary(dt: datetime, shifts: dict, window_minutes: float) -> bool:
    """True if `dt` (IST wall time) falls within `window_minutes` after any shift start.

    Used by the classifier to recognise a fleet-wide stop at a changeover (the
    punch-station queue / shift change), which is a queue, not an escalatable fault.
    """
    local = to_ist(dt)
    minute_of_day = local.hour * 60 + local.minute
    for letter, span in shifts.items():
        if letter == "timezone" or not isinstance(span, str) or "-" not in span:
            continue
        start_s, _ = span.split("-")
        sh, sm = _hhmm(start_s)
        start = sh * 60 + sm
        delta = (minute_of_day - start) % (24 * 60)
        if 0 <= delta < window_minutes:
            return True
    return False
