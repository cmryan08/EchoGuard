"""Line sources: live serial, and replay of recordings / raw serial logs.

Every source yields ``(host_ts, line)`` tuples of raw text so live data,
replays (and, later, simulation) all go through the same parser and pipeline.
"""

from __future__ import annotations

import csv
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

from .recorder import RAW_FIELDS

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


class ReplaySource(LineSource):
    """Re-emit recorded lines through the normal pipeline.

    Accepts two formats:

    * EchoGuard recordings written by :class:`backend.recorder.RawRecorder`
      (CSV with ``host_ts_iso,host_ts_unix,raw_line,...``): original relative
      timing is reproduced, scaled by ``speed``.
    * plain raw serial logs (``.txt`` / ``.log``, e.g. ``idf.py monitor``
      captures, test fixtures): no host timestamps exist, so lines are emitted
      at ``line_rate_hz`` (scaled by ``speed``).

    ``speed=0`` means as fast as possible. Yielded ``host_ts`` values follow
    the (scaled) original timeline starting at the replay start time, so rate
    calculations stay meaningful.
    """

    kind = "replay"

    def __init__(self, path, speed: float = 1.0, loop: bool = False, line_rate_hz: float = 100.0) -> None:
        super().__init__()
        self.path = Path(path)
        self.speed = float(speed)
        self.loop = loop
        self.line_rate_hz = float(line_rate_hz)
        self.status.port = str(self.path)
        self.recording_format = self._detect_format()
        self.status.extra["format"] = self.recording_format

    def _detect_format(self) -> str:
        with open(self.path, "r", encoding="utf-8", errors="replace", newline="") as f:
            first = f.readline()
        return "recording" if first.startswith(",".join(RAW_FIELDS)) else "raw_log"

    def _iter_original(self) -> Iterator[Tuple[float, str]]:
        """Yield (original_relative_seconds, line)."""
        if self.recording_format == "recording":
            with open(self.path, "r", encoding="utf-8", errors="replace", newline="") as f:
                t0 = None
                for row in csv.DictReader(f):
                    try:
                        ts = float(row["host_ts_unix"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    if t0 is None:
                        t0 = ts
                    yield ts - t0, row.get("raw_line") or ""
        else:
            period = 1.0 / self.line_rate_hz if self.line_rate_hz > 0 else 0.0
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    yield i * period, line.rstrip("\r\n")

    def lines(self) -> Iterator[Line]:
        self.status.connected = True
        self.status.connected_since = time.time()
        start = time.time()
        loop_offset = 0.0
        try:
            while not self._stop.is_set():
                last_rel = 0.0
                for rel, line in self._iter_original():
                    if self._stop.is_set():
                        return
                    rel_scaled = (rel / self.speed) if self.speed > 0 else rel
                    last_rel = rel_scaled
                    target = start + loop_offset + rel_scaled
                    if self.speed > 0:
                        delay = target - time.time()
                        if delay > 0 and self._stop.wait(delay):
                            return
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
