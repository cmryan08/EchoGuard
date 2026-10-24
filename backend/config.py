"""Application configuration: dataclasses with tolerant JSON load/save.

The file lives at ``data/config.json`` and is created with defaults on first
use. Unknown keys are ignored and missing keys fall back to their defaults, so
older or hand-edited files keep working. Standard library only (the optional
ML tooling imports this module too).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_CONFIG_PATH = DATA_DIR / "config.json"


@dataclass
class ProcessingConfig:
    """Signal processing parameters (see :mod:`backend.processing`)."""

    #: Sliding analysis window, seconds of host time.
    window_s: float = 1.0
    #: Hampel filter history length (packets) and threshold (scaled MADs).
    hampel_window: int = 7
    hampel_k: float = 3.0
    #: EMA smoothing factor per packet (1.0 = no smoothing).
    ema_alpha: float = 0.3
    #: Time constant of the slow adaptive reference profile used when no
    #: calibration baseline is set.
    reference_tau_s: float = 30.0
    #: Frames (motion feature snapshots) emitted per second of packet time.
    frame_rate_hz: float = 10.0
    #: Data-quality gates: below these a frame is flagged insufficient.
    min_packet_rate_hz: float = 20.0
    min_window_fill: float = 0.7
    stale_after_s: float = 1.0
    #: Noise references used to normalise the motion score while uncalibrated.
    #: Calibration (Milestone 3) replaces them with measured empty-room values.
    uncalibrated_variance_ref: float = 0.03
    uncalibrated_diff_ref: float = 0.01


@dataclass
class MotionThresholds:
    """Motion score thresholds. Calibrated empty room scores about 1.0.

    * score < ``movement``          -> empty room / low activity
    * ``movement`` <= score < ``rapid`` -> movement detected
    * score >= ``rapid``            -> rapid movement
    * ``low`` marks "still" (used later for post-event stillness checks)

    The defaults are uncalibrated guesses, not measured values.
    """

    low: float = 1.5
    movement: float = 2.5
    rapid: float = 6.0
    calibrated: bool = False


@dataclass
class FallConfig:
    """Explainable potential-fall heuristic (see :mod:`backend.detector`).

    A potential fall needs (1) a sudden motion spike, (2) post-event stillness
    that starts soon after the spike and lasts long enough, and (3) sufficient
    data quality over the whole evaluation window. Stillness alone is never a
    fall. Values of 0 for the two score thresholds mean "derive from the
    active motion thresholds" (calibrated when a calibration is loaded).
    """

    enabled: bool = True
    #: Spike: peak motion score must reach this (0 = use MotionThresholds.rapid).
    spike_threshold: float = 0.0
    #: Spike: the score must rise by at least ``min_rise_fraction * spike_threshold``
    #: within ``rise_window_s`` (a *sudden* increase, not sustained activity).
    rise_window_s: float = 1.5
    min_rise_fraction: float = 0.5
    #: Stillness: motion score at or below this (0 = use MotionThresholds.low) ...
    stillness_threshold: float = 0.0
    #: ... continuously for at least this long ...
    stillness_duration_s: float = 3.0
    #: ... starting no later than this after the last spike frame.
    max_gap_s: float = 2.0
    #: Quality: every frame in the evaluation window must be quality-ok and at
    #: least this packet rate.
    min_packet_rate_hz: float = 20.0
    #: After a detection, ignore new spikes for this long (seconds of data time).
    refractory_s: float = 30.0


@dataclass
class AlertConfig:
    """Alert handling (see :mod:`backend.alerts`)."""

    #: Countdown before the *simulated* caregiver notification.
    countdown_s: float = 30.0
    #: Events kept in memory for the dashboard (the JSONL file is append-only).
    event_log_max: int = 500
    #: An activity change is logged only after the new state held this long.
    activity_min_s: float = 1.0


@dataclass
class PostureConfig:
    """Sitting / standing tracking (see :mod:`backend.posture` and :mod:`backend.sitting`).

    Upright time = standing + moving (like Apple Watch stand hours, moving
    counts as standing). Valid ranges are in :data:`POSTURE_RANGES`.
    """

    #: Seconds of smoothed amplitude profiles averaged before classifying.
    posture_window_s: float = 3.0
    #: A still posture must have at least this probability ...
    min_confidence: float = 0.6
    #: ... for this long before the state changes ...
    hold_s: float = 5.0
    #: ... or this long when a movement burst ended within the last 5 s
    #: (sitting down / standing up produce one).
    transition_confirm_s: float = 2.0
    #: Unknown / no-data gaps shorter than this do not break a sitting streak.
    unknown_grace_s: float = 10.0
    #: Being away (empty room) longer than this ends a sitting streak.
    away_end_streak_s: float = 120.0
    #: Stand reminder once a sitting streak reaches this many minutes.
    reminder_after_min: float = 30.0
    #: Continuous upright time that counts as a stand break (ends the streak,
    #: clears the reminder).
    stand_break_s: float = 60.0
    #: Daily goal of stand hours.
    stand_goal_hours: int = 12
    #: An hour counts as a stand hour with at least this much upright time.
    stand_hour_min_s: float = 60.0


#: PostureConfig field -> (min, max), inclusive. Out-of-range values in the
#: config file are clamped; the API rejects them.
POSTURE_RANGES = {
    "posture_window_s": (1.0, 10.0),
    "min_confidence": (0.5, 0.99),
    "hold_s": (1.0, 60.0),
    "transition_confirm_s": (0.5, 30.0),
    "unknown_grace_s": (0.0, 300.0),
    "away_end_streak_s": (10.0, 3600.0),
    "reminder_after_min": (1.0, 180.0),
    "stand_break_s": (10.0, 600.0),
    "stand_goal_hours": (1, 24),
    "stand_hour_min_s": (10.0, 1800.0),
}


def _clamp_posture(cfg: PostureConfig) -> PostureConfig:
    for name, (lo, hi) in POSTURE_RANGES.items():
        v = getattr(cfg, name)
        if v != v:   # NaN
            v = getattr(PostureConfig(), name)
        setattr(cfg, name, type(lo)(min(hi, max(lo, v))))
    return cfg


@dataclass
class AppConfig:
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    thresholds: MotionThresholds = field(default_factory=MotionThresholds)
    fall: FallConfig = field(default_factory=FallConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    posture: PostureConfig = field(default_factory=PostureConfig)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AppConfig":
        data = data if isinstance(data, dict) else {}
        return cls(
            processing=_build(ProcessingConfig, data.get("processing")),
            thresholds=_build(MotionThresholds, data.get("thresholds")),
            fall=_build(FallConfig, data.get("fall")),
            alerts=_build(AlertConfig, data.get("alerts")),
            posture=_clamp_posture(_build(PostureConfig, data.get("posture"))),
        )

    def save(self, path=DEFAULT_CONFIG_PATH) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path=DEFAULT_CONFIG_PATH) -> "AppConfig":
        """Load ``path``; raises ``FileNotFoundError`` / ``ValueError`` on problems."""
        text = Path(path).read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}: invalid JSON ({e})") from None
        return cls.from_dict(data)

    @classmethod
    def load_or_create(cls, path=DEFAULT_CONFIG_PATH) -> "AppConfig":
        """Load ``path``, creating it with defaults if it does not exist."""
        path = Path(path)
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        return cls.load(path)


def _build(dc_type, data: Any):
    """Instantiate ``dc_type`` from a dict, ignoring unknown keys and keeping
    defaults for missing or wrongly typed values."""
    obj = dc_type()
    if not isinstance(data, dict):
        return obj
    for f in fields(dc_type):
        if f.name not in data:
            continue
        default = getattr(obj, f.name)
        value = data[f.name]
        try:
            if isinstance(default, bool):
                if isinstance(value, bool):
                    setattr(obj, f.name, value)
            elif isinstance(default, int):
                setattr(obj, f.name, int(value))
            elif isinstance(default, float):
                setattr(obj, f.name, float(value))
            else:
                setattr(obj, f.name, value)
        except (TypeError, ValueError):
            pass
    return obj
