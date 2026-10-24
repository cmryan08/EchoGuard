"""CSV export of amplitudes + motion features + labels to ``data/exports``.

Two sources:

* :func:`export_buffer` - the Engine's in-memory packet buffer (what the
  dashboard currently shows), joined with the latest frame at each packet,
* :func:`export_session` - a recorded session (or one take), replayed offline
  through the same pipeline (:mod:`backend.offline`) with each take's label.

Each row is one CSI packet: timestamps, source kind, ``synthetic`` flag,
label, packet metadata, the frame features in effect at that packet
(motion score, variance, diff energy, baseline deviation, quality, activity)
and the raw amplitudes of the valid subcarriers (``amp_<subcarrier>``).
Synthetic data stays marked synthetic in every row.
"""

from __future__ import annotations

import csv
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from .config import DATA_DIR
from .csi_parser import FAMILY_CLASSIC, VALID_LLTF_INDICES, valid_subcarrier_amplitudes

DEFAULT_EXPORT_DIR = DATA_DIR / "exports"
EXPORT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.csv$")

BASE_FIELDS = ["host_ts_iso", "host_ts_unix", "source_kind", "synthetic", "session_id", "take_id", "label",
               "seq", "rssi", "channel", "len", "chip_family", "motion_score", "variance", "diff_energy",
               "baseline_deviation", "quality_ok", "activity"]


class ExportError(ValueError):
    """Invalid export request (HTTP 400)."""


def validate_export_name(name) -> str:
    if not isinstance(name, str) or not EXPORT_NAME_RE.match(name) or ".." in name:
        raise ExportError("invalid export file name")
    return name


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="microseconds")


def _fmt(v, digits: int = 6):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return "" if not math.isfinite(v) else f"{v:.{digits}g}"
    return v


def _amp_labels(pkt) -> list:
    n = len(valid_subcarrier_amplitudes(pkt))
    if pkt.chip_family == FAMILY_CLASSIC and n == len(VALID_LLTF_INDICES):
        return [f"amp_lltf_{i}" for i in VALID_LLTF_INDICES]
    return [f"amp_sc_{i}" for i in range(n)]


class _Writer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows = 0
        self._fh = None
        self._amp_fields: Optional[list] = None
        self._w = None

    def write(self, pkt, frame, activity, meta: dict) -> None:
        amps = valid_subcarrier_amplitudes(pkt)
        if self._fh is None:
            self._amp_fields = _amp_labels(pkt)
            self._fh = open(self.path, "x", encoding="utf-8", newline="")
            self._w = csv.writer(self._fh)
            self._w.writerow(BASE_FIELDS + self._amp_fields)
        if len(amps) != len(self._amp_fields):
            return  # layout changed mid-export: keep the file rectangular
        ts = float(pkt.host_ts) if pkt.host_ts is not None else time.time()
        row = [_iso(ts), f"{ts:.6f}", meta.get("source_kind") or "", _fmt(bool(meta.get("synthetic"))),
               meta.get("session_id") or "", meta.get("take_id") or "", meta.get("label") or "",
               pkt.seq, pkt.rssi, pkt.channel, pkt.len, pkt.chip_family]
        if frame is not None:
            row += [_fmt(frame.motion_score), _fmt(frame.variance), _fmt(frame.diff_energy),
                    _fmt(frame.baseline_deviation), _fmt(bool(frame.quality_ok)), activity or ""]
        else:
            row += ["", "", "", "", "", ""]
        row += [_fmt(float(a), 5) for a in np.asarray(amps, dtype=float)]
        self._w.writerow(row)
        self.rows += 1

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()


def _target(export_dir: Path, stem: str) -> Path:
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_.-]", "-", stem)[:80].strip("-.") or "export"
    base = f"{stem}_{time.strftime('%Y%m%d-%H%M%S')}"
    name, n = f"{base}.csv", 1
    while (export_dir / name).exists():
        n += 1
        name = f"{base}-{n}.csv"
    return export_dir / validate_export_name(name)


def _finish(writer: _Writer, meta: dict) -> dict:
    writer.close()
    if writer.rows == 0:
        if writer.path.exists():
            writer.path.unlink()
        raise ExportError("nothing to export: no CSI packets")
    return {"name": writer.path.name, "path": writer.path.as_posix(), "rows": writer.rows,
            "synthetic": bool(meta.get("synthetic")), "size": writer.path.stat().st_size}


def export_buffer(engine, export_dir: Path = DEFAULT_EXPORT_DIR) -> dict:
    """Export the engine's buffered packets with the frame features in effect."""
    packets = engine.buffer_packets()
    if not packets:
        raise ExportError("nothing to export: the buffer is empty")
    frames = engine.frames()
    snap_kind = engine.source_kind
    synthetic = bool(engine.synthetic)
    st = engine.source_status
    gt = (st.extra if st is not None else {}) or {}
    meta = {"source_kind": snap_kind, "synthetic": synthetic, "label": gt.get("label"),
            "session_id": gt.get("session_id"), "take_id": gt.get("take_id")}
    stem = ("buffer_" + (snap_kind or "none")) + ("_synthetic" if synthetic else "")
    writer = _Writer(_target(export_dir, stem))
    fi = -1
    try:
        for pkt in packets:
            ts = pkt.host_ts if pkt.host_ts is not None else 0.0
            while fi + 1 < len(frames) and frames[fi + 1][0].ts <= ts:
                fi += 1
            frame, act = frames[fi] if fi >= 0 else (None, None)
            writer.write(pkt, frame, act, meta)
    except Exception:
        writer.close()
        raise
    return _finish(writer, meta)


def export_session(path: Path, config=None, export_dir: Path = DEFAULT_EXPORT_DIR) -> dict:
    """Replay a session directory or take CSV offline and export every packet."""
    from .offline import OfflineReplay

    rep = OfflineReplay(path, config)
    synthetic = rep.synthetic
    stem = (Path(path).name if Path(path).is_dir() else Path(path).stem) + ("_synthetic" if synthetic else "")
    writer = _Writer(_target(export_dir, "session_" + stem))
    meta = {"source_kind": "replay", "synthetic": synthetic}
    last_frame, last_act = None, None
    try:
        for step in rep:
            if step.frame is not None:
                last_frame, last_act = step.frame, step.activity
            meta.update({"label": step.label, "take_id": step.take_id, "session_id": step.session_id})
            writer.write(step.packet, last_frame, last_act, meta)
    except Exception:
        writer.close()
        raise
    return _finish(writer, meta)


def list_exports(export_dir: Path = DEFAULT_EXPORT_DIR) -> list:
    d = Path(export_dir)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.is_file() and EXPORT_NAME_RE.match(p.name):
            out.append({"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime,
                        "synthetic": "_synthetic" in p.name})
    return out


def iter_names(items: Iterable[dict]) -> list:
    return [i["name"] for i in items]
