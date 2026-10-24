"""Raw recording and CSV export.

Recordings store the *raw serial line* plus host timestamps, so a replay goes
through exactly the same parser as live data. Extra constant or per-row
columns (label, session id, ...) can be appended after the raw fields.
"""

from __future__ import annotations

import csv
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .csi_parser import CSIPacket, subcarrier_labels, valid_subcarrier_amplitudes

RAW_FIELDS = ["host_ts_iso", "host_ts_unix", "raw_line"]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="microseconds")


class RawRecorder:
    """Thread-safe CSV writer of ``host_ts_iso,host_ts_unix,raw_line[,extra...]``."""

    def __init__(self, path, extra_fields: Optional[dict] = None, flush_every: int = 50) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.extra_defaults = dict(extra_fields or {})
        self.fieldnames = RAW_FIELDS + list(self.extra_defaults.keys())
        self.flush_every = max(1, int(flush_every))
        self.rows_written = 0
        self._lock = threading.Lock()
        self._fh = open(self.path, "w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.fieldnames, extrasaction="ignore")
        self._writer.writeheader()

    def write(self, host_ts: Optional[float], line: str, extra: Optional[dict] = None) -> None:
        ts = time.time() if host_ts is None else float(host_ts)
        row = {"host_ts_iso": _iso(ts), "host_ts_unix": f"{ts:.6f}", "raw_line": line}
        row.update(self.extra_defaults)
        if extra:
            row.update(extra)
        with self._lock:
            if self._fh.closed:
                return
            self._writer.writerow(row)
            self.rows_written += 1
            if self.rows_written % self.flush_every == 0:
                self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def export_amplitudes_csv(packets: Iterable[CSIPacket], path) -> int:
    """Write per-packet metadata and valid-subcarrier amplitudes to CSV.

    The column layout is taken from the first packet; packets with a different
    layout (chip family or subcarrier count) are skipped. Returns rows written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    labels = None
    family = None
    width = None
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for pkt in packets:
            amps = valid_subcarrier_amplitudes(pkt)
            if labels is None:
                labels = subcarrier_labels(pkt)
                family, width = pkt.chip_family, len(amps)
                w.writerow(["host_ts_unix", "seq", "chip_family", "rssi", "noise_floor", "channel",
                            "local_timestamp", "len"] + labels)
            if pkt.chip_family != family or len(amps) != width:
                continue
            ts = "" if pkt.host_ts is None else f"{pkt.host_ts:.6f}"
            w.writerow([ts, pkt.seq, pkt.chip_family, pkt.rssi, pkt.noise_floor, pkt.channel,
                        pkt.local_timestamp, pkt.len]
                       + ["" if a != a else f"{a:.3f}" for a in amps])
            rows += 1
    return rows
