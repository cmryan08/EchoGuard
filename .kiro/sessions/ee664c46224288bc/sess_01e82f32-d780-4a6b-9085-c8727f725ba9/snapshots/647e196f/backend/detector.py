"""Explainable heuristic potential-fall detector (Milestone 4).

The detector consumes :class:`backend.processing.MotionFrame` objects (about
10 per second) and looks for this pattern, using data (packet) time only:

1. **Sudden motion spike**: a quality-ok frame whose motion score reaches the
   spike threshold *and* rose by at least ``min_rise_fraction * spike`` within
   the preceding ``rise_window_s`` seconds (sustained vigorous activity that
   was already high is not "sudden").
2. **Post-event stillness**: after the last spike frame, the motion score
   drops to the stillness threshold or below no later than ``max_gap_s`` and
   then stays there for ``stillness_duration_s``.
3. **Data quality**: every frame from the start of the rise window until the
   decision is quality-ok (no "unknown" frames) with at least
   ``min_packet_rate_hz``. Any bad frame aborts the candidate.

Stillness is only evaluated after a spike: low movement alone never produces
an event (an empty room or a person sitting still is not a fall). After a
detection a refractory period suppresses further events.

Every :class:`FallEvent` lists all criteria with value, threshold and
pass/fail so the dashboard can show exactly why the alert fired. This is a
configurable baseline heuristic, not a validated fall detector: a rapid sit
followed by sitting still can trigger it, and a real fall with continued
movement may not.
"""

from __future__ import annotations

import itertools
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Optional

from .config import FallConfig, MotionThresholds

#: Detector states reported in :meth:`FallDetector.status`.
ST_DISABLED = "disabled"
ST_MONITORING = "monitoring"
ST_SPIKE = "spike"                    # spike seen, motion still above stillness level
ST_STILLNESS = "stillness"            # post-spike stillness run in progress
ST_REFRACTORY = "refractory"

STATE_TEXT = {
    ST_DISABLED: "Fall detection disabled",
    ST_MONITORING: "Monitoring for a sudden motion spike",
    ST_SPIKE: "Motion spike detected - waiting for stillness",
    ST_STILLNESS: "Spike followed by stillness - timing stillness",
    ST_REFRACTORY: "Refractory period after a detection",
}

_EPS = 1e-6
_ids = itertools.count(1)


@dataclass
class Criterion:
    """One explainable check. ``comparison`` is '>=' or '<='."""

    name: str
    description: str
    value: Optional[float]
    threshold: float
    comparison: str
    passed: bool
    unit: str = ""


@dataclass
class FallEvent:
    event_id: str
    ts: float                    # decision time (data time of the frame)
    spike_start_ts: float
    spike_peak_ts: float
    spike_end_ts: float
    stillness_start_ts: float
    peak_score: float
    criteria: list
    thresholds: dict
    calibrated: bool
    frames_evaluated: int
    source_kind: Optional[str] = None
    synthetic: bool = False
    wall_time: float = field(default_factory=time.time)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.criteria)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["passed"] = self.passed
        d["summary"] = summarize(self)
        return d


def summarize(ev: FallEvent) -> str:
    th = ev.thresholds
    still = ev.ts - ev.stillness_start_ts
    return (f"motion spike {ev.peak_score:.2f} (>= {th['spike']:.2f}) followed by {still:.1f} s of stillness "
            f"(score <= {th['stillness']:.2f}) starting {ev.stillness_start_ts - ev.spike_end_ts:.1f} s after the spike")


def effective_thresholds(cfg: FallConfig, thresholds: MotionThresholds) -> dict:
    """Spike / stillness / rise thresholds actually used (0 in the config = derive
    from the active motion thresholds)."""
    spike = cfg.spike_threshold if cfg.spike_threshold > 0 else thresholds.rapid
    still = cfg.stillness_threshold if cfg.stillness_threshold > 0 else thresholds.low
    return {
        "spike": float(spike),
        "stillness": float(still),
        "min_rise": float(max(0.0, cfg.min_rise_fraction) * spike),
        "spike_source": "config" if cfg.spike_threshold > 0 else "thresholds.rapid",
        "stillness_source": "config" if cfg.stillness_threshold > 0 else "thresholds.low",
        "calibrated": bool(thresholds.calibrated),
    }


def _num(x) -> Optional[float]:
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


class FallDetector:
    """Streaming state machine. Feed frames from one thread (not thread-safe)."""

    def __init__(self, config: Optional[FallConfig] = None) -> None:
        self.config = config or FallConfig()
        self.events_detected = 0
        self.last_event: Optional[FallEvent] = None
        self.last_rejection: Optional[dict] = None
        self.reset()

    def reset(self) -> None:
        """Forget history, any candidate and the refractory period (new source)."""
        self._hist: deque = deque()          # (ts, score, ok)
        self._cand: Optional[dict] = None
        self._refractory_until: Optional[float] = None
        self._last_ts: Optional[float] = None
        self._th: Optional[dict] = None

    # ------------------------------------------------------------ helpers
    def _frame_ok(self, frame) -> bool:
        score = _num(frame.motion_score)
        rate = _num(frame.packet_rate)
        return bool(frame.quality_ok) and score is not None and rate is not None \
            and rate >= self.config.min_packet_rate_hz

    def _reject(self, ts: float, reason: str, **extra) -> None:
        self.last_rejection = {"ts": ts, "reason": reason, **extra}
        self._cand = None

    # -------------------------------------------------------------- update
    def update(self, frame, thresholds: Optional[MotionThresholds] = None) -> Optional[FallEvent]:
        """Consume one frame; returns a :class:`FallEvent` when all criteria pass."""
        cfg = self.config
        th = effective_thresholds(cfg, thresholds or MotionThresholds())
        self._th = th
        ts = float(frame.ts)
        if self._last_ts is not None and ts < self._last_ts:
            self.reset()                     # time went backwards: a different stream
            self._th = th
        self._last_ts = ts
        ok = self._frame_ok(frame)
        score = _num(frame.motion_score)
        rate = _num(frame.packet_rate)

        # rise-window history (frames strictly before this one)
        prior = [h for h in self._hist if ts - cfg.rise_window_s - _EPS <= h[0] < ts]
        self._hist.append((ts, score, ok, rate))
        keep_from = ts - max(cfg.rise_window_s, 0.0) - 1.0
        while self._hist and self._hist[0][0] < keep_from:
            self._hist.popleft()

        if not cfg.enabled:
            self._cand = None
            return None

        cand = self._cand
        if cand is None:
            if self._refractory_until is not None and ts < self._refractory_until:
                return None
            if not (ok and score is not None and score >= th["spike"]):
                return None
            if not prior:
                self._reject(ts, "spike without history: cannot verify a sudden rise", score=score)
                return None
            if not all(h[2] for h in prior):
                self._reject(ts, "insufficient data quality before the spike", score=score)
                return None
            base = min(h[1] for h in prior)
            rise = score - base
            if rise < th["min_rise"]:
                self._reject(ts, f"not sudden: rise {rise:.2f} < {th['min_rise']:.2f} within "
                                 f"{cfg.rise_window_s:g} s", score=score)
                return None
            self._cand = {
                "eval_start": prior[0][0], "spike_start": ts, "peak": score, "peak_ts": ts, "rise": rise,
                "rise_base": base, "last_spike": ts, "still_start": None, "still_max": None,
                "frames": len(prior) + 1, "min_rate": min([rate] + [h[3] for h in prior]),
            }
            return None

        # ----- candidate in progress
        cand["frames"] += 1
        if not ok:
            reasons = list(getattr(frame, "quality_reasons", []) or [])
            if rate is not None and rate < cfg.min_packet_rate_hz:
                reasons.append(f"packet rate {rate:.1f} < {cfg.min_packet_rate_hz:g} Hz")
            self._reject(ts, "insufficient data quality during the evaluation window", details=reasons)
            return None
        cand["min_rate"] = min(cand["min_rate"], rate)
        if score >= th["spike"]:
            if score > cand["peak"]:
                cand["peak"], cand["peak_ts"] = score, ts
            cand["last_spike"] = ts
            cand["still_start"] = cand["still_max"] = None
            return None
        gap = ts - cand["last_spike"]
        if score <= th["stillness"]:
            if cand["still_start"] is None:
                if gap > cfg.max_gap_s + _EPS:
                    self._reject(ts, f"stillness started {gap:.1f} s after the spike (> {cfg.max_gap_s:g} s)")
                    return None
                cand["still_start"], cand["still_max"] = ts, score
            else:
                cand["still_max"] = max(cand["still_max"], score)
            if ts - cand["still_start"] + _EPS >= cfg.stillness_duration_s:
                return self._trigger(ts, cand, th)
            return None
        # between stillness and spike level: stillness (if any) interrupted
        cand["still_start"] = cand["still_max"] = None
        if gap > cfg.max_gap_s + _EPS:
            self._reject(ts, f"no stillness within {cfg.max_gap_s:g} s after the spike (motion continued)")
        return None

    def _trigger(self, ts: float, cand: dict, th: dict) -> FallEvent:
        cfg = self.config
        still_s = ts - cand["still_start"]
        onset = cand["still_start"] - cand["last_spike"]
        criteria = [
            Criterion("motion_spike", "Sudden increase: peak motion score reached the spike threshold",
                      round(cand["peak"], 4), round(th["spike"], 4), ">=", cand["peak"] >= th["spike"]),
            Criterion("sudden_rise", f"Score rise within {cfg.rise_window_s:g} s before the spike",
                      round(cand["rise"], 4), round(th["min_rise"], 4), ">=", cand["rise"] >= th["min_rise"]),
            Criterion("stillness_onset", "Stillness began soon after the last spike frame",
                      round(onset, 3), float(cfg.max_gap_s), "<=", onset <= cfg.max_gap_s + _EPS, "s"),
            Criterion("stillness_level", "Highest motion score during the stillness period",
                      round(cand["still_max"], 4), round(th["stillness"], 4), "<=",
                      cand["still_max"] <= th["stillness"]),
            Criterion("stillness_duration", "Continuous post-event stillness",
                      round(still_s, 3), float(cfg.stillness_duration_s), ">=",
                      still_s + _EPS >= cfg.stillness_duration_s, "s"),
            Criterion("data_quality", "Fraction of quality-ok frames in the evaluation window",
                      1.0, 1.0, ">=", True),
            Criterion("packet_rate", "Lowest packet rate in the evaluation window",
                      round(cand["min_rate"], 2), float(cfg.min_packet_rate_hz), ">=",
                      cand["min_rate"] >= cfg.min_packet_rate_hz, "Hz"),
        ]
        ev = FallEvent(
            event_id=f"fall-{int(time.time())}-{next(_ids)}", ts=ts, spike_start_ts=cand["spike_start"],
            spike_peak_ts=cand["peak_ts"], spike_end_ts=cand["last_spike"], stillness_start_ts=cand["still_start"],
            peak_score=round(cand["peak"], 4), criteria=criteria,
            thresholds={k: th[k] for k in ("spike", "stillness", "min_rise", "spike_source", "stillness_source")}
            | {"rise_window_s": cfg.rise_window_s, "max_gap_s": cfg.max_gap_s,
               "stillness_duration_s": cfg.stillness_duration_s, "min_packet_rate_hz": cfg.min_packet_rate_hz},
            calibrated=th["calibrated"], frames_evaluated=cand["frames"],
        )
        self._cand = None
        self._refractory_until = ts + max(0.0, cfg.refractory_s)
        self.events_detected += 1
        self.last_event = ev
        return ev

    # -------------------------------------------------------------- status
    def state(self) -> str:
        if not self.config.enabled:
            return ST_DISABLED
        c = self._cand
        if c is not None:
            return ST_STILLNESS if c["still_start"] is not None else ST_SPIKE
        if self._refractory_until is not None and self._last_ts is not None and self._last_ts < self._refractory_until:
            return ST_REFRACTORY
        return ST_MONITORING

    def status(self) -> dict:
        cfg = self.config
        st = self.state()
        c = self._cand
        cand = None
        if c is not None and self._last_ts is not None:
            cand = {
                "peak_score": c["peak"], "rise": c["rise"], "spike_start_ts": c["spike_start"],
                "since_spike_s": round(self._last_ts - c["last_spike"], 2),
                "stillness_elapsed_s": round(self._last_ts - c["still_start"], 2) if c["still_start"] is not None else None,
                "stillness_needed_s": cfg.stillness_duration_s, "max_gap_s": cfg.max_gap_s,
            }
        refr = None
        if st == ST_REFRACTORY:
            refr = round(self._refractory_until - self._last_ts, 1)
        return {
            "state": st,
            "state_text": STATE_TEXT[st],
            "enabled": cfg.enabled,
            "thresholds": self._th,
            "config": asdict(cfg),
            "candidate": cand,
            "refractory_remaining_s": refr,
            "last_rejection": self.last_rejection,
            "events_detected": self.events_detected,
            "last_event": self.last_event.to_dict() if self.last_event is not None else None,
        }
