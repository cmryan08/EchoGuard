"""Streaming CSI signal processing and motion features.

Per packet (``SignalProcessor.process``):

1. take the valid subcarrier amplitudes (:func:`valid_subcarrier_amplitudes`;
   52 LLTF subcarriers on classic chips, NaN where masked; on C5/C6 columns
   that have never been non-zero, i.e. guard/null subcarriers, are dropped by
   masking them as NaN),
2. per-packet mean normalisation (``amp / mean(amp)``) to suppress AGC / gain
   jumps that scale all subcarriers together (the raw mean is kept as
   ``mean_amplitude``),
3. Hampel outlier rejection per subcarrier over the last ``hampel_window``
   packets (a value further than ``hampel_k`` scaled MADs from the median is
   replaced by the median),
4. EMA smoothing (``ema_alpha``).

Over a sliding window of ``window_s`` seconds (host timestamps) it computes:

* ``variance``: mean over subcarriers of the temporal std of the smoothed,
  normalised amplitude,
* ``diff_energy``: RMS of the first difference over time (temporal feature),
* ``baseline_deviation``: mean ``|profile - baseline_mean| / baseline_std``
  over subcarriers, using the calibration baseline when one is set, otherwise
  a slow adaptive EMA reference profile (``reference_tau_s``),
* ``motion_score``: ``0.5 * (variance / var_ref + diff_energy / diff_ref)``
  where the references are the calibrated empty-room noise levels (so an empty
  room scores about 1.0) or, uncalibrated, ``uncalibrated_*_ref`` defaults.

Frames (:class:`MotionFrame`) are emitted at ``frame_rate_hz`` of packet time,
not per packet. Each frame carries a data-quality verdict with reasons. Only
numpy is used so the module can be reused by offline training tools.
"""

from __future__ import annotations

import math
import time
import warnings
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np

from .config import ProcessingConfig
from .csi_parser import FAMILY_C5C6, CSIPacket, valid_subcarrier_amplitudes

#: Minimum std used when dividing by a baseline / reference std (normalised units).
STD_FLOOR = 1e-3
#: Minimum Hampel scale so a perfectly constant history does not flag tiny noise.
HAMPEL_FLOOR = 1e-3
#: MAD -> std for Gaussian data.
MAD_SCALE = 1.4826

# Quality reason codes (each reason string starts with one of these).
Q_NO_DATA = "no_data"
Q_LOW_RATE = "low_packet_rate"
Q_LOW_FILL = "low_window_fill"
Q_LAYOUT = "layout_changed"
Q_STALE = "stale"
Q_FEW_PACKETS = "insufficient_packets"


@dataclass
class Baseline:
    """Empty-room reference (set by calibration in Milestone 3)."""

    mean_profile: np.ndarray
    std_profile: np.ndarray
    noise_variance: Optional[float] = None
    noise_diff: Optional[float] = None
    name: str = ""


@dataclass
class MotionFrame:
    ts: float
    motion_score: Optional[float]
    variance: Optional[float]
    diff_energy: Optional[float]
    baseline_deviation: Optional[float]
    mean_amplitude: Optional[float]
    rssi: Optional[float]
    packet_rate: Optional[float]
    quality_ok: bool
    quality_reasons: list = field(default_factory=list)
    n_packets: int = 0
    window_fill: float = 0.0
    n_subcarriers: int = 0
    calibrated: bool = False
    baseline_source: str = "adaptive"

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
        return d


@dataclass
class _Entry:
    ts: float
    smoothed: np.ndarray
    mean_amp: float
    rssi: float


def _finite_or_none(x) -> Optional[float]:
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


class SignalProcessor:
    """Streaming processor. Not thread-safe; feed it from a single thread."""

    def __init__(self, config: Optional[ProcessingConfig] = None) -> None:
        self.config = config or ProcessingConfig()
        self.baseline: Optional[Baseline] = None
        self.layout: Optional[tuple] = None
        self.layout_changes = 0
        self.invalid_packets = 0
        self.outliers_replaced = 0
        self.packets_processed = 0
        self.last_profile: Optional[np.ndarray] = None   # normalised, Hampel-filtered
        self.last_smoothed: Optional[np.ndarray] = None  # + EMA
        self.last_raw: Optional[np.ndarray] = None       # raw amplitudes (masked)
        self.last_frame: Optional[MotionFrame] = None
        self._layout_change_ts: Optional[float] = None
        self._last_frame_ts: Optional[float] = None
        self._reset_history()

    # ----------------------------------------------------------- baseline
    def set_baseline(self, mean_profile, std_profile, noise_variance: Optional[float] = None,
                     noise_diff: Optional[float] = None, name: str = "") -> None:
        mean_profile = np.asarray(mean_profile, dtype=float)
        std_profile = np.asarray(std_profile, dtype=float)
        if mean_profile.shape != std_profile.shape or mean_profile.ndim != 1:
            raise ValueError("baseline mean and std profiles must be 1-D arrays of equal length")
        self.baseline = Baseline(mean_profile, std_profile, noise_variance, noise_diff, name)

    def clear_baseline(self) -> None:
        self.baseline = None

    @property
    def baseline_active(self) -> bool:
        """True when a baseline is set and matches the current subcarrier layout."""
        b = self.baseline
        return b is not None and self.layout is not None and b.mean_profile.size == self.layout[1]

    # -------------------------------------------------------------- state
    def _reset_history(self) -> None:
        self._hist: Optional[np.ndarray] = None
        self._hist_idx = 0
        self._ema: Optional[np.ndarray] = None
        self._ref_mean: Optional[np.ndarray] = None
        self._ref_var: Optional[np.ndarray] = None
        self._active: Optional[np.ndarray] = None
        self._window: deque = deque()
        self._start_ts: Optional[float] = None
        self._last_ts: Optional[float] = None

    def reset(self) -> None:
        """Forget all history (keeps config and baseline)."""
        self._reset_history()
        self.layout = None
        self._layout_change_ts = None
        self._last_frame_ts = None
        self.last_profile = self.last_smoothed = self.last_raw = None
        self.last_frame = None

    # ----------------------------------------------------------- per packet
    def process(self, pkt: CSIPacket) -> Optional[MotionFrame]:
        """Consume one packet. Returns a :class:`MotionFrame` when one is due."""
        ts = float(pkt.host_ts) if pkt.host_ts is not None else time.time()
        amps = np.asarray(valid_subcarrier_amplitudes(pkt), dtype=float)
        layout = (pkt.chip_family, int(amps.size))

        if layout != self.layout:
            if self.layout is not None:
                self.layout_changes += 1
                self._layout_change_ts = ts
            self._reset_history()
            self.layout = layout
        elif self._last_ts is not None and ts < self._last_ts:
            # host time went backwards (e.g. a different replay): start over
            self._reset_history()
        if self._start_ts is None:
            self._start_ts = ts
        self._last_ts = ts

        if pkt.chip_family == FAMILY_C5C6:
            nonzero = np.nan_to_num(amps, nan=0.0) > 0
            self._active = nonzero if self._active is None else (self._active | nonzero)
            amps = np.where(self._active, amps, np.nan)

        finite = np.isfinite(amps)
        mean_amp = float(np.mean(amps[finite])) if finite.any() else float("nan")
        if not math.isfinite(mean_amp) or mean_amp <= 0:
            self.invalid_packets += 1
            return self._maybe_emit(ts)

        self.packets_processed += 1
        norm = amps / mean_amp
        filt = self._hampel(norm)
        smooth = self._smooth(filt)
        self._update_reference(smooth, ts)
        self.last_raw, self.last_profile, self.last_smoothed = amps, filt, smooth

        self._window.append(_Entry(ts, smooth, mean_amp, float(pkt.rssi)))
        cutoff = ts - self.config.window_s
        while self._window and self._window[0].ts < cutoff:
            self._window.popleft()
        return self._maybe_emit(ts)

    def _hampel(self, x: np.ndarray) -> np.ndarray:
        w = max(1, int(self.config.hampel_window))
        if self._hist is None:
            self._hist = np.full((w, x.size), np.nan)
            self._hist_idx = 0
        self._hist[self._hist_idx % w] = x
        self._hist_idx += 1
        if w < 3:
            return x.copy()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(self._hist, axis=0)
            mad = np.nanmedian(np.abs(self._hist - med), axis=0)
        count = np.isfinite(self._hist).sum(axis=0)
        scale = np.maximum(MAD_SCALE * np.nan_to_num(mad, nan=0.0), HAMPEL_FLOOR)
        outlier = (count >= 3) & np.isfinite(x) & (np.abs(x - med) > self.config.hampel_k * scale)
        n_out = int(outlier.sum())
        if n_out:
            self.outliers_replaced += n_out
            return np.where(outlier, med, x)
        return x.copy()

    def _smooth(self, x: np.ndarray) -> np.ndarray:
        a = float(self.config.ema_alpha)
        if self._ema is None:
            self._ema = x.copy()
        else:
            prev = self._ema
            blended = np.where(np.isfinite(prev), a * x + (1.0 - a) * prev, x)
            self._ema = np.where(np.isfinite(x), blended, prev)
        return self._ema.copy()

    def _update_reference(self, x: np.ndarray, ts: float) -> None:
        if self._ref_mean is None:
            self._ref_mean = x.copy()
            self._ref_var = np.where(np.isfinite(x), 0.0, np.nan)
            self._ref_ts = ts
            return
        dt = max(0.0, ts - self._ref_ts)
        self._ref_ts = ts
        tau = max(1e-3, float(self.config.reference_tau_s))
        a = 1.0 - math.exp(-dt / tau) if dt > 0 else 1.0 / 1000.0
        fin = np.isfinite(x)
        init = fin & ~np.isfinite(self._ref_mean)
        upd = fin & ~init
        delta = np.where(upd, x - self._ref_mean, 0.0)
        self._ref_mean = np.where(init, x, self._ref_mean + a * delta)
        self._ref_var = np.where(init, 0.0, np.where(upd, (1.0 - a) * (self._ref_var + a * delta ** 2),
                                                     self._ref_var))

    def _maybe_emit(self, ts: float) -> Optional[MotionFrame]:
        period = 1.0 / self.config.frame_rate_hz if self.config.frame_rate_hz > 0 else 0.0
        if self._last_frame_ts is not None and ts - self._last_frame_ts < period and ts >= self._last_frame_ts:
            return None
        self._last_frame_ts = ts
        frame = self.compute_frame(ts)
        self.last_frame = frame
        return frame

    # ------------------------------------------------------------- features
    def compute_frame(self, now: Optional[float] = None) -> MotionFrame:
        """Features and quality over the window ending at ``now`` (host time;
        default: the newest packet). Values are ``None`` when not computable."""
        cfg = self.config
        calibrated = self.baseline_active
        source = "calibration" if calibrated else "adaptive"
        n_sc = self.layout[1] if self.layout else 0
        if not self._window:
            t = now if now is not None else (self._last_ts if self._last_ts is not None else time.time())
            return MotionFrame(t, None, None, None, None, None, None, None, False, [f"{Q_NO_DATA}: no valid CSI packets"],
                               n_subcarriers=n_sc, calibrated=calibrated, baseline_source=source)

        t_last = self._window[-1].ts
        now = t_last if now is None else float(now)
        reasons = []
        age = now - t_last
        if age > cfg.stale_after_s:
            reasons.append(f"{Q_STALE}: no packets for {age:.1f} s")

        lo = now - cfg.window_s
        wins = [e for e in self._window if lo <= e.ts <= now]
        n = len(wins)
        effective = min(cfg.window_s, now - self._start_ts) if self._start_ts is not None else 0.0
        rate = n / effective if effective > 0 else None
        fill = (wins[-1].ts - wins[0].ts) / cfg.window_s if n >= 2 and cfg.window_s > 0 else 0.0
        fill = min(1.0, fill)

        if rate is None or rate < cfg.min_packet_rate_hz:
            shown = "n/a" if rate is None else f"{rate:.1f} Hz"
            reasons.append(f"{Q_LOW_RATE}: {shown} < {cfg.min_packet_rate_hz:g} Hz")
        if fill < cfg.min_window_fill:
            reasons.append(f"{Q_LOW_FILL}: {fill:.2f} < {cfg.min_window_fill:g}")
        if self._layout_change_ts is not None and lo <= self._layout_change_ts <= now:
            reasons.append(f"{Q_LAYOUT}: CSI subcarrier layout changed in this window")

        variance = diff_energy = deviation = score = None
        mean_amp = rssi = None
        if n:
            mean_amp = float(np.mean([e.mean_amp for e in wins]))
            rssi = float(np.mean([e.rssi for e in wins]))
        if n >= 3:
            X = np.vstack([e.smoothed for e in wins])
            cols = np.isfinite(X).sum(axis=0) >= 2
            if cols.any():
                Xc = X[:, cols]
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    variance = _finite_or_none(np.nanmean(np.nanstd(Xc, axis=0)))
                    d = np.diff(Xc, axis=0)
                    diff_energy = _finite_or_none(np.sqrt(np.nanmean(d ** 2))) if np.isfinite(d).any() else None
                    profile = np.nanmean(X, axis=0)
                    deviation = self._deviation(profile, cols)
        else:
            reasons.append(f"{Q_FEW_PACKETS}: {n} packets in window")

        if variance is not None and diff_energy is not None:
            var_ref, diff_ref = cfg.uncalibrated_variance_ref, cfg.uncalibrated_diff_ref
            if calibrated:
                b = self.baseline
                if b.noise_variance and b.noise_variance > 0:
                    var_ref = b.noise_variance
                if b.noise_diff and b.noise_diff > 0:
                    diff_ref = b.noise_diff
            score = 0.5 * (variance / max(var_ref, 1e-12) + diff_energy / max(diff_ref, 1e-12))

        return MotionFrame(
            ts=now, motion_score=_finite_or_none(score), variance=variance, diff_energy=diff_energy,
            baseline_deviation=deviation, mean_amplitude=_finite_or_none(mean_amp), rssi=_finite_or_none(rssi),
            packet_rate=_finite_or_none(rate), quality_ok=not reasons, quality_reasons=reasons, n_packets=n,
            window_fill=float(fill), n_subcarriers=n_sc, calibrated=calibrated, baseline_source=source,
        )

    def _deviation(self, profile: np.ndarray, cols: np.ndarray) -> Optional[float]:
        if self.baseline_active:
            ref_mean = self.baseline.mean_profile
            ref_std = self.baseline.std_profile
        elif self._ref_mean is not None:
            ref_mean = self._ref_mean
            ref_std = np.sqrt(np.maximum(np.nan_to_num(self._ref_var, nan=0.0), 0.0))
        else:
            return None
        ok = cols & np.isfinite(profile) & np.isfinite(ref_mean) & np.isfinite(ref_std)
        if not ok.any():
            return None
        z = np.abs(profile[ok] - ref_mean[ok]) / np.maximum(ref_std[ok], STD_FLOOR)
        return _finite_or_none(np.mean(z))

    def reference_profile(self) -> Optional[np.ndarray]:
        """Current adaptive reference profile (normalised amplitudes) or None."""
        return None if self._ref_mean is None else self._ref_mean.copy()
