"""Labeled recording sessions.

Layout on disk (``data/recordings`` by default)::

    data/recordings/<session_id>/session.json
    data/recordings/<session_id>/<take_index>_<label>_<start_ts>.csv

``session_id`` is ``YYYYMMDD-HHMMSS-<placement>``. ``session.json`` holds the
creation time, sensor configuration, source kind, synthetic flag, notes, app
version and a ``takes`` index (label, start, end, packet count, duration). Each
take CSV stores every raw line received while the take was running, with the
columns in :data:`TAKE_FIELDS`, so a replay goes through exactly the same
parser and pipeline as live data.

Sessions are the unit for machine-learning evaluation: hold out complete
sessions, never random packets or windows from the same session.

Writes happen in the engine thread (the engine calls the recorder for every
raw line), so nothing depends on the dashboard refresh rate.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import __version__
from .config import DATA_DIR
from .csi_parser import parse_any

log = logging.getLogger(__name__)

DEFAULT_RECORDINGS_DIR = DATA_DIR / "recordings"
SESSION_FILE = "session.json"
SESSION_FORMAT = 1

#: Label keys and display names (exact, in UI order).
LABELS = {
    "empty_room": "Empty room",
    "standing_still": "Standing still",
    "walking": "Walking",
    "sitting": "Sitting",
    "lying_down": "Lying down",
    "rapid_sitting": "Rapid sitting",
    "simulated_fall": "Simulated fall",
}

#: Columns of a take CSV (raw line plus parsed metadata).
TAKE_FIELDS = ["host_ts_iso", "host_ts_unix", "session_id", "take_id", "label", "raw_line",
               "rssi", "id", "channel", "len", "local_timestamp"]

PLACEMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")
SESSION_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9][a-z0-9_-]{0,47}$")
TAKE_ID_RE = re.compile(r"^t[0-9]{3,6}$")
MAX_NOTES = 1000
MAX_TAKE_DURATION_S = 3600.0


class RecordingError(ValueError):
    """Invalid request or state (reported to the UI as a 400 error)."""


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="microseconds")


def validate_placement(name) -> str:
    """Return the slug of a user-entered placement name (e.g. 'Living room' ->
    'living-room'). Only letters, digits, space, '_' and '-' are allowed."""
    if not isinstance(name, str) or not PLACEMENT_RE.match(name.strip() or "-"):
        raise RecordingError("placement must be 1-40 characters: letters, digits, space, '_' or '-'")
    slug = re.sub(r"[ ]+", "-", name.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-_") or "default"
    return slug


def validate_label(label) -> str:
    if not isinstance(label, str) or label not in LABELS:
        raise RecordingError(f"label must be one of: {', '.join(LABELS)}")
    return label


def validate_session_id(session_id) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise RecordingError("invalid session id")
    return session_id


def validate_take_id(take_id) -> str:
    if not isinstance(take_id, str) or not TAKE_ID_RE.match(take_id):
        raise RecordingError("invalid take id")
    return take_id


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _packet_meta(parsed) -> dict:
    pkt = getattr(parsed, "packet", None) if parsed is not None and parsed.kind == "csi" else None
    if pkt is None:
        return {"rssi": "", "id": "", "channel": "", "len": "", "local_timestamp": ""}
    return {"rssi": pkt.rssi, "id": pkt.seq, "channel": pkt.channel, "len": pkt.len,
            "local_timestamp": pkt.local_timestamp}


class TakeWriter:
    """CSV writer for one labeled take. Not thread-safe on its own; the
    :class:`RecordingManager` serialises access."""

    def __init__(self, path: Path, session_id: str, take_id: str, label: str, flush_every: int = 50) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.take_id = take_id
        self.label = label
        self.flush_every = max(1, int(flush_every))
        self.lines = 0
        self.packets = 0
        self.first_ts: Optional[float] = None
        self.last_ts: Optional[float] = None
        self._fh = open(self.path, "x", encoding="utf-8", newline="")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(TAKE_FIELDS)

    def write(self, ts: Optional[float], line: str, parsed=None) -> None:
        if self._fh.closed:
            return
        ts = time.time() if ts is None else float(ts)
        if parsed is None:
            parsed = parse_any(line, ts)
        meta = _packet_meta(parsed)
        self._writer.writerow([iso(ts), f"{ts:.6f}", self.session_id, self.take_id, self.label, line,
                               meta["rssi"], meta["id"], meta["channel"], meta["len"], meta["local_timestamp"]])
        self.lines += 1
        if parsed.kind == "csi":
            self.packets += 1
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts
        if self.lines % self.flush_every == 0:
            self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()


class Session:
    """A recording session directory and its ``session.json``."""

    def __init__(self, directory: Path, meta: dict) -> None:
        self.dir = Path(directory)
        self.meta = meta

    @property
    def session_id(self) -> str:
        return self.meta["session_id"]

    @property
    def meta_path(self) -> Path:
        return self.dir / SESSION_FILE

    def save(self) -> None:
        write_json_atomic(self.meta_path, self.meta)

    @classmethod
    def load(cls, directory: Path) -> "Session":
        path = Path(directory) / SESSION_FILE
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RecordingError("session not found") from None
        except (OSError, json.JSONDecodeError) as e:
            raise RecordingError(f"cannot read {path.name}: {e}") from None
        if not isinstance(meta, dict) or "session_id" not in meta:
            raise RecordingError("session.json is not a valid EchoGuard session")
        meta.setdefault("takes", [])
        meta.setdefault("next_take_index", len(meta["takes"]) + 1)
        return cls(directory, meta)

    def summary(self) -> dict:
        m = self.meta
        takes = m.get("takes", [])
        return {
            "session_id": m.get("session_id"),
            "created": m.get("created"),
            "placement": m.get("placement"),
            "notes": m.get("notes", ""),
            "source_kind": m.get("source_kind"),
            "synthetic": bool(m.get("synthetic", False)),
            "sensor": m.get("sensor"),
            "n_takes": len(takes),
            "packets": sum(int(t.get("packets") or 0) for t in takes),
            "path": self.dir.as_posix(),
        }

    def take_files(self) -> list:
        """Existing take CSVs in take order."""
        out = []
        for t in sorted(self.meta.get("takes", []), key=lambda t: t.get("index", 0)):
            name = t.get("file")
            if isinstance(name, str) and "/" not in name and "\\" not in name:
                p = self.dir / name
                if p.is_file():
                    out.append(p)
        return out


class SessionStore:
    """Creates, lists and loads sessions under ``root``."""

    def __init__(self, root: Path = DEFAULT_RECORDINGS_DIR) -> None:
        self.root = Path(root)

    def create(self, placement: str, notes: str = "", sensor: Optional[dict] = None,
               source_kind: Optional[str] = None, synthetic: bool = False, now: Optional[float] = None) -> Session:
        slug = validate_placement(placement)
        if not isinstance(notes, str) or len(notes) > MAX_NOTES:
            raise RecordingError(f"notes must be a string of at most {MAX_NOTES} characters")
        now = time.time() if now is None else now
        base = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + slug
        self.root.mkdir(parents=True, exist_ok=True)
        session_id, n = base, 1
        while True:
            try:
                (self.root / session_id).mkdir()
                break
            except FileExistsError:
                n += 1
                session_id = f"{base}-{n}"
        meta = {
            "format": SESSION_FORMAT,
            "session_id": session_id,
            "created": iso(now),
            "created_unix": now,
            "placement": slug,
            "notes": notes,
            "sensor": sensor,
            "source_kind": source_kind,
            "synthetic": bool(synthetic),
            "app_version": __version__,
            "labels": dict(LABELS),
            "takes": [],
            "next_take_index": 1,
        }
        session = Session(self.root / session_id, meta)
        session.save()
        return session

    def path_for(self, session_id: str) -> Path:
        validate_session_id(session_id)
        p = (self.root / session_id).resolve()
        if p.parent != self.root.resolve():
            raise RecordingError("invalid session id")
        return p

    def load(self, session_id: str) -> Session:
        return Session.load(self.path_for(session_id))

    def list(self) -> list:
        out = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir(), reverse=True):
            if d.is_dir() and SESSION_ID_RE.match(d.name) and (d / SESSION_FILE).is_file():
                try:
                    out.append(Session.load(d).summary())
                except RecordingError as e:
                    log.warning("skipping session %s: %s", d.name, e)
        return out


class _EngineSink:
    """Recorder object attached to the engine; forwards raw lines."""

    accepts_parsed = True

    def __init__(self, manager: "RecordingManager", path: Path) -> None:
        self._manager = manager
        self.path = path

    def write(self, ts, line, parsed=None) -> None:
        self._manager._on_line(self, ts, line, parsed)


class RecordingManager:
    """Session / take control shared by the dashboard and the engine thread.

    ``engine`` must provide ``set_recorder``, ``sensor_config()``,
    ``source_kind``, ``synthetic``, ``started_at``, ``running`` and
    ``source_status``. All state is guarded by one lock; the engine thread
    only enters through the sink's ``write``.
    """

    def __init__(self, engine, store: Optional[SessionStore] = None, clock=time.time) -> None:
        self.engine = engine
        self.store = store or SessionStore()
        self.clock = clock
        self._lock = threading.RLock()
        self._session: Optional[Session] = None
        self._take: Optional[dict] = None   # {"writer", "sink", "entry", "duration", "started", "source_marker"}
        self._last_take: Optional[dict] = None

    # ------------------------------------------------------------ sessions
    def create_session(self, placement: str, notes: str = "") -> dict:
        with self._lock:
            if self._take is not None:
                raise RecordingError("stop the running take before creating a new session")
            sensor = self.engine.sensor_config()
            kind = self.engine.source_kind
            synthetic = bool(self.engine.synthetic) if kind else False
            self._session = self.store.create(placement, notes, sensor=sensor, source_kind=kind, synthetic=synthetic)
            return self._session.summary()

    def select_session(self, session_id: str) -> dict:
        with self._lock:
            if self._take is not None:
                raise RecordingError("stop the running take before switching sessions")
            self._session = self.store.load(session_id)
            return self._session.summary()

    def list_sessions(self) -> list:
        return self.store.list()

    @property
    def session(self) -> Optional[Session]:
        return self._session

    # --------------------------------------------------------------- takes
    def can_record(self) -> tuple:
        """(ok, reason). Live hardware recording needs a connected serial source."""
        eng = self.engine
        kind = eng.source_kind
        st = eng.source_status
        if self._session is None:
            return False, "create or select a session first"
        if not kind or not eng.running:
            return False, "no data source is running"
        if st is not None and st.finished:
            return False, "the source has finished"
        if kind == "live" and not (st is not None and st.connected):
            return False, "serial source is disconnected"
        if bool(eng.synthetic) != bool(self._session.meta.get("synthetic", False)):
            return False, ("this session holds synthetic data; the current source is real" if self._session.meta.get("synthetic")
                           else "this session holds real data; do not mix in synthetic data (create a new session)")
        return True, ""

    def start_take(self, label: str, duration_s: Optional[float] = None) -> dict:
        label = validate_label(label)
        if duration_s is not None:
            if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)) or not (0 < duration_s <= MAX_TAKE_DURATION_S):
                raise RecordingError(f"duration must be between 0 and {MAX_TAKE_DURATION_S:g} s")
            duration_s = float(duration_s)
        with self._lock:
            self._check_take_end()
            if self._take is not None:
                raise RecordingError("a take is already running")
            ok, reason = self.can_record()
            if not ok:
                raise RecordingError(f"cannot record: {reason}")
            s = self._session
            index = int(s.meta.get("next_take_index", 1))
            take_id = f"t{index:03d}"
            now = self.clock()
            fname = f"{index:03d}_{label}_{time.strftime('%Y%m%dT%H%M%S', time.localtime(now))}.csv"
            writer = TakeWriter(s.dir / fname, s.session_id, take_id, label)
            sensor = self.engine.sensor_config()
            if s.meta.get("sensor") is None and sensor is not None:
                s.meta["sensor"] = sensor
            if s.meta.get("source_kind") is None:
                s.meta["source_kind"] = self.engine.source_kind
            entry = {
                "take_id": take_id, "index": index, "label": label, "label_name": LABELS[label], "file": fname,
                "start": iso(now), "start_unix": now, "end": None, "end_unix": None, "duration_s": None,
                "packets": 0, "lines": 0, "requested_duration_s": duration_s,
                "source_kind": self.engine.source_kind, "synthetic": bool(self.engine.synthetic),
                "sensor": sensor, "stop_reason": None,
            }
            s.meta["next_take_index"] = index + 1
            s.save()
            sink = _EngineSink(self, writer.path)
            self._take = {"writer": writer, "sink": sink, "entry": entry, "duration": duration_s,
                          "started": now, "source_marker": self.engine.started_at}
            prev = self.engine.set_recorder(sink)
            if prev is not None and prev is not sink and hasattr(prev, "close"):
                prev.close()
            return self._take_status()

    def stop_take(self, reason: str = "user") -> Optional[dict]:
        with self._lock:
            if self._take is None:
                raise RecordingError("no take is running")
            return self._finish(reason)

    def delete_take(self, take_id: str) -> dict:
        validate_take_id(take_id)
        with self._lock:
            s = self._session
            if s is None:
                raise RecordingError("no session selected")
            if self._take is not None and self._take["entry"]["take_id"] == take_id:
                raise RecordingError("stop the take before deleting it")
            takes = s.meta.get("takes", [])
            entry = next((t for t in takes if t.get("take_id") == take_id), None)
            if entry is None:
                raise RecordingError("take not found")
            name = entry.get("file") or ""
            path = (s.dir / name).resolve()
            if path.parent == s.dir.resolve() and path.suffix == ".csv" and path.is_file():
                path.unlink()
            s.meta["takes"] = [t for t in takes if t.get("take_id") != take_id]
            s.save()
            return s.summary()

    def _finish(self, reason: str) -> dict:
        take, self._take = self._take, None
        if self.engine.set_recorder(None) not in (None, take["sink"]):
            log.warning("engine recorder was replaced while a take was running")
        w = take["writer"]
        w.close()
        e = take["entry"]
        end = self.clock()
        if w.last_ts is not None:
            end = max(end, w.last_ts) if reason == "user" else w.last_ts
        e.update({"end": iso(end), "end_unix": end, "duration_s": round(end - e["start_unix"], 3),
                  "packets": w.packets, "lines": w.lines, "stop_reason": reason})
        s = self._session
        s.meta.setdefault("takes", []).append(e)
        try:
            s.save()
        except OSError as ex:
            log.error("could not update %s: %s", s.meta_path, ex)
        self._last_take = dict(e)
        return dict(e)

    def _check_take_end(self) -> None:
        """Stop the take when its duration elapsed or its source ended (wall clock)."""
        take = self._take
        if take is None:
            return
        eng = self.engine
        st = eng.source_status
        if eng.started_at != take["source_marker"]:
            self._finish("source_changed")
        elif st is None or st.finished or not eng.running:
            self._finish("source_ended")
        elif take["duration"] is not None and self.clock() - take["started"] >= take["duration"]:
            self._finish("duration")

    def _on_line(self, sink, ts, line, parsed) -> None:
        """Engine thread: write one raw line to the running take."""
        with self._lock:
            take = self._take
            if take is None or take["sink"] is not sink:
                return
            if self.engine.started_at != take["source_marker"]:
                self._finish("source_changed")
                return
            take["writer"].write(ts, line, parsed)
            d = take["duration"]
            if d is not None and ts is not None and float(ts) - take["started"] >= d:
                self._finish("duration")

    def _take_status(self) -> Optional[dict]:
        take = self._take
        if take is None:
            return None
        w = take["writer"]
        e = take["entry"]
        elapsed = self.clock() - take["started"]
        return {"take_id": e["take_id"], "label": e["label"], "label_name": e["label_name"], "file": e["file"],
                "elapsed_s": round(elapsed, 2), "duration_s": take["duration"], "packets": w.packets, "lines": w.lines}

    def status(self) -> dict:
        with self._lock:
            self._check_take_end()
            ok, reason = self.can_record()
            s = self._session
            return {
                "session": s.summary() if s is not None else None,
                "takes": list(s.meta.get("takes", [])) if s is not None else [],
                "active_take": self._take_status(),
                "last_take": self._last_take,
                "can_record": ok,
                "reason": reason or None,
                "labels": [{"key": k, "name": v} for k, v in LABELS.items()],
            }

    def close(self) -> None:
        with self._lock:
            if self._take is not None:
                self._finish("shutdown")
