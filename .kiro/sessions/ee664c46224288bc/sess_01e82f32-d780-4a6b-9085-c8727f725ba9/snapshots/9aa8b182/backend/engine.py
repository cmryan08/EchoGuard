"""Background acquisition engine shared by the dashboard (and later tools).

The engine owns one :class:`backend.sources.LineSource` (serial, replay, and
later simulation), reads it in a daemon thread, feeds every raw line through
:class:`backend.buffer.CSIBuffer` (parser + counters), the optional raw
recorder and :class:`backend.processing.SignalProcessor`, and keeps a bounded
history of motion frames plus normalised amplitude rows for the heatmap.

All public read methods return JSON-serialisable structures. Nothing is ever
synthesised: when no data has arrived, values are ``None`` / empty lists.
"""

from __future__ import annotations

import copy
import logging
import math
import threading
import time
from collections import deque
from dataclasses import asdict, is_dataclass
from typing import Callable, Optional

import numpy as np

from .activity import Activity, classify_activity
from .buffer import CSIBuffer
from .config import AppConfig, MotionThresholds
from .csi_parser import FAMILY_CLASSIC, VALID_LLTF_INDICES
from .processing import Q_STALE, SignalProcessor

log = logging.getLogger(__name__)

#: LineSource.kind -> dashboard source kind.
SOURCE_KINDS = {"serial": "live", "replay": "replay", "simulation": "simulation"}
#: classic rx_ctrl 'bandwidth' (cwb) field -> MHz.
BANDWIDTH_MHZ = {0: 20, 1: 40}


def json_safe(obj):
    """Recursively convert dataclasses / numpy values / non-finite floats to JSON-safe values."""
    if is_dataclass(obj) and not isinstance(obj, type):
        obj = asdict(obj)
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [json_safe(v) for v in obj.tolist()]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def _round_row(row: np.ndarray, digits: int = 4) -> list:
    return [round(float(v), digits) if math.isfinite(v) else None for v in row]


def _ground_truth(extra: dict) -> Optional[dict]:
    """Label of the take currently being replayed (from ReplaySource.status.extra)."""
    if not extra or not extra.get("label"):
        return None
    return {k: extra.get(k) for k in ("session_id", "take_id", "label", "label_name")}


class Engine:
    """Thread-safe owner of the acquisition pipeline."""

    def __init__(self, config: Optional[AppConfig] = None, buffer_maxlen: int = 3000,
                 history_s: float = 120.0, heatmap_maxlen: int = 600) -> None:
        self.config = config or AppConfig()
        self.history_s = float(history_s)
        self.buffer_maxlen = int(buffer_maxlen)
        self.heatmap_maxlen = int(heatmap_maxlen)
        self.processor = SignalProcessor(self.config.processing)
        #: Callables ``hook(frame, activity)`` run in the engine thread after each frame.
        self.frame_hooks: list = []
        self._lock = threading.RLock()
        self._gen = 0
        self._thread: Optional[threading.Thread] = None
        self._source = None
        self._kind: Optional[str] = None
        self._synthetic = False
        self._recorder = None
        self._base_thresholds = copy.deepcopy(self.config.thresholds)
        self._calibration_info: Optional[dict] = None
        #: Forced activity (e.g. POTENTIAL_FALL while an alert is unresolved).
        self._activity_override: Optional[Activity] = None
        #: name -> callable returning extra JSON-safe data merged into snapshot().
        self.status_providers: dict = {}
        self._reset_state()

    def _reset_state(self) -> None:
        self._buffer = CSIBuffer(maxlen=self.buffer_maxlen)
        self.processor.reset()
        self._frames: deque = deque(maxlen=max(100, int(self.history_s * max(1.0, self.config.processing.frame_rate_hz) * 2)))
        self._heat: deque = deque(maxlen=self.heatmap_maxlen)
        self._activity = Activity.UNKNOWN
        self._engine_error = ""
        self._line_errors = 0
        self._last_packet_wall: Optional[float] = None
        self._started_at: Optional[float] = None

    # ------------------------------------------------------------ control
    def start(self, source, synthetic: Optional[bool] = None) -> None:
        """Stop any running source, reset state and start reading ``source``."""
        self.stop()
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._reset_state()
            self._source = source
            self._kind = SOURCE_KINDS.get(source.kind, source.kind)
            if synthetic is None:
                synthetic = bool(getattr(source, "synthetic", False)) or self._kind == "simulation"
            self._synthetic = bool(synthetic)
            self._started_at = time.time()
            self._thread = threading.Thread(target=self._run, args=(source, gen), name="echoguard-engine", daemon=True)
            self._thread.start()

    def switch_source(self, source, synthetic: Optional[bool] = None) -> None:
        self.start(source, synthetic=synthetic)

    def stop(self, timeout: float = 3.0) -> None:
        """Stop the current source (data stays visible until the next start)."""
        with self._lock:
            src, thread = self._source, self._thread
            self._gen += 1
        if src is not None:
            src.stop()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)

    def join(self, timeout: Optional[float] = None) -> bool:
        """Wait for the reader thread to end (e.g. a finished replay). True if ended."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_recorder(self, recorder):
        """Attach (or detach with ``None``) an object with ``write(ts, line)``
        (or ``write(ts, line, parsed)`` when it sets ``accepts_parsed = True``).
        Returns the previous recorder; closing it is the caller's job."""
        with self._lock:
            prev, self._recorder = self._recorder, recorder
        return prev

    # ------------------------------------------------- source / sensor info
    @property
    def source_kind(self) -> Optional[str]:
        """'live' | 'replay' | 'simulation' or None when no source was started."""
        return self._kind

    @property
    def synthetic(self) -> bool:
        return bool(self._synthetic) if self._source is not None else False

    @property
    def started_at(self) -> Optional[float]:
        """Wall time of the last :meth:`start` (identifies the current source)."""
        return self._started_at

    @property
    def source_status(self):
        src = self._source
        return src.status if src is not None else None

    def sensor_config(self) -> Optional[dict]:
        """Sensor configuration seen in the newest packet, or None before any packet.

        Keys: tx_mac, channel, secondary_channel, chip_family, csi_len,
        n_subcarriers (used to key calibrations and label recordings)."""
        with self._lock:
            pkt = self._buffer.latest()
            layout = self.processor.layout
        if pkt is None:
            return None
        return {
            "tx_mac": pkt.mac,
            "channel": pkt.channel,
            "secondary_channel": pkt.secondary_channel,
            "chip_family": pkt.chip_family,
            "csi_len": pkt.len,
            "n_subcarriers": layout[1] if layout else None,
        }

    # ---------------------------------------------------------- calibration
    def apply_calibration(self, mean_profile, std_profile, noise_variance, noise_diff, thresholds: dict,
                          info: Optional[dict] = None) -> None:
        """Use a calibration: baseline + noise normalisation in the processor and
        calibrated motion thresholds. Thread-safe."""
        th = MotionThresholds(low=float(thresholds["low"]), movement=float(thresholds["movement"]),
                              rapid=float(thresholds["rapid"]), calibrated=True)
        info = dict(info or {})
        with self._lock:
            self.processor.set_baseline(mean_profile, std_profile, noise_variance=noise_variance,
                                        noise_diff=noise_diff, name=str(info.get("name", "")))
            self.config.thresholds = th
            self._calibration_info = info

    def clear_calibration(self) -> None:
        """Back to the adaptive reference and the configured (uncalibrated) thresholds."""
        with self._lock:
            self.processor.clear_baseline()
            self.config.thresholds = copy.deepcopy(self._base_thresholds)
            self._calibration_info = None

    @property
    def calibration_info(self) -> Optional[dict]:
        info = self._calibration_info
        return dict(info) if info is not None else None

    @property
    def base_thresholds(self) -> MotionThresholds:
        """The configured (uncalibrated) thresholds from data/config.json."""
        return copy.deepcopy(self._base_thresholds)

    # ------------------------------------------------------------- thread
    def _run(self, source, gen: int) -> None:
        try:
            for ts, line in source.lines():
                if gen != self._gen:
                    break
                try:
                    self._handle_line(ts, line, gen)
                except Exception as e:  # noqa: BLE001 - one bad line must not stop acquisition
                    with self._lock:
                        self._line_errors += 1
                        self._engine_error = f"line processing error: {e!r}"
                    log.exception("error processing line")
        except Exception as e:  # noqa: BLE001 - source crashed; keep the process alive
            with self._lock:
                if gen == self._gen:
                    self._engine_error = f"source error: {e!r}"
            log.exception("source thread crashed")

    def _handle_line(self, ts: float, line: str, gen: int) -> None:
        with self._lock:
            if gen != self._gen:
                return
            parsed = self._buffer.ingest(ts, line)
            rec = self._recorder
        if rec is not None:
            if getattr(rec, "accepts_parsed", False):
                rec.write(ts, line, parsed)
            else:
                rec.write(ts, line)
        if parsed.kind != "csi":
            return
        pkt = parsed.packet
        frame = activity = None
        with self._lock:
            if gen != self._gen:
                return
            proc = self.processor
            before = proc.packets_processed
            frame = proc.process(pkt)
            self._last_packet_wall = time.time()
            if proc.packets_processed > before and proc.last_profile is not None:
                self._heat.append((pkt.host_ts, proc.last_profile.astype(np.float32), proc.layout))
            if frame is not None:
                activity = classify_activity(frame, self.config.thresholds)
                shown = self._activity_override or activity
                self._activity = shown
                self._frames.append((frame, shown.value))
                cutoff = frame.ts - self.history_s
                while self._frames and self._frames[0][0].ts < cutoff:
                    self._frames.popleft()
        if frame is not None:
            # hooks get the threshold classification (not the alert override)
            for hook in list(self.frame_hooks):
                try:
                    hook(frame, activity)
                except Exception:  # noqa: BLE001
                    log.exception("frame hook failed")

    # ------------------------------------------------------------- output
    def snapshot(self) -> dict:
        now = time.time()
        cfg = self.config
        with self._lock:
            src = self._source
            kind = self._kind
            st = src.status if src is not None else None
            stats = self._buffer.snapshot(now=now if kind in ("live", "simulation") else None)
            latest = self._buffer.latest()
            frame = self._frames[-1][0] if self._frames else None
            activity = self._activity_override or self._activity
            proc = self.processor
            last_wall = self._last_packet_wall
            baseline = proc.baseline
            calib_active = proc.baseline_active
            calib_info = dict(self._calibration_info) if self._calibration_info else None
            layout = proc.layout
            counters_extra = {
                "invalid_packets": proc.invalid_packets,
                "outliers_replaced": proc.outliers_replaced,
                "layout_changes": proc.layout_changes,
                "frames": len(self._frames),
                "line_errors": self._line_errors,
            }
            engine_error = self._engine_error
            recorder = self._recorder
            started_at = self._started_at
            running = self.running

        age = (now - last_wall) if last_wall is not None else None
        if src is None:
            quality = {"ok": None, "reasons": []}
        elif stats.csi_packets == 0 or frame is None:
            quality = {"ok": False, "reasons": ["no_data: no CSI packets processed yet"]}
        else:
            reasons = list(frame.quality_reasons)
            if age is not None and age > cfg.processing.stale_after_s and not any(r.startswith(Q_STALE) for r in reasons):
                reasons.append(f"{Q_STALE}: no packets for {age:.1f} s")
            if st is not None and st.finished:
                reasons.append("source_finished: the source has ended")
            quality = {"ok": not reasons, "reasons": reasons}
        if not quality["ok"] and activity != Activity.POTENTIAL_FALL:
            activity = Activity.UNKNOWN

        source = {
            "kind": kind,
            "synthetic": self._synthetic if src is not None else False,
            "running": running,
            "connected": bool(st.connected) if st else False,
            "port": (st.port or None) if st else None,
            "reconnects": st.reconnect_count if st else 0,
            "last_error": (st.last_error or None) if st else None,
            "finished": bool(st.finished) if st else False,
            "lines_read": st.lines_read if st else 0,
            "bytes_read": st.bytes_read if st else 0,
            "connected_since": st.connected_since if st else None,
            "started_at": started_at,
            "extra": dict(st.extra) if st else {},
            "engine_error": engine_error or None,
        }
        bw = latest.bandwidth if latest is not None else None
        device = {
            "chip_family": stats.chip_family,
            "header_family": stats.header_family,
            "tx_mac": latest.mac if latest is not None else None,
            "channel": latest.channel if latest is not None else None,
            "secondary_channel": latest.secondary_channel if latest is not None else None,
            "bandwidth": bw,
            "bandwidth_mhz": BANDWIDTH_MHZ.get(bw) if bw is not None else None,
            "csi_len": stats.last_len,
            "n_subcarriers": layout[1] if layout else None,
            "last_seq": latest.seq if latest is not None else None,
            "rx_stats": stats.last_rx_stats,
            "tx_stats": stats.last_tx_stats,
        }
        counters = {
            "lines": stats.total_lines,
            "packets": stats.csi_packets,
            "malformed": stats.malformed,
            "malformed_reasons": stats.malformed_reasons,
            "non_csi_lines": stats.non_csi_lines,
            "seq_gaps": stats.seq_gaps,
            "seq_resets": stats.seq_resets,
            "duplicates": stats.duplicates,
            "buffered": stats.buffered,
            **counters_extra,
        }
        snap = {
            "server_time": now,
            "source": source,
            "config": device,
            "counters": counters,
            "packet_rate_hz": stats.packet_rate_hz,
            "rssi": {"last": stats.last_rssi, "mean": stats.rssi_mean, "std": stats.rssi_std},
            "noise_floor": stats.noise_floor,
            "last_packet_age_s": age,
            "quality": quality,
            "activity": {"key": activity.value, "label": activity.label},
            "motion": frame.to_dict() if frame is not None else None,
            "thresholds": cfg.thresholds,
            "calibration": {
                "active": calib_active,
                "loaded": baseline is not None,
                "name": (baseline.name or None) if baseline is not None else None,
                "baseline_source": "calibration" if calib_active else "adaptive",
                "key": calib_info.get("key") if calib_info else None,
                "created": calib_info.get("created") if calib_info else None,
                "synthetic": bool(calib_info.get("synthetic")) if calib_info else False,
                "auto_loaded": bool(calib_info.get("auto_loaded")) if calib_info else False,
            },
            "ground_truth": _ground_truth(source["extra"]),
            "processing": cfg.processing,
            "recording": {"active": recorder is not None, "path": str(getattr(recorder, "path", "")) or None},
        }
        for name, provider in list(self.status_providers.items()):
            try:
                snap[name] = provider()
            except Exception as e:  # noqa: BLE001 - a broken extension must not break /api/status
                log.exception("status provider %s failed", name)
                snap[name] = {"error": repr(e)}
        return json_safe(snap)

    def heatmap(self, n: int = 300) -> dict:
        """Last ``n`` normalised amplitude rows (packets x subcarriers), NaN -> None.

        Rows with a different layout than the newest are omitted and columns
        that are masked in every row are dropped (``labels`` names the rest).
        """
        n = max(1, int(n))
        with self._lock:
            rows = list(self._heat)[-n:]
        empty = {"ts": [], "rows": [], "labels": [], "n_subcarriers": 0, "chip_family": None,
                 "vmin": None, "vmax": None}
        if not rows:
            return empty
        layout = rows[-1][2]
        rows = [r for r in rows if r[2] == layout]
        M = np.vstack([r[1] for r in rows]).astype(float)
        keep = np.isfinite(M).any(axis=0)
        if not keep.any():
            return empty
        family, width = layout
        if family == FAMILY_CLASSIC and width == len(VALID_LLTF_INDICES):
            labels = [f"lltf_{i}" for i in VALID_LLTF_INDICES]
        else:
            labels = [f"sc_{i}" for i in range(width)]
        M = M[:, keep]
        labels = [lab for lab, k in zip(labels, keep) if k]
        vmin, vmax = np.nanpercentile(M, [2, 98])
        return json_safe({
            "ts": [r[0] for r in rows],
            "rows": [_round_row(row) for row in M],
            "labels": labels,
            "n_subcarriers": len(labels),
            "chip_family": family,
            "vmin": float(vmin),
            "vmax": float(vmax),
        })

    def series(self, seconds: float = 30.0) -> dict:
        """Frame time series for the last ``seconds`` (of packet time)."""
        seconds = max(1.0, float(seconds))
        with self._lock:
            frames = list(self._frames)
        out = {"seconds": seconds, "t_end": None, "ts": [], "motion": [], "variance": [], "diff_energy": [],
               "baseline_deviation": [], "mean_amplitude": [], "rssi": [], "packet_rate": [],
               "quality_ok": [], "activity": [], "thresholds": self.config.thresholds}
        if frames:
            t_end = frames[-1][0].ts
            out["t_end"] = t_end
            for f, act in frames:
                if f.ts < t_end - seconds:
                    continue
                out["ts"].append(f.ts)
                out["motion"].append(f.motion_score)
                out["variance"].append(f.variance)
                out["diff_energy"].append(f.diff_energy)
                out["baseline_deviation"].append(f.baseline_deviation)
                out["mean_amplitude"].append(f.mean_amplitude)
                out["rssi"].append(f.rssi)
                out["packet_rate"].append(f.packet_rate)
                out["quality_ok"].append(f.quality_ok)
                out["activity"].append(act)
        return json_safe(out)

    def add_frame_hook(self, hook: Callable) -> None:
        self.frame_hooks.append(hook)
