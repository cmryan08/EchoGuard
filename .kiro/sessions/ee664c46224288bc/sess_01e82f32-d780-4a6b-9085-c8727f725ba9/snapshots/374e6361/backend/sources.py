"""Line sources: live serial, and replay of recordings / raw serial logs.

Every source yields ``(host_ts, line)`` tuples of raw text so live data,
replays (and, later, simulation) all go through the same parser and pipeline.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

from .recording import LABELS, Session

Line = Tuple[float, str]

# Upper bound for a partial line kept between reads (a full HT40 CSI line is < 4 kB).
_MAX_PENDING = 64 * 1024


@dataclass
class SourceStatus:
    kind: str
    port: str = ""
    connected: bool = False
    finished: bool = False
    reconnect_count: int = 0
    last_error: str = ""
    bytes_read: int = 0
    lines_read: int = 0
    connected_since: Optional[float] = None
    extra: dict = field(default_factory=dict)


class LineSource:
    """Base class. Iterate :meth:`lines`; call :meth:`stop` from another thread."""

    kind = "base"

    def __init__(self) -> None:
        self._stop = threading.Event()
        self.status = SourceStatus(kind=self.kind)

    def lines(self) -> Iterator[Line]:  # pragma: no cover - interface
        raise NotImplementedError

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def __iter__(self):
        return self.lines()


class SerialSource(LineSource):
    """Read lines from an ESP32 over USB serial with automatic reconnect.

    ``port`` may be a device path, a pyserial URL (``loop://`` for tests) or
    ``"auto"``, which is resolved via :func:`backend.ports.auto_select` on every
    (re)connect so a re-enumerated board is found again.
    """

    kind = "serial"

    def __init__(
        self,
        port: str,
        baud: int = 921600,
        read_timeout: float = 0.2,
        backoff_initial: float = 0.5,
        backoff_max: float = 5.0,
        serial_factory: Optional[Callable[[str, int, float], object]] = None,
        port_resolver: Optional[Callable[[], str]] = None,
    ) -> None:
        super().__init__()
        self.port = port
        self.baud = baud
        self.read_timeout = read_timeout
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self._factory = serial_factory or self._default_factory
        self._resolver = port_resolver
        self._ser = None
        self.status.port = port

    @staticmethod
    def _default_factory(port: str, baud: int, timeout: float):
        import serial

        return serial.serial_for_url(port, baudrate=baud, timeout=timeout)

    def _resolve_port(self) -> str:
        if self.port != "auto":
            return self.port
        if self._resolver is not None:
            return self._resolver()
        from .ports import auto_select, probe_port

        return auto_select(probe=lambda dev: probe_port(dev, baud=self.baud))

    def _open(self) -> bool:
        try:
            device = self._resolve_port()
            self._ser = self._factory(device, self.baud, self.read_timeout)
        except Exception as e:  # noqa: BLE001 - SerialException, OSError, PortSelectionError
            self._ser = None
            self.status.connected = False
            self.status.last_error = str(e)
            return False
        self.status.port = device
        self.status.connected = True
        self.status.connected_since = time.time()
        self.status.last_error = ""
        return True

    def _close(self) -> None:
        ser, self._ser = self._ser, None
        self.status.connected = False
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass

    def lines(self) -> Iterator[Line]:
        backoff = self.backoff_initial
        pending = b""
        ever_connected = False
        try:
            while not self._stop.is_set():
                if self._ser is None:
                    if not self._open():
                        self._stop.wait(backoff)
                        backoff = min(backoff * 2, self.backoff_max)
                        continue
                    if ever_connected:
                        self.status.reconnect_count += 1
                    ever_connected = True
                    backoff = self.backoff_initial
                    pending = b""
                try:
                    chunk = self._ser.readline()
                except Exception as e:  # noqa: BLE001 - SerialException / OSError on unplug
                    self.status.last_error = f"disconnected: {e}"
                    self._close()
                    continue
                if not chunk:
                    continue
                self.status.bytes_read += len(chunk)
                pending += chunk
                if not pending.endswith(b"\n"):
                    # readline() timed out mid-line; keep the fragment.
                    if len(pending) > _MAX_PENDING:
                        pending = b""
                    continue
                text = pending.decode("utf-8", errors="replace").rstrip("\r\n")
                pending = b""
                self.status.lines_read += 1
                yield time.time(), text
        finally:
            self._close()


#: Pause inserted between consecutive takes when replaying a whole session.
SESSION_TAKE_GAP_S = 0.5
#: Ground-truth keys published in ``status.extra`` for labeled recordings.
GROUND_TRUTH_KEYS = ("session_id", "take_id", "label", "label_name")


class ReplaySource(LineSource):
    """Re-emit recorded lines through the normal pipeline.

    Accepts these inputs:

    * EchoGuard recordings (CSV with ``host_ts_unix`` and ``raw_line``
      columns): :class:`backend.recorder.RawRecorder` files and labeled take
      CSVs from :mod:`backend.recording`. Original relative timing is
      reproduced, scaled by ``speed``. For labeled takes the current
      ``session_id`` / ``take_id`` / ``label`` / ``label_name`` are published in
      ``status.extra`` as ground truth.
    * a session directory (containing ``session.json``): all takes are
      replayed in take order with a short pause between takes.
    * plain raw serial logs (``.txt`` / ``.log``, e.g. ``idf.py monitor``
      captures, test fixtures): no host timestamps exist, so lines are emitted
      at ``line_rate_hz`` (scaled by ``speed``).

    ``speed=0`` means as fast as possible. Yielded ``host_ts`` values follow
    the (scaled) original timeline starting at the replay start time, so rate
    calculations stay meaningful. ``synthetic`` is taken from the session's
    ``session.json`` when one exists (simulated recordings stay marked).
    """

    kind = "replay"

    def __init__(self, path, speed: float = 1.0, loop: bool = False, line_rate_hz: float = 100.0) -> None:
        super().__init__()
        self.path = Path(path)
        self.speed = float(speed)
        self.loop = loop
        self.line_rate_hz = float(line_rate_hz)
        self.synthetic = False
        self._files = [self.path]
        self.status.port = str(self.path)
        self.recording_format = self._detect_format()
        self.status.extra["format"] = self.recording_format
        if self.recording_format in ("recording", "session") and self._has_labels:
            for k in GROUND_TRUTH_KEYS:
                self.status.extra[k] = None

    def _detect_format(self) -> str:
        self._has_labels = False
        session_json = None
        if self.path.is_dir():
            session_json = self.path / "session.json"
            if not session_json.is_file():
                raise FileNotFoundError(f"{self.path} is not a session directory (no session.json)")
        elif (self.path.parent / "session.json").is_file():
            session_json = self.path.parent / "session.json"
        if session_json is not None:
            try:
                meta = json.loads(session_json.read_text(encoding="utf-8"))
                self.synthetic = bool(meta.get("synthetic", False))
            except (OSError, ValueError, AttributeError):
                self.synthetic = False
            if self.path.is_dir():
                self._files = Session.load(self.path).take_files()
                self.status.extra["takes"] = len(self._files)
                self._has_labels = True
                return "session"
        with open(self.path, "r", encoding="utf-8", errors="replace", newline="") as f:
            header = next(csv.reader([f.readline()]), [])
        if "host_ts_unix" in header and "raw_line" in header:
            self._has_labels = "label" in header
            return "recording"
        return "raw_log"

    @staticmethod
    def _iter_csv(path: Path) -> Iterator[Tuple[float, str, dict]]:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            t0 = None
            for row in csv.DictReader(f):
                try:
                    ts = float(row["host_ts_unix"])
                except (TypeError, ValueError, KeyError):
                    continue
                if t0 is None:
                    t0 = ts
                yield ts - t0, row.get("raw_line") or "", row

    def _iter_original(self) -> Iterator[Tuple[float, str, Optional[dict]]]:
        """Yield (original_relative_seconds, line, csv_row_or_None)."""
        if self.recording_format == "session":
            offset = 0.0
            for path in self._files:
                last = 0.0
                for rel, line, row in self._iter_csv(path):
                    last = rel
                    yield offset + rel, line, row
                offset += last + SESSION_TAKE_GAP_S
        elif self.recording_format == "recording":
            yield from self._iter_csv(self.path)
        else:
            period = 1.0 / self.line_rate_hz if self.line_rate_hz > 0 else 0.0
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    yield i * period, line.rstrip("\r\n"), None

    def lines(self) -> Iterator[Line]:
        self.status.connected = True
        self.status.connected_since = time.time()
        start = time.time()
        loop_offset = 0.0
        extra = self.status.extra
        try:
            while not self._stop.is_set():
                last_rel = 0.0
                for rel, line, row in self._iter_original():
                    if self._stop.is_set():
                        return
                    rel_scaled = (rel / self.speed) if self.speed > 0 else rel
                    last_rel = rel_scaled
                    target = start + loop_offset + rel_scaled
                    if self.speed > 0:
                        delay = target - time.time()
                        if delay > 0 and self._stop.wait(delay):
                            return
                    if row is not None and self._has_labels:
                        label = row.get("label") or None
                        if label != extra.get("label") or row.get("take_id") != extra.get("take_id"):
                            extra["session_id"] = row.get("session_id") or None
                            extra["take_id"] = row.get("take_id") or None
                            extra["label"] = label
                            extra["label_name"] = LABELS.get(label, label)
                    self.status.lines_read += 1
                    self.status.bytes_read += len(line) + 1
                    yield target, line
                if not self.loop:
                    break
                gap = (1.0 / self.line_rate_hz) if self.line_rate_hz > 0 else 0.01
                loop_offset += last_rel + gap
        finally:
            self.status.connected = False
            self.status.finished = True
