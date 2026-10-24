"""Sitting / standing time tracking, stand reminders and posture history.

Pieces (all JSON-friendly, numpy free):

* :class:`StreakMachine` - sitting streaks from posture intervals. A streak is
  continuous sitting that tolerates unknown / no-data gaps shorter than
  ``unknown_grace_s`` and short upright interruptions; it ends after
  ``stand_break_s`` of continuous upright time (a stand break), after
  ``unknown_grace_s`` of unknown, or after more than ``away_end_streak_s``
  away (empty room).
* :func:`summarize_days` - per local calendar day: seconds per state, 24
  hourly bins, upright total, stand hours, sit-to-stand transitions, sitting
  bouts (= streaks) and reminder responses, computed from a list of bouts.
  The in-memory tracker and the SQLite history use the same function.
* :class:`SittingTracker` - pure logic driven by
  :class:`backend.posture.PostureReading` objects with data timestamps:
  bouts (state, start, end), the current streak, stand reminders (fire when a
  streak reaches ``reminder_after_min``, clear when a stand break completes,
  snooze for N minutes).
* :class:`PostureStore` - stdlib sqlite3 history (``data/posture.sqlite3``).
* :class:`PostureService` - wires an :class:`backend.engine.Engine` frame hook
  to the classifier, tracker and store and serves the dashboard API.

Upright = standing + moving: as with Apple Watch stand hours, moving counts as
standing. Only live, non-synthetic sources are persisted; replay and
simulation data are tracked in memory and every response says so. All of this
is a research prototype: postures are estimated from Wi-Fi signals and may be
wrong; nothing here is a medical measurement.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
import statistics
import threading
import time
from collections import deque
from dataclasses import replace
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from . import __version__
from .config import PostureConfig
from .posture import CLASS_STATES, POSTURE_STATES, REASON_NOT_CALIBRATED, PostureClassifier, PostureReading

log = logging.getLogger(__name__)

#: States that count as upright time (standing + moving).
UPRIGHT_STATES = ("standing", "moving")
#: Readings further apart than this are a data gap (no bout, unknown for streaks).
MAX_FRAME_GAP_S = 2.0
#: The open bout is written to the store at most this often.
CHECKPOINT_S = 30.0
#: A moving bout at least this long after sitting counts as getting up.
MOVING_TRANSITION_S = 5.0
#: Bouts before a day start used to continue streaks across midnight.
LOOKBACK_S = 6 * 3600.0
#: /api/posture reports 'unknown' when the newest reading is older than this (wall time).
STALE_S = 5.0
MAX_MEMORY_BOUTS = 50_000
DISCLAIMER = ("Research prototype, not a medical device; sitting/standing are estimated from Wi-Fi signals "
              "and may be wrong")
UPRIGHT_NOTE = "Upright = standing + moving (as with Apple Watch stand hours, moving counts as standing)."
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNSET = object()


# ------------------------------------------------------------------ dates
def local_date(ts: float) -> str:
    """Local calendar date ``YYYY-MM-DD`` of a unix timestamp."""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def parse_date(text) -> Date:
    """Strict ``YYYY-MM-DD``; raises ValueError."""
    if not isinstance(text, str) or not DATE_RE.match(text):
        raise ValueError("date must be YYYY-MM-DD")
    return datetime.strptime(text, "%Y-%m-%d").date()


def day_bounds(day: Date) -> tuple:
    """Unix timestamps of local midnight at the start and end of ``day``."""
    nxt = day + timedelta(days=1)
    return (time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1)),
            time.mktime((nxt.year, nxt.month, nxt.day, 0, 0, 0, 0, 0, -1)))


def _hour_start(ts: float) -> float:
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, 0, 0, 0, 0, lt.tm_isdst))


def _split_hours(t0: float, t1: float):
    """Yield (date, hour, seconds) pieces of [t0, t1) split at local hour boundaries."""
    t = t0
    while t < t1:
        nxt = min(t1, _hour_start(t) + 3600.0)
        if nxt <= t:
            nxt = t1
        yield local_date(t), time.localtime(t).tm_hour, nxt - t
        t = nxt


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------- bouts
@dataclass
class Bout:
    """A period in one posture state (data timestamps)."""

    state: str
    start_ts: float
    end_ts: float
    source_kind: Optional[str] = None
    calibration_key: Optional[str] = None
    row_id: Optional[int] = None

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)

    def to_dict(self) -> dict:
        return {"state": self.state, "start_ts": self.start_ts, "end_ts": self.end_ts,
                "duration_s": round(self.duration_s, 1), "start": iso(self.start_ts), "end": iso(self.end_ts),
                "source_kind": self.source_kind, "calibration_key": self.calibration_key}


def _kind(state: str) -> str:
    if state == "sitting":
        return "sit"
    if state in UPRIGHT_STATES:
        return "upright"
    if state == "away":
        return "away"
    return "unknown"


class StreakMachine:
    """Sitting streaks from consecutive posture intervals (see module doc)."""

    ENDED_BY = {"upright": "stand_break", "away": "away", "unknown": "unknown"}

    def __init__(self, config: PostureConfig) -> None:
        self.config = config
        self.start: Optional[float] = None
        self.last_sit: Optional[float] = None
        self._run_kind: Optional[str] = None
        self._run_start: Optional[float] = None

    def add(self, state: str, t0: float, t1: float) -> Optional[dict]:
        """Feed the interval [t0, t1) spent in ``state`` (``unknown`` for data
        gaps). Returns the finished streak when this interval ended it."""
        if t1 < t0:
            return None
        kind = _kind(state)
        if kind == "sit":
            if self.start is None:
                self.start = t0
            self.last_sit = t1
            self._run_kind = self._run_start = None
            return None
        if self.start is None:
            return None
        if self._run_kind != kind:
            self._run_kind, self._run_start = kind, t0
        cfg = self.config
        run = t1 - self._run_start
        if kind == "upright":
            limit, ended = cfg.stand_break_s, run >= cfg.stand_break_s
        elif kind == "away":
            limit, ended = cfg.away_end_streak_s, run > cfg.away_end_streak_s
        else:
            limit, ended = cfg.unknown_grace_s, run >= cfg.unknown_grace_s
        if not ended:
            return None
        ep = {"start_ts": self.start, "end_ts": self.last_sit, "duration_s": self.last_sit - self.start,
              "ended_by": self.ENDED_BY[kind], "break_start_ts": self._run_start,
              "ended_at_ts": self._run_start + limit}
        self.start = self.last_sit = self._run_kind = self._run_start = None
        return ep

    def current(self) -> Optional[dict]:
        """The streak in progress (``seconds`` = first to latest sitting time)."""
        if self.start is None:
            return None
        return {"start_ts": self.start, "last_sitting_ts": self.last_sit, "seconds": self.last_sit - self.start}


def _empty_hours() -> dict:
    return {s: [0.0] * 24 for s in POSTURE_STATES}


def summarize_days(bouts: Iterable[Bout], dates: Iterable[str], config: PostureConfig,
                   reminders: Iterable[dict] = ()) -> dict:
    """``{date: summary}`` for the requested local dates from ``bouts`` (and
    reminder records ``{ts, kind, streak_s, responded_after_s}``).

    Sitting bouts are streaks (see :class:`StreakMachine`) counted on the day
    they start; a sit-to-stand transition is a sitting bout followed (ignoring
    unknown and moving bouts shorter than :data:`MOVING_TRANSITION_S`) by
    standing, moving or away.
    """
    dates = list(dates)
    hours = {d: _empty_hours() for d in dates}
    trans = {d: 0 for d in dates}
    eps = {d: [] for d in dates}
    rems = {d: [] for d in dates}
    ordered = sorted(bouts, key=lambda b: (b.start_ts, b.end_ts))
    for b in ordered:
        if b.end_ts > b.start_ts and b.state in POSTURE_STATES:
            for d, h, sec in _split_hours(b.start_ts, b.end_ts):
                if d in hours:
                    hours[d][b.state][h] += sec
    settled = None
    for b in ordered:
        if b.state == "sitting":
            settled = "sitting"
        elif b.state in ("standing", "away") or (b.state == "moving" and b.duration_s >= MOVING_TRANSITION_S):
            if settled == "sitting":
                d = local_date(b.start_ts)
                if d in trans:
                    trans[d] += 1
            settled = b.state
    machine = StreakMachine(config)
    found = []
    prev_end = None
    for b in ordered:
        if prev_end is not None and b.start_ts - prev_end > MAX_FRAME_GAP_S:
            ep = machine.add("unknown", prev_end, b.start_ts)
            if ep:
                found.append(ep)
        ep = machine.add(b.state, b.start_ts, b.end_ts)
        if ep:
            found.append(ep)
        prev_end = b.end_ts if prev_end is None else max(prev_end, b.end_ts)
    cur = machine.current()
    if cur:
        found.append({"start_ts": cur["start_ts"], "end_ts": cur["last_sitting_ts"], "duration_s": cur["seconds"],
                      "ended_by": None})
    for ep in found:
        d = local_date(ep["start_ts"])
        if d in eps:
            eps[d].append(ep["duration_s"])
    for r in reminders:
        if r.get("kind") == "reminder":
            d = local_date(r["ts"])
            if d in rems:
                rems[d].append(r)
    out = {}
    for d in dates:
        h = hours[d]
        upright = [a + b for a, b in zip(h["standing"], h["moving"])]
        flags = [u >= config.stand_hour_min_s for u in upright]
        totals = {s: round(sum(h[s]), 1) for s in POSTURE_STATES}
        sits = eps[d]
        responded = [r["responded_after_s"] for r in rems[d] if r.get("responded_after_s") is not None]
        out[d] = {
            "date": d,
            "totals_s": totals,
            "upright_s": round(sum(upright), 1),
            "tracked_s": round(sum(totals.values()), 1),
            "hourly_min": {**{s: [round(v / 60.0, 2) for v in h[s]] for s in POSTURE_STATES},
                           "upright": [round(v / 60.0, 2) for v in upright]},
            "stand_hour_flags": flags,
            "stand_hours": sum(flags),
            "stand_goal_hours": int(config.stand_goal_hours),
            "transitions": trans[d],
            "sitting_bouts": len(sits),
            "longest_sitting_s": round(max(sits), 1) if sits else 0.0,
            "average_sitting_s": round(sum(sits) / len(sits), 1) if sits else 0.0,
            "reminders": {"count": len(rems[d]), "responded": len(responded),
                          "median_response_s": round(statistics.median(responded), 1) if responded else None},
        }
    return out


# ----------------------------------------------------------------- tracker
class SittingTracker:
    """Bouts, sitting streak and stand reminders from posture readings.

    Pure logic: every time comes from the readings (data timestamps); the
    injectable ``clock`` is only used before the first reading. ``store``
    (optional, a :class:`PostureStore`) receives closed bouts, open-bout
    checkpoints (at most every :data:`CHECKPOINT_S`) and reminder records.
    """

    def __init__(self, config: PostureConfig, clock=time.time, store: Optional["PostureStore"] = None,
                 source_kind: Optional[str] = None, calibration_key: Optional[str] = None) -> None:
        self.config = config
        self.clock = clock
        self.store = store
        self.source_kind = source_kind
        self.calibration_key = calibration_key
        self.bouts: deque = deque(maxlen=MAX_MEMORY_BOUTS)
        self.open: Optional[Bout] = None
        self.reminders: list = []
        self.streaks = StreakMachine(config)
        self.last_ts: Optional[float] = None
        self.last_state: Optional[str] = None
        self.active_reminder: Optional[dict] = None
        self.snoozed_until: Optional[float] = None
        self.last_streak: Optional[dict] = None
        self._last_checkpoint: Optional[float] = None

    # --------------------------------------------------------------- feed
    def update(self, reading: PostureReading) -> list:
        """Consume one reading; returns events (``reminder`` / ``streak_end``)."""
        ts, state = float(reading.ts), reading.state
        events: list = []
        if self.last_ts is None:
            self._open_bout(state, ts)
        else:
            prev_ts, prev_state = self.last_ts, self.last_state
            dt = ts - prev_ts
            if dt <= 0:
                return events            # out-of-order or duplicate timestamp
            if dt > MAX_FRAME_GAP_S:
                self._close_open()
                self._streak(self.streaks.add("unknown", prev_ts, ts), events)
                self._open_bout(state, ts)
            else:
                self.open.end_ts = ts
                self._streak(self.streaks.add(prev_state, prev_ts, ts), events)
                if state != prev_state:
                    self._close_open()
                    self._open_bout(state, ts)
        self.last_ts, self.last_state = ts, state
        self._check_reminder(ts, events)
        if self.store is not None and self.open is not None and (
                self._last_checkpoint is None or ts - self._last_checkpoint >= CHECKPOINT_S):
            self._save_bout(self.open)
            self._last_checkpoint = ts
        return events

    def _open_bout(self, state: str, ts: float) -> None:
        self.open = Bout(state, ts, ts, self.source_kind, self.calibration_key)

    def _close_open(self) -> None:
        b = self.open
        if b is None:
            return
        self.open = None
        self.bouts.append(b)
        if self.store is not None:
            self._save_bout(b)

    def _save_bout(self, b: Bout) -> None:
        try:
            self.store.save_bout(b)
        except sqlite3.Error:
            log.exception("cannot save a posture bout")

    def _save_reminder(self, r: dict) -> None:
        if self.store is None:
            return
        try:
            self.store.save_reminder(r)
        except sqlite3.Error:
            log.exception("cannot save a reminder record")

    def _streak(self, ep: Optional[dict], events: list) -> None:
        if ep is None:
            return
        self.last_streak = ep
        events.append({"kind": "streak_end", **ep})
        rem = self.active_reminder
        if rem is not None:
            if ep["ended_by"] in ("stand_break", "away"):
                rem["responded_after_s"] = round(max(0.0, ep["break_start_ts"] - rem["ts"]), 1)
            rem["cleared_by"] = ep["ended_by"]
            self.active_reminder = None
            self._save_reminder(rem)
            events.append({"kind": "reminder_cleared", **rem})

    def _check_reminder(self, ts: float, events: list) -> None:
        cur = self.streaks.current()
        if cur is None or self.active_reminder is not None:
            return
        if self.snoozed_until is not None and ts < self.snoozed_until:
            return
        if cur["seconds"] >= self.config.reminder_after_min * 60.0:
            rem = {"id": None, "ts": ts, "kind": "reminder", "streak_s": round(cur["seconds"], 1),
                   "responded_after_s": None}
            self.reminders.append(rem)
            self.active_reminder = rem
            self._save_reminder(rem)
            events.append({"kind": "reminder", **rem})

    def flush(self) -> None:
        """Write the open bout to the store (on source change / shutdown)."""
        if self.store is not None and self.open is not None:
            self._save_bout(self.open)
            self._last_checkpoint = self.last_ts

    # ---------------------------------------------------------------- api
    def now(self) -> float:
        return self.last_ts if self.last_ts is not None else self.clock()

    def snooze(self, minutes: float) -> dict:
        now = self.now()
        self.snoozed_until = now + float(minutes) * 60.0
        cur = self.streaks.current()
        rem = {"id": None, "ts": now, "kind": "snooze", "streak_s": round(cur["seconds"], 1) if cur else 0.0,
               "responded_after_s": None, "minutes": float(minutes)}
        if self.active_reminder is not None:
            self.active_reminder["snoozed"] = True
            self.active_reminder = None
        self.reminders.append(rem)
        self._save_reminder(rem)
        return self.reminder_status()

    def current_streak(self) -> dict:
        cur = self.streaks.current()
        return {"active": cur is not None, "start_ts": cur["start_ts"] if cur else None,
                "seconds": round(cur["seconds"], 1) if cur else 0.0,
                "last": self.last_streak}

    def reminder_status(self) -> dict:
        cfg = self.config
        cur = self.streaks.current()
        streak_s = cur["seconds"] if cur else 0.0
        now = self.now()
        snoozed = self.snoozed_until is not None and now < self.snoozed_until
        rem = self.active_reminder
        return {
            "active": rem is not None,
            "since_ts": rem["ts"] if rem else None,
            "message": (f"You have been sitting for {int(streak_s // 60)} min. Time to stand up and move for "
                        f"{cfg.stand_break_s / 60:g} min." if rem else None),
            "reminder_after_min": cfg.reminder_after_min,
            "due_in_s": 0.0 if rem else round(max(0.0, cfg.reminder_after_min * 60.0 - streak_s), 1),
            "snoozed": snoozed,
            "snoozed_until": self.snoozed_until if snoozed else None,
            "snooze_remaining_s": round(self.snoozed_until - now, 1) if snoozed else 0.0,
            "stand_break_s": cfg.stand_break_s,
        }

    def all_bouts(self) -> list:
        out = list(self.bouts)
        if self.open is not None:
            out.append(replace(self.open))
        return out

    def summary(self, day: str) -> dict:
        return summarize_days(self.all_bouts(), [day], self.config, self.reminders)[day]


# ------------------------------------------------------------------- store
SCHEMA = """
CREATE TABLE IF NOT EXISTS bouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    source_kind TEXT,
    calibration_key TEXT
);
CREATE INDEX IF NOT EXISTS bouts_start ON bouts(start_ts);
CREATE INDEX IF NOT EXISTS bouts_end ON bouts(end_ts);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    streak_s REAL,
    responded_after_s REAL
);
CREATE INDEX IF NOT EXISTS reminders_ts ON reminders(ts);
"""


class PostureStore:
    """Posture history in SQLite (stdlib sqlite3, WAL journal, one connection
    guarded by a lock so the engine thread and request threads can share it)."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def save_bout(self, b: Bout) -> None:
        with self._lock, self._conn:
            if b.row_id is None:
                cur = self._conn.execute(
                    "INSERT INTO bouts (state, start_ts, end_ts, source_kind, calibration_key) VALUES (?, ?, ?, ?, ?)",
                    (b.state, b.start_ts, b.end_ts, b.source_kind, b.calibration_key))
                b.row_id = cur.lastrowid
            else:
                self._conn.execute("UPDATE bouts SET end_ts = ?, calibration_key = ? WHERE id = ?",
                                   (b.end_ts, b.calibration_key, b.row_id))

    def save_reminder(self, r: dict) -> None:
        with self._lock, self._conn:
            if r.get("id") is None:
                cur = self._conn.execute(
                    "INSERT INTO reminders (ts, kind, streak_s, responded_after_s) VALUES (?, ?, ?, ?)",
                    (r["ts"], r["kind"], r.get("streak_s"), r.get("responded_after_s")))
                r["id"] = cur.lastrowid
            else:
                self._conn.execute("UPDATE reminders SET responded_after_s = ? WHERE id = ?",
                                   (r.get("responded_after_s"), r["id"]))

    def bouts(self, t0: float, t1: float) -> list:
        """Bouts overlapping [t0, t1), oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, state, start_ts, end_ts, source_kind, calibration_key FROM bouts "
                "WHERE end_ts >= ? AND start_ts < ? ORDER BY start_ts, id", (t0, t1)).fetchall()
        return [Bout(state=r[1], start_ts=r[2], end_ts=r[3], source_kind=r[4], calibration_key=r[5], row_id=r[0])
                for r in rows]

    def reminders(self, t0: float, t1: float) -> list:
        with self._lock:
            rows = self._conn.execute("SELECT id, ts, kind, streak_s, responded_after_s FROM reminders "
                                      "WHERE ts >= ? AND ts < ? ORDER BY ts, id", (t0, t1)).fetchall()
        return [{"id": r[0], "ts": r[1], "kind": r[2], "streak_s": r[3], "responded_after_s": r[4]} for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ----------------------------------------------------------------- service
class PostureService:
    """Live posture tracking for an Engine (frame hook + status provider).

    Picks up the posture model applied by the calibration manager
    (``engine.posture_model``) on every frame, resets its in-memory state when
    the engine source changes (``engine.started_at``) and persists history to
    ``store`` only for live, non-synthetic sources.
    """

    def __init__(self, engine, store: Optional[PostureStore] = None, clock=time.time) -> None:
        self.engine = engine
        self.store = store
        self.clock = clock
        self.config: PostureConfig = engine.config.posture
        self.classifier = PostureClassifier(self.config)
        self.tracker = SittingTracker(self.config, clock=clock)
        self._lock = threading.RLock()
        self._started_at = _UNSET
        self._kind: Optional[str] = None
        self._synthetic = False
        self._persist = False
        self._last_wall: Optional[float] = None
        engine.add_frame_hook(self._on_frame)
        engine.add_status_provider("posture", self.status_summary)

    # ----------------------------------------------------- engine thread
    def _on_frame(self, frame, activity) -> None:
        eng = self.engine
        proc = eng.processor
        profile, layout = proc.last_smoothed, proc.layout
        model = eng.posture_model
        threshold = float(eng.config.thresholds.movement)
        info = eng.calibration_info
        with self._lock:
            self._sync()
            reading = self.classifier.update(frame, profile, layout, model, threshold)
            self.tracker.calibration_key = info.get("key") if info else None
            self.tracker.update(reading)
            self._last_wall = self.clock()

    def _sync(self) -> None:
        eng = self.engine
        started = eng.started_at
        if started == self._started_at:
            return
        self.tracker.flush()
        self._started_at = started
        self._kind = eng.source_kind
        self._synthetic = bool(eng.synthetic)
        self._persist = self.store is not None and self._kind == "live" and not self._synthetic
        self.classifier.reset()
        self.tracker = SittingTracker(self.config, clock=self.clock,
                                      store=self.store if self._persist else None, source_kind=self._kind)
        self._last_wall = None

    def close(self) -> None:
        with self._lock:
            self.tracker.flush()
            if self.store is not None:
                self.store.close()
                self.store = None

    # ------------------------------------------------------------ helpers
    def _meta(self) -> dict:
        return {"source_kind": self._kind, "synthetic": self._synthetic, "persisting": self._persist,
                "data_source": "store" if self._uses_store() else "memory"}

    def _uses_store(self) -> bool:
        return self.store is not None and (self._persist or self._kind is None)

    def _bouts(self, t0: float, t1: float) -> list:
        if self._uses_store():
            rows = self.store.bouts(t0, t1)
            if self._persist and self.tracker.open is not None:
                op = self.tracker.open
                rows = [b for b in rows if b.row_id is None or b.row_id != op.row_id]
                if op.end_ts >= t0 and op.start_ts < t1:
                    rows.append(replace(op))
            return rows
        return [b for b in self.tracker.all_bouts() if b.end_ts >= t0 and b.start_ts < t1]

    def _reminders(self, t0: float, t1: float) -> list:
        if self._uses_store():
            return self.store.reminders(t0, t1)
        return [r for r in self.tracker.reminders if t0 <= r["ts"] < t1]

    def default_date(self) -> Date:
        """Date of the newest reading of the current source, else today (local)."""
        ts = self.tracker.last_ts if self._kind is not None and self.tracker.last_ts is not None else self.clock()
        return datetime.fromtimestamp(ts).date()

    def _days(self, first: Date, last: Date) -> dict:
        dates = []
        d = first
        while d <= last:
            dates.append(d.isoformat())
            d += timedelta(days=1)
        t0, _ = day_bounds(first)
        _, t1 = day_bounds(last)
        bouts = self._bouts(t0 - LOOKBACK_S, t1)
        return summarize_days(bouts, dates, self.config, self._reminders(t0, t1)), bouts

    def _current_reading(self) -> dict:
        eng = self.engine
        calibrated = eng.posture_model is not None
        r = self.classifier.last
        st = eng.source_status
        running = bool(eng.running and st is not None and not st.finished)
        age = (self.clock() - self._last_wall) if self._last_wall is not None else None
        fresh = r is not None and running and age is not None and age <= STALE_S
        if fresh:
            out = r.to_dict()
            out["state_duration_s"] = round(r.ts - r.state_since, 1) if r.state_since is not None else 0.0
        else:
            reason = (REASON_NOT_CALIBRATED if not calibrated
                      else "no data source" if eng.source_kind is None else "no recent data")
            out = {"ts": r.ts if r else None, "raw_label": None, "state": "unknown", "probabilities": {},
                   "confidence": None, "reason": reason, "motion_score": None, "state_since": None,
                   "state_duration_s": None}
        if not calibrated and out["state"] in CLASS_STATES.values():
            out.update(state="unknown", reason=REASON_NOT_CALIBRATED, probabilities={}, confidence=None)
        out.update(calibrated=calibrated, running=running, data_age_s=round(age, 2) if age is not None else None)
        return out

    # --------------------------------------------------------------- API
    def status_summary(self) -> dict:
        """Compact state for /api/status."""
        with self._lock:
            self._sync()
            cur = self._current_reading()
            streak = self.tracker.current_streak()
            return {"state": cur["state"], "confidence": cur["confidence"], "reason": cur["reason"],
                    "calibrated": cur["calibrated"], "sitting_streak_s": streak["seconds"],
                    "reminder_active": self.tracker.active_reminder is not None, **self._meta()}

    def current(self) -> dict:
        with self._lock:
            self._sync()
            cur = self._current_reading()
            info = self.engine.calibration_info or {}
            streak = self.tracker.current_streak()
            day = self.default_date()
            summ = self._days(day, day)[0][day.isoformat()]
            today = {k: summ[k] for k in ("date", "totals_s", "upright_s", "tracked_s", "stand_hours",
                                          "stand_goal_hours", "transitions", "sitting_bouts", "longest_sitting_s")}
            return {
                **cur,
                "calibration": {"key": info.get("key"), "name": info.get("name"),
                                "synthetic": bool(info.get("synthetic")) if info else False},
                "streak": streak,
                "sitting_streak_s": streak["seconds"],
                "current_bout": self.tracker.open.to_dict() if self.tracker.open is not None else None,
                "reminder": self.tracker.reminder_status(),
                "today": today,
                **self._meta(),
                "states": list(POSTURE_STATES),
                "upright_states": list(UPRIGHT_STATES),
                "upright_note": UPRIGHT_NOTE,
                "disclaimer": DISCLAIMER,
            }

    def today(self, day: Optional[Date] = None) -> dict:
        with self._lock:
            self._sync()
            day = day or self.default_date()
            summ = self._days(day, day)[0][day.isoformat()]
            return {**summ, **self._meta(), "upright_note": UPRIGHT_NOTE, "disclaimer": DISCLAIMER}

    def history(self, days: int, end: Optional[Date] = None) -> dict:
        with self._lock:
            self._sync()
            end = end or self.default_date()
            first = end - timedelta(days=int(days) - 1)
            summ = self._days(first, end)[0]
            return {"days": int(days), "start_date": first.isoformat(), "end_date": end.isoformat(),
                    "daily": [_daily_row(s) for s in summ.values()],
                    "stand_goal_hours": int(self.config.stand_goal_hours),
                    **self._meta(), "upright_note": UPRIGHT_NOTE, "disclaimer": DISCLAIMER}

    def snooze(self, minutes: float) -> dict:
        with self._lock:
            self._sync()
            return self.tracker.snooze(minutes)

    def export(self, days: int, fmt: str, end: Optional[Date] = None) -> tuple:
        """``(text, mimetype, filename)`` for a provider export (csv | json)."""
        with self._lock:
            self._sync()
            end = end or self.default_date()
            first = end - timedelta(days=int(days) - 1)
            summ, bouts = self._days(first, end)
            t0, _ = day_bounds(first)
            _, t1 = day_bounds(end)
            bouts = [b for b in bouts if b.end_ts >= t0 and b.start_ts < t1]
            reminders = self._reminders(t0, t1)
            meta = self._meta()
            info = self.engine.calibration_info or {}
        if meta["synthetic"]:
            source = "SYNTHETIC simulation data (in memory only, not real measurements)"
        elif meta["data_source"] == "store":
            source = "live sensor history (data/posture.sqlite3)"
        else:
            source = f"{meta['source_kind']} data (in memory only, not persisted)"
        metadata = {
            "title": "Sitting and standing history",
            "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "app_version": __version__,
            "date_range": [first.isoformat(), end.isoformat()],
            "calibration_key": info.get("key"),
            "calibration_keys_in_data": sorted({b.calibration_key for b in bouts if b.calibration_key}),
            "source": source,
            "source_kind": meta["source_kind"],
            "synthetic": meta["synthetic"],
            "upright": UPRIGHT_NOTE,
            "stand_hour_rule": f"an hour with at least {self.config.stand_hour_min_s:g} s upright",
            "disclaimer": DISCLAIMER,
        }
        daily = [_daily_row(s) for s in summ.values()]
        name = f"posture_{first.isoformat()}_{end.isoformat()}{'_SYNTHETIC' if meta['synthetic'] else ''}"
        if fmt == "json":
            body = {"metadata": metadata, "daily": daily, "bouts": [b.to_dict() for b in bouts],
                    "reminders": reminders}
            return json.dumps(body, indent=2) + "\n", "application/json", name + ".json"
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        for key in ("title", "generated", "app_version", "date_range", "calibration_key", "source", "upright",
                    "stand_hour_rule", "disclaimer"):
            v = metadata[key]
            w.writerow([f"# {key}", " to ".join(v) if isinstance(v, list) else ("" if v is None else v)])
        w.writerow([])
        w.writerow(["# daily summary"])
        cols = list(daily[0].keys()) if daily else []
        w.writerow(cols)
        for row in daily:
            w.writerow([row[c] for c in cols])
        w.writerow([])
        w.writerow(["# bouts"])
        w.writerow(["state", "start", "end", "duration_s", "source_kind", "calibration_key"])
        for b in bouts:
            w.writerow([b.state, iso(b.start_ts), iso(b.end_ts), round(b.duration_s, 1), b.source_kind or "",
                        b.calibration_key or ""])
        return buf.getvalue(), "text/csv", name + ".csv"


def _daily_row(s: dict) -> dict:
    t = s["totals_s"]
    return {
        "date": s["date"],
        "sitting_min": round(t["sitting"] / 60.0, 2),
        "standing_min": round(t["standing"] / 60.0, 2),
        "moving_min": round(t["moving"] / 60.0, 2),
        "away_min": round(t["away"] / 60.0, 2),
        "unknown_min": round(t["unknown"] / 60.0, 2),
        "upright_min": round(s["upright_s"] / 60.0, 2),
        "stand_hours": s["stand_hours"],
        "stand_goal_hours": s["stand_goal_hours"],
        "sit_to_stand_transitions": s["transitions"],
        "sitting_bouts": s["sitting_bouts"],
        "longest_sitting_min": round(s["longest_sitting_s"] / 60.0, 2),
        "average_sitting_min": round(s["average_sitting_s"] / 60.0, 2),
        "reminders": s["reminders"]["count"],
        "median_reminder_response_s": s["reminders"]["median_response_s"],
    }
