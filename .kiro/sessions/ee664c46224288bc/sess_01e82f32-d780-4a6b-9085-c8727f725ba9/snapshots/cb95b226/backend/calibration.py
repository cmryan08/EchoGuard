"""Guided self-calibration: noise levels, baseline profile and motion thresholds.

Workflow (:class:`CalibrationSession`), each step recorded for a fixed span of
packet time while the engine runs:

1. ``empty_baseline`` - empty, stationary room (15-30 s, default 20 s)
2. ``walking``        - a person walks across the sensing area (default 15 s)
3. ``standing_still`` - a person stands still where they normally stand (default 20 s)
4. ``sitting``        - seated on the chair they normally use (default 20 s)
5. compute            - :func:`compute_calibration`

A run (:meth:`CalibrationSession.begin`) waits ``lead_in_s`` (time to leave the
room, default 5 s), then records; with ``auto`` the remaining steps follow by
themselves, with ``grace_s`` between the empty room and walking (time to come
back in, default 5 s) and ``transition_s`` between consecutive steps with a
person (stop walking / sit down, default 3 s). :func:`build_timeline` gives the
planned schedule (with speakable cues) shown and read aloud by the dashboard;
with the defaults and a 1 s settle time a full run takes 95 s.

Frames come from the engine thread (``Engine.add_frame_hook``) together with
``SignalProcessor.last_smoothed`` (the normalised, filtered amplitude profile
that the features and the baseline deviation use). Nothing is driven by the UI
refresh.

Computed values:

* empty-room baseline: per-subcarrier mean and std of the smoothed profile,
* noise levels: median / p95 of variance, diff energy, baseline deviation and
  motion score in the empty room; ``noise_variance`` / ``noise_diff`` (medians)
  become the motion-score normalisation, so a calibrated empty room scores ~1,
* per-step distributions (median, quartiles, p5/p95) of the recomputed
  calibrated motion score and other features, and per-step mean profiles,
* preliminary thresholds from the empty/still p95 and the walking median/p95
  (monotonic ``low < movement < rapid``; falls back to conservative defaults
  with a warning when walking is not clearly separable from stillness),
* a sitting / standing / empty posture model (:mod:`backend.posture`) with a
  within-calibration self-check (optimistic, not a validated accuracy).

Calibrations are persisted as ``data/calibration/<sensor_key>.json``, where the
key combines placement, chip family, TX MAC, channel, secondary channel and
CSI length. Calibrations computed from synthetic (simulated) data carry
``synthetic: true`` and a ``__synthetic`` key suffix, and are never applied to
real sources automatically (and not manually either).

These thresholds are preliminary heuristics, not validated detection limits.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from . import __version__
from . import posture
from .config import DATA_DIR, MotionThresholds
from .posture import json_list as _json_list
from .processing import STD_FLOOR
from .recording import iso, validate_placement, write_json_atomic

log = logging.getLogger(__name__)

DEFAULT_CALIBRATION_DIR = DATA_DIR / "calibration"
#: 2 adds ``posture_model``; format-1 files (no posture model) still load.
CALIBRATION_FORMAT = 2
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,159}$")
SYNTHETIC_SUFFIX = "__synthetic"


@dataclass(frozen=True)
class StepSpec:
    key: str
    title: str
    instructions: str
    default_s: float
    min_s: float
    max_s: float
    cue: str = ""                   # short speakable prompt when the step begins (read aloud by the UI)
    person_present: bool = True     # False: the room must be empty during the step
    prepare: str = ""               # what to do during the transition before the step


STEPS = [
    StepSpec("empty_baseline", "Empty room baseline",
             "Leave the room and keep everything still: no people, pets, fans or moving curtains between "
             "or near the two boards.", 20.0, 15.0, 30.0,
             cue="Recording the empty room. Stay outside.", person_present=False),
    StepSpec("walking", "Walking",
             "Walk back and forth across the room at a normal pace, crossing the line between the two boards.",
             15.0, 5.0, 60.0, cue="Come in and walk back and forth between the two boards.",
             prepare="Walk to the area between the two boards"),
    StepSpec("standing_still", "Standing still",
             "Stand still at the spot where you normally stand, for example at your standing desk. Breathe "
             "normally and keep your arms still.", 20.0, 5.0, 60.0,
             cue="Stand still where you normally stand.", prepare="Stop walking and stand still"),
    StepSpec("sitting", "Sitting",
             "Sit in the chair you normally use, at the spot where you usually work, and stay seated as you "
             "normally would.", 20.0, 5.0, 60.0,
             cue="Sit in the chair you normally use and stay seated.", prepare="Sit down on your chair now"),
]
STEP_KEYS = [s.key for s in STEPS]
STEP_BY_KEY = {s.key: s for s in STEPS}

#: Wait before the first recorded step of a run (time to leave the room).
MAX_LEAD_IN_S = 60.0
#: Wait between an empty-room step and the next step with a person (time to
#: come back in).
MAX_GRACE_S = 60.0
#: Wait between two consecutive steps with a person (stop walking and stand
#: still / walk to the chair and sit down).
MAX_TRANSITION_S = 30.0
#: Defaults for a one-click run from the dashboard.
DEFAULT_LEAD_IN_S = 5.0
DEFAULT_GRACE_S = 5.0
DEFAULT_TRANSITION_S = 3.0
#: Spoken after the last step of a run.
DONE_CUE = "Calibration recording finished. You can get up now."


def _seconds(value, name: str, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not (0 <= value <= hi):
        raise CalibrationError(f"{name} must be 0-{hi:g} s")
    return float(value)


def mmss(seconds: float) -> str:
    """``m:ss``-style offset with two-digit minutes, e.g. ``01:35``."""
    s = max(0, int(round(float(seconds))))
    return f"{s // 60:02d}:{s % 60:02d}"


def build_timeline(keys, durations: dict, lead_in_s: float = 0.0, grace_s: float = 0.0,
                   settle_s: float = 0.0, transition_s: float = 0.0) -> dict:
    """Planned wall-clock schedule for recording ``keys`` back to back.

    Phases, in order: an optional ``lead_in`` before the first step (time to
    leave the room), each ``step`` (``settle_s`` discarded while the analysis
    window clears, then the step duration), an optional ``grace`` period before
    a step with a person that follows an empty-room step (time to come back in)
    and an optional ``transition`` between two consecutive steps with a person
    (time to get into position). Each phase has ``kind``, ``key`` (the step it
    belongs to), ``title``, speakable ``cue``, human ``instruction``,
    ``start_s`` / ``end_s`` / ``length_s`` (seconds from pressing Start) and
    ``when`` / ``until`` (``mm:ss`` offsets).
    """
    phases = []
    t = 0.0

    def add(kind, key, title, cue, instruction, length):
        nonlocal t
        phases.append({"kind": kind, "key": key, "title": title, "cue": cue, "instruction": instruction,
                       "start_s": round(t, 3), "end_s": round(t + length, 3), "length_s": round(length, 3),
                       "when": mmss(t), "until": mmss(t + length)})
        t += length

    prev = None
    for i, key in enumerate(keys):
        spec = STEP_BY_KEY[key]
        dur = float(durations[key])
        if i == 0 and lead_in_s > 0:
            if not spec.person_present:
                add("lead_in", key, "Leave the room now",
                    f"Leave the room now. Recording the empty room starts in {lead_in_s:g} seconds.",
                    f"Leave the room within {lead_in_s:g} seconds of pressing Start and stay outside until "
                    "you hear 'Come back in'.", lead_in_s)
            else:
                add("lead_in", key, "Get ready",
                    f"Get ready. {spec.title} starts in {lead_in_s:g} seconds.",
                    f"{spec.prepare or 'Get into position'}. Recording starts {lead_in_s:g} seconds after "
                    "pressing Start.", lead_in_s)
        elif prev is not None and spec.person_present and not prev.person_present:
            if grace_s > 0:
                add("grace", key, "Come back in",
                    f"Come back in. {spec.title} starts in {grace_s:g} seconds.",
                    f"Come back into the room. You have {grace_s:g} seconds before {spec.title.lower()} "
                    "starts.", grace_s)
        elif prev is not None and spec.person_present and prev.person_present and transition_s > 0:
            prepare = spec.prepare or "Get into position"
            add("transition", key, "Get into position",
                f"{prepare}. Recording starts in {transition_s:g} seconds.",
                f"{prepare}. You have {transition_s:g} seconds before {spec.title.lower()} is recorded.",
                transition_s)
        add("step", key, spec.title, spec.cue,
            f"{spec.instructions} Recording takes {settle_s + dur:g} seconds.", settle_s + dur)
        prev = spec
    return {"phases": phases, "total_s": round(t, 3), "total": mmss(t), "settle_s": settle_s,
            "done_cue": DONE_CUE}


#: A step needs this fraction of frames with sufficient data quality ...
MIN_OK_FRACTION = 0.8
#: ... and at least this many good frames (or half the expected frames).
MIN_OK_FRAMES = 20
#: Walking median must exceed the quiet (empty/still) p95 by this factor.
SEPARATION_RATIO = 1.5
#: Minimum relative gaps enforced between thresholds.
MOVEMENT_OVER_LOW = 1.1
RAPID_OVER_MOVEMENT = 1.2
#: Floor for the noise references (normalised amplitude units).
NOISE_FLOOR = 1e-6
FEATURES = ("motion_score", "variance", "diff_energy", "baseline_deviation")


class CalibrationError(ValueError):
    """Invalid request, state, or data that cannot be calibrated (HTTP 400)."""


# ------------------------------------------------------------------ helpers
def _slug_part(value) -> str:
    s = re.sub(r"[^0-9a-z]", "", str(value).lower())
    return s or "na"


def sensor_part(sensor: Optional[dict], synthetic: bool = False) -> Optional[str]:
    """Placement-independent part of the calibration key (None if unknown)."""
    if not sensor or not sensor.get("chip_family"):
        return None
    sec = sensor.get("secondary_channel")
    part = (f"{_slug_part(sensor.get('chip_family'))}_{_slug_part(sensor.get('tx_mac') or 'unknown')}"
            f"_ch{_slug_part(sensor.get('channel'))}-{_slug_part(sec if sec is not None else 'na')}"
            f"_len{_slug_part(sensor.get('csi_len'))}")
    return part + (SYNTHETIC_SUFFIX if synthetic else "")


def sensor_key(sensor: dict, placement: str, synthetic: bool = False) -> str:
    """``<placement>__<chip>_<txmac>_ch<ch>-<sec>_len<len>[__synthetic]``."""
    part = sensor_part(sensor, synthetic)
    if part is None:
        raise CalibrationError("sensor configuration unknown (no CSI packets seen yet)")
    return f"{validate_placement(placement)}__{part}"


def validate_key(key) -> str:
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise CalibrationError("invalid calibration key")
    return key


def _stats(values) -> Optional[dict]:
    a = np.asarray([v for v in values if v is not None], dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return None
    p = np.percentile(a, [5, 25, 50, 75, 95])
    return {"n": int(a.size), "min": float(a.min()), "p05": float(p[0]), "p25": float(p[1]), "median": float(p[2]),
            "p75": float(p[3]), "p95": float(p[4]), "max": float(a.max()), "mean": float(a.mean())}


def _profile_stats(profiles: list):
    if not profiles:
        return None, None
    P = np.vstack(profiles).astype(float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(P, axis=0), np.nanstd(P, axis=0)


def _to_array(values) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in values], dtype=float)


# -------------------------------------------------------------- step data
@dataclass
class StepData:
    key: str
    duration_s: float
    status: str = "pending"            # pending | recording | done | failed
    failure: Optional[str] = None
    settle_s: float = 0.0              # frames in the first settle_s are skipped (window still holds the previous step)
    t0: Optional[float] = None
    t0_hint: Optional[float] = None    # packet time the step starts at when chained directly to the previous one
    t_last: Optional[float] = None     # packet time of the last frame seen during the step
    wall_start: Optional[float] = None
    total: int = 0
    ok: int = 0
    reasons: Counter = field(default_factory=Counter)
    features: dict = field(default_factory=lambda: {k: [] for k in FEATURES})
    profiles: list = field(default_factory=list)
    layout: Optional[tuple] = None
    sensor: Optional[dict] = None
    synthetic: Optional[bool] = None
    source_kind: Optional[str] = None
    problems: list = field(default_factory=list)
    last_quality: Optional[dict] = None

    @property
    def elapsed_s(self) -> float:
        if self.t0 is None or self.t_last is None:
            return 0.0
        return max(0.0, self.t_last - self.t0 - self.settle_s)

    def summary(self) -> dict:
        spec = STEP_BY_KEY[self.key]
        return {
            "key": self.key, "title": spec.title, "instructions": spec.instructions,
            "duration_s": self.duration_s, "status": self.status, "failure": self.failure,
            "elapsed_s": round(self.elapsed_s, 2), "frames": self.total, "ok_frames": self.ok,
            "ok_fraction": round(self.ok / self.total, 3) if self.total else None,
            "top_reasons": [{"reason": r, "count": c} for r, c in self.reasons.most_common(3)],
            "last_quality": self.last_quality,
        }


def step_problems(sd: StepData, frame_rate_hz: float = 10.0) -> list:
    """Reasons why a step's data is insufficient (empty list = OK)."""
    problems = list(sd.problems)
    if sd.total == 0:
        return problems + ["no motion frames were received (is a data source running?)"]
    frac = sd.ok / sd.total
    min_ok = max(MIN_OK_FRAMES, int(0.5 * sd.duration_s * max(frame_rate_hz, 1.0)))
    top = ", ".join(f"{r} x{c}" for r, c in sd.reasons.most_common(3))
    if frac < MIN_OK_FRACTION:
        problems.append(f"only {frac:.0%} of frames had sufficient data quality (need {MIN_OK_FRACTION:.0%})"
                        + (f"; main reasons: {top}" if top else ""))
    if sd.ok < min_ok:
        problems.append(f"only {sd.ok} frames with sufficient quality (need at least {min_ok})")
    if sd.elapsed_s < 0.9 * sd.duration_s:
        problems.append(f"covered {sd.elapsed_s:.1f} s of {sd.duration_s:g} s")
    if sd.key == "empty_baseline" and len(sd.profiles) < min_ok:
        problems.append("not enough amplitude profiles for the baseline")
    return problems


# --------------------------------------------------------------- compute
def build_posture_model(steps: dict, frame_rate_hz: float = 10.0):
    """``(model, None)`` or ``(None, reason)``: the sitting / standing / empty
    posture model from the recorded step profiles plus its self-check."""
    by_class = {cls: steps[key].profiles for key, cls in posture.STEP_CLASSES.items() if key in steps}
    try:
        model = posture.fit_posture_model(by_class, k=posture.DEFAULT_K)
    except ValueError as e:
        return None, str(e)
    model["self_check"] = posture.temporal_self_check(by_class, frame_rate_hz, k=posture.DEFAULT_K)
    model["note"] = ("Location-dependent reference profiles from one calibration run; the self-check is "
                     f"a {posture.SELF_CHECK_LABEL}. Not a medical measurement.")
    return model, None


def compute_calibration(steps: dict, placement: str, frame_rate_hz: float = 10.0,
                        defaults: Optional[MotionThresholds] = None, now: Optional[float] = None) -> dict:
    """Derive baseline, noise levels and thresholds from recorded steps.

    ``steps`` maps step keys to :class:`StepData`. Raises
    :class:`CalibrationError` (with every reason) when a step is missing or its
    data quality was insufficient.
    """
    defaults = defaults or MotionThresholds()
    problems = []
    for key in STEP_KEYS:
        sd = steps.get(key)
        if sd is None or sd.total == 0:
            problems.append(f"{STEP_BY_KEY[key].title}: not recorded")
            continue
        for p in step_problems(sd, frame_rate_hz):
            problems.append(f"{STEP_BY_KEY[key].title}: {p}")
    if problems:
        raise CalibrationError("cannot compute calibration: " + "; ".join(problems))

    empty = steps["empty_baseline"]
    layouts = {steps[k].layout for k in STEP_KEYS}
    if len(layouts) != 1:
        raise CalibrationError("cannot compute calibration: the CSI layout differs between steps")
    warnings_out = []

    ev = np.asarray(empty.features["variance"], dtype=float)
    ed = np.asarray(empty.features["diff_energy"], dtype=float)
    noise_variance = float(np.median(ev))
    noise_diff = float(np.median(ed))
    if noise_variance < NOISE_FLOOR or noise_diff < NOISE_FLOOR:
        warnings_out.append("empty-room noise is essentially zero; noise references were floored "
                            f"at {NOISE_FLOOR:g} (is the signal real and varying?)")
    noise_variance = max(noise_variance, NOISE_FLOOR)
    noise_diff = max(noise_diff, NOISE_FLOOR)

    base_mean, base_std = _profile_stats(empty.profiles)

    def deviation(profile):
        ok = np.isfinite(profile) & np.isfinite(base_mean) & np.isfinite(base_std)
        if not ok.any():
            return None
        return float(np.mean(np.abs(profile[ok] - base_mean[ok]) / np.maximum(base_std[ok], STD_FLOOR)))

    step_out = {}
    scores = {}
    for key in STEP_KEYS:
        sd = steps[key]
        v = np.asarray(sd.features["variance"], dtype=float)
        d = np.asarray(sd.features["diff_energy"], dtype=float)
        s = 0.5 * (v / noise_variance + d / noise_diff)
        scores[key] = s
        devs = [deviation(p) for p in sd.profiles]
        pm, ps = _profile_stats(sd.profiles)
        step_out[key] = {
            "title": STEP_BY_KEY[key].title,
            "duration_s": sd.duration_s,
            "elapsed_s": round(sd.elapsed_s, 2),
            "frames": sd.total,
            "ok_frames": sd.ok,
            "ok_fraction": round(sd.ok / sd.total, 3),
            "stats": {
                "motion_score": _stats(s.tolist()),
                "variance": _stats(sd.features["variance"]),
                "diff_energy": _stats(sd.features["diff_energy"]),
                "baseline_deviation": _stats(devs),
                "motion_score_uncalibrated": _stats(sd.features["motion_score"]),
            },
            "profile_mean": _json_list(pm) if pm is not None else [],
            "profile_std": _json_list(ps) if ps is not None else [],
        }

    q = lambda key, pct: float(np.percentile(scores[key], pct))  # noqa: E731
    quiet_p95 = max(q("empty_baseline", 95), q("standing_still", 95))
    walk_med = q("walking", 50)
    walk_p95 = q("walking", 95)
    separable = walk_med >= SEPARATION_RATIO * quiet_p95
    if separable:
        gap = walk_med - quiet_p95
        low = quiet_p95 + 0.25 * gap
        movement = quiet_p95 + 0.5 * gap
        rapid = max(1.5 * walk_p95, 2.0 * movement)
    else:
        warnings_out.append(
            f"walking is not clearly separable from the empty room / standing still (walking median "
            f"{walk_med:.2f} vs quiet p95 {quiet_p95:.2f}, need x{SEPARATION_RATIO:g}); using conservative "
            "default thresholds. Check the board placement and that the person crossed the line between "
            "the boards, then recalibrate.")
        low = max(defaults.low, 1.2 * quiet_p95)
        movement = max(defaults.movement, 1.5 * low)
        rapid = max(defaults.rapid, 2.0 * movement)
    if movement < MOVEMENT_OVER_LOW * low:
        movement = MOVEMENT_OVER_LOW * low
        warnings_out.append("movement threshold raised to keep it above the stillness threshold")
    if rapid < RAPID_OVER_MOVEMENT * movement:
        rapid = RAPID_OVER_MOVEMENT * movement
        warnings_out.append("rapid threshold raised to keep it above the movement threshold")
    if q("sitting", 50) >= rapid:
        warnings_out.append("ordinary sitting scored above the rapid-movement threshold; rapid movement may be "
                            "flagged during normal sitting")

    posture_model, posture_error = build_posture_model(steps, frame_rate_hz)
    if posture_model is None:
        warnings_out.append(f"no sitting/standing posture model: {posture_error}")
    else:
        warnings_out.extend(posture.quality_warnings(posture_model))

    now = time.time() if now is None else now
    noise = {f: step_out["empty_baseline"]["stats"][f] for f in ("variance", "diff_energy", "baseline_deviation")}
    noise["motion_score"] = step_out["empty_baseline"]["stats"]["motion_score"]
    family, width = empty.layout
    synthetic = any(bool(steps[k].synthetic) for k in STEP_KEYS)
    return {
        "format": CALIBRATION_FORMAT,
        "kind": "echoguard-calibration",
        "placement": validate_placement(placement),
        "created": iso(now),
        "created_unix": now,
        "app_version": __version__,
        "source_kind": empty.source_kind,
        "synthetic": synthetic,
        "sensor": empty.sensor,
        "durations": {k: steps[k].duration_s for k in STEP_KEYS},
        "baseline": {"chip_family": family, "n_subcarriers": width,
                     "mean_profile": _json_list(base_mean), "std_profile": _json_list(base_std)},
        "noise_variance": noise_variance,
        "noise_diff": noise_diff,
        "noise": noise,
        "thresholds": {"low": round(low, 4), "movement": round(movement, 4), "rapid": round(rapid, 4),
                       "calibrated": True},
        "separable": bool(separable),
        "quiet_p95": quiet_p95,
        "walking_median": walk_med,
        "walking_p95": walk_p95,
        "posture_model": posture_model,
        "warnings": warnings_out,
        "steps": step_out,
        "note": "Preliminary heuristic thresholds from one calibration run; not validated detection limits.",
    }


# --------------------------------------------------------------- session
class CalibrationSession:
    """State machine for one guided calibration. Not thread-safe by itself
    (the :class:`CalibrationManager` holds a lock around every call).

    :meth:`begin` starts a run: after ``lead_in_s`` (wall clock) the current
    step records; with ``auto`` the remaining steps follow on their own, with
    ``grace_s`` before the first step with a person after the empty room (time
    to come back in), ``transition_s`` between two steps with a person (time to
    get into position) and no pause otherwise. Frames that arrive while waiting
    are ignored. A failed step stops the run.
    """

    def __init__(self, placement: str, durations: Optional[dict] = None, frame_rate_hz: float = 10.0,
                 clock=time.time, defaults: Optional[MotionThresholds] = None, settle_s: float = 1.0,
                 lead_in_s: float = DEFAULT_LEAD_IN_S, grace_s: float = DEFAULT_GRACE_S, auto: bool = True,
                 transition_s: float = DEFAULT_TRANSITION_S) -> None:
        self.placement = validate_placement(placement)
        self.frame_rate_hz = float(frame_rate_hz)
        self.settle_s = max(0.0, float(settle_s))
        self.lead_in_s = _seconds(lead_in_s, "lead-in", MAX_LEAD_IN_S)
        self.grace_s = _seconds(grace_s, "grace period", MAX_GRACE_S)
        self.transition_s = _seconds(transition_s, "transition", MAX_TRANSITION_S)
        if not isinstance(auto, bool):
            raise CalibrationError("auto must be true or false")
        self.auto = auto
        self.clock = clock
        self.defaults = defaults
        durations = durations or {}
        if not isinstance(durations, dict):
            raise CalibrationError("durations must be an object")
        unknown = set(durations) - set(STEP_KEYS)
        if unknown:
            raise CalibrationError(f"unknown calibration steps: {', '.join(sorted(unknown))}")
        self.steps = {}
        for spec in STEPS:
            d = durations.get(spec.key, spec.default_s)
            if isinstance(d, bool) or not isinstance(d, (int, float)) or not (spec.min_s <= d <= spec.max_s):
                raise CalibrationError(f"{spec.title} duration must be {spec.min_s:g}-{spec.max_s:g} s")
            self.steps[spec.key] = StepData(spec.key, float(d))
        self.state = "ready"           # ready | waiting | recording | computed | failed | cancelled
        self.step_index = 0
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.created = clock()
        self.running = False           # a run started with begin() is in progress
        self.run_keys: list = []       # steps planned for the current run
        self.run_started: Optional[float] = None
        self.wait_until: Optional[float] = None
        self.wait_kind: Optional[str] = None   # lead_in | grace | transition
        self.run_timeline: Optional[dict] = None   # schedule fixed when the run started

    @property
    def current(self) -> Optional[StepData]:
        if 0 <= self.step_index < len(STEPS):
            return self.steps[STEP_KEYS[self.step_index]]
        return None

    def planned_keys(self) -> list:
        """Steps a run started now would record (all pending ones with auto)."""
        cur = self.current
        if cur is None:
            return []
        if not self.auto:
            return [cur.key]
        return [k for k in STEP_KEYS[self.step_index:] if k == cur.key or self.steps[k].status != "done"]

    def timeline(self) -> dict:
        if self.running and self.run_timeline is not None:
            return self.run_timeline
        return build_timeline(self.planned_keys(), {k: self.steps[k].duration_s for k in STEP_KEYS},
                              self.lead_in_s, self.grace_s, self.settle_s, self.transition_s)

    def begin(self) -> None:
        """Start a run: lead-in, then the current step (and the rest with auto)."""
        if self.state != "ready" or self.current is None:
            raise CalibrationError(f"cannot start a step in state '{self.state}'")
        self.run_keys = self.planned_keys()
        self.run_timeline = self.timeline()      # not running yet: planned from run_keys
        self.run_started = self.clock()
        self.running = True
        self._schedule(self.lead_in_s, "lead_in")

    def start_step(self) -> None:
        """Record the current step immediately (no lead-in, no auto-advance)."""
        if self.state != "ready" or self.current is None:
            raise CalibrationError(f"cannot start a step in state '{self.state}'")
        self._start_current()

    def _schedule(self, delay: float, kind: str, t0_hint: Optional[float] = None) -> None:
        if delay > 0:
            self.state = "waiting"
            self.wait_until = self.clock() + delay
            self.wait_kind = kind
        else:
            self._start_current(t0_hint)

    def _start_current(self, t0_hint: Optional[float] = None) -> None:
        sd = self.current
        self.steps[sd.key] = StepData(sd.key, sd.duration_s, status="recording", wall_start=self.clock(),
                                      settle_s=self.settle_s, t0_hint=t0_hint)
        self.state = "recording"
        self.wait_until = None
        self.wait_kind = None

    def tick(self) -> None:
        """Start the scheduled step once its lead-in / grace period is over."""
        if self.state == "waiting" and self.clock() >= self.wait_until:
            self._start_current()

    def add_frame(self, frame, profile, layout, sensor=None, synthetic=None, source_kind=None) -> None:
        """Engine thread: one motion frame plus the current smoothed profile."""
        self.tick()
        if self.state != "recording":
            return
        sd = self.current
        if sd.t0 is None:
            sd.t0 = sd.t0_hint if sd.t0_hint is not None and sd.t0_hint <= frame.ts else frame.ts
            sd.layout = layout
            sd.sensor, sd.synthetic, sd.source_kind = sensor, synthetic, source_kind
            ref = self.steps["empty_baseline"]
            if sd.key != "empty_baseline" and ref.status == "done":
                if layout != ref.layout or (sensor_part(sensor) != sensor_part(ref.sensor)) or synthetic != ref.synthetic:
                    sd.problems.append("the sensor configuration or source differs from the empty-room baseline")
                    self._finish_step()
                    return
        elif layout != sd.layout:
            sd.problems.append("the CSI subcarrier layout changed during the step")
            self._finish_step()
            return
        sd.t_last = frame.ts
        sd.last_quality = {"ok": bool(frame.quality_ok), "reasons": list(frame.quality_reasons)}
        if frame.ts - sd.t0 < sd.settle_s:
            return  # analysis window still contains data from before the step
        sd.total += 1
        feats = (frame.motion_score, frame.variance, frame.diff_energy, frame.baseline_deviation)
        if frame.quality_ok and frame.variance is not None and frame.diff_energy is not None:
            sd.ok += 1
            for name, val in zip(FEATURES, feats):
                sd.features[name].append(val)
            if profile is not None and layout is not None and np.size(profile) == layout[1]:
                sd.profiles.append(np.array(profile, dtype=float))
        else:
            for r in frame.quality_reasons or ["features unavailable"]:
                sd.reasons[r.split(":", 1)[0]] += 1
        if sd.elapsed_s >= sd.duration_s:
            self._finish_step()

    def check_timeout(self, source_ended: bool = False) -> None:
        """Wall-clock guard: end a step that receives no (or too little) data."""
        self.tick()
        if self.state != "recording":
            return
        sd = self.current
        waited = self.clock() - (sd.wall_start if sd.wall_start is not None else self.clock())
        if source_ended or waited > 2.0 * sd.duration_s + 15.0:
            sd.problems.append("the data source ended" if source_ended else "timed out waiting for data")
            self._finish_step()

    def _finish_step(self) -> None:
        sd = self.current
        problems = step_problems(sd, self.frame_rate_hz)
        if problems:
            sd.status = "failed"
            sd.failure = "; ".join(problems)
            self.state = "ready"          # redo the same step
            self._end_run()
            return
        sd.status = "done"
        sd.failure = None
        self._advance()
        nxt = self.current
        if self.running and self.auto and self.state == "ready" and nxt is not None and nxt.key in self.run_keys:
            prev, spec = STEP_BY_KEY[sd.key], STEP_BY_KEY[nxt.key]
            if spec.person_present and not prev.person_present and self.grace_s > 0:
                self._schedule(self.grace_s, "grace")
            else:
                self._schedule(0.0, "chain", t0_hint=sd.t_last)   # back to back in packet time
        elif self.state != "recording":
            self._end_run()

    def _end_run(self) -> None:
        self.running = False
        self.wait_until = None
        self.wait_kind = None

    def _advance(self) -> None:
        pending = [i for i, k in enumerate(STEP_KEYS) if self.steps[k].status != "done"]
        if pending:
            self.step_index = pending[0]
            self.state = "ready"
            return
        self.step_index = len(STEPS)
        try:
            self.compute()
        except CalibrationError:
            pass  # state 'failed' with the reasons in self.error

    def compute(self) -> dict:
        try:
            self.result = compute_calibration(self.steps, self.placement, self.frame_rate_hz, self.defaults,
                                              now=self.clock())
        except CalibrationError as e:
            self.result = None
            self.error = str(e)
            self.state = "failed"
            raise
        self.error = None
        self.state = "computed"
        return self.result

    def cancel_step(self) -> None:
        if self.state not in ("recording", "waiting"):
            raise CalibrationError("no step is being recorded")
        sd = self.current
        self.steps[sd.key] = StepData(sd.key, sd.duration_s)
        self.state = "ready"
        self._end_run()

    def redo(self, key: Optional[str] = None) -> None:
        if self.state == "cancelled":
            raise CalibrationError("calibration was cancelled; start a new one")
        if self.state in ("recording", "waiting"):
            raise CalibrationError("cancel the running step first")
        if key is None:
            if self.current is not None:
                key = self.current.key
            else:
                key = STEP_KEYS[-1]
        if key not in self.steps:
            raise CalibrationError(f"step must be one of: {', '.join(STEP_KEYS)}")
        old = self.steps[key]
        self.steps[key] = StepData(key, old.duration_s)
        self.step_index = STEP_KEYS.index(key)
        self.result = None
        self.error = None
        self.state = "ready"

    def cancel(self) -> None:
        self.state = "cancelled"
        self._end_run()

    def phase(self) -> Optional[dict]:
        """What is happening right now and how long it still takes."""
        cur = self.current
        if cur is None:
            return None
        if self.state == "waiting":
            return {"kind": self.wait_kind, "key": cur.key,
                    "remaining_s": round(max(0.0, self.wait_until - self.clock()), 2)}
        if self.state == "recording":
            span = cur.settle_s + cur.duration_s
            done = (cur.t_last - cur.t0) if cur.t0 is not None and cur.t_last is not None else 0.0
            return {"kind": "step", "key": cur.key, "remaining_s": round(max(0.0, span - done), 2)}
        return None

    def to_dict(self) -> dict:
        cur = self.current
        return {
            "placement": self.placement,
            "state": self.state,
            "step_index": self.step_index,
            "current_step": cur.key if cur is not None else None,
            "steps": [self.steps[k].summary() for k in STEP_KEYS],
            "result": self.result,
            "error": self.error,
            "auto": self.auto,
            "lead_in_s": self.lead_in_s,
            "grace_s": self.grace_s,
            "settle_s": self.settle_s,
            "running": self.running,
            "run_elapsed_s": round(self.clock() - self.run_started, 2) if self.running else None,
            "phase": self.phase(),
            "timeline": self.timeline(),
        }


# ----------------------------------------------------------------- store
class CalibrationStore:
    """JSON calibration files in ``root`` (``data/calibration`` by default)."""

    def __init__(self, root: Path = DEFAULT_CALIBRATION_DIR) -> None:
        self.root = Path(root)

    def path_for(self, key: str) -> Path:
        validate_key(key)
        p = (self.root / f"{key}.json").resolve()
        if p.parent != self.root.resolve():
            raise CalibrationError("invalid calibration key")
        return p

    def save(self, result: dict) -> Path:
        key = validate_key(result.get("key"))
        path = self.path_for(key)
        write_json_atomic(path, result)
        return path

    def load(self, key: str) -> dict:
        path = self.path_for(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CalibrationError("calibration not found") from None
        except (OSError, json.JSONDecodeError) as e:
            raise CalibrationError(f"cannot read calibration {key}: {e}") from None
        if not isinstance(data, dict) or data.get("kind") != "echoguard-calibration":
            raise CalibrationError(f"{path.name} is not an EchoGuard calibration")
        data["key"] = key
        return data

    def delete(self, key: str) -> None:
        path = self.path_for(key)
        if not path.is_file():
            raise CalibrationError("calibration not found")
        path.unlink()

    def list(self) -> list:
        out = []
        if not self.root.is_dir():
            return out
        for p in self.root.glob("*.json"):
            if not KEY_RE.match(p.stem):
                continue
            try:
                d = self.load(p.stem)
            except CalibrationError as e:
                log.warning("skipping %s: %s", p.name, e)
                continue
            out.append(summary(d))
        out.sort(key=lambda s: s.get("created_unix") or 0, reverse=True)
        return out

    def find_match(self, sensor: Optional[dict], synthetic: bool, placement: Optional[str] = None) -> Optional[dict]:
        """Newest calibration for this sensor configuration (preferring
        ``placement``). Synthetic calibrations only match synthetic sources."""
        part = sensor_part(sensor, synthetic)
        if part is None:
            return None
        cands = [s for s in self.list() if s["key"].split("__", 1)[-1] == part
                 and bool(s.get("synthetic")) == bool(synthetic)]
        if not cands:
            return None
        if placement:
            same = [s for s in cands if s.get("placement") == placement]
            cands = same or cands
        return self.load(cands[0]["key"])


def summary(d: dict) -> dict:
    return {
        "key": d.get("key"), "placement": d.get("placement"), "name": display_name(d),
        "created": d.get("created"), "created_unix": d.get("created_unix"),
        "synthetic": bool(d.get("synthetic", False)), "source_kind": d.get("source_kind"),
        "sensor": d.get("sensor"), "thresholds": d.get("thresholds"), "separable": d.get("separable"),
        "warnings": d.get("warnings", []),
    }


def display_name(d: dict) -> str:
    created = d.get("created_unix")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(created)) if isinstance(created, (int, float)) else "?"
    return f"{d.get('placement') or 'calibration'} ({when}){' [SYNTHETIC]' if d.get('synthetic') else ''}"


# --------------------------------------------------------------- manager
class CalibrationManager:
    """Connects calibration sessions and stored calibrations to an Engine.

    Registers an engine frame hook that (a) feeds the running calibration step
    and (b) auto-loads the newest stored calibration matching the detected
    sensor configuration (synthetic only for synthetic sources). Guarded by a
    lock: the hook runs in the engine thread, the API in request threads.
    """

    def __init__(self, engine, store: Optional[CalibrationStore] = None, auto_load: bool = True,
                 clock=time.time) -> None:
        self.engine = engine
        self.store = store or CalibrationStore()
        self.auto_load = auto_load
        self.clock = clock
        self._lock = threading.RLock()
        self.session: Optional[CalibrationSession] = None
        self.placement: Optional[str] = None
        self.last_auto: Optional[dict] = None
        self._seen = None
        engine.add_frame_hook(self._on_frame)

    # ----------------------------------------------------- engine thread
    def _on_frame(self, frame, activity) -> None:
        eng = self.engine
        sensor = eng.sensor_config()
        synthetic = bool(eng.synthetic)
        proc = eng.processor
        profile = proc.last_smoothed
        layout = proc.layout
        with self._lock:
            marker = (eng.started_at, sensor_part(sensor, synthetic))
            if self.auto_load and marker != self._seen and marker[1] is not None:
                self._seen = marker
                try:
                    self._auto_select(sensor, synthetic)
                except Exception:  # noqa: BLE001 - never break acquisition
                    log.exception("calibration auto-load failed")
            if self.session is not None:
                self.session.add_frame(frame, None if profile is None else profile.copy(), layout,
                                       sensor=sensor, synthetic=synthetic, source_kind=eng.source_kind)

    def _auto_select(self, sensor: dict, synthetic: bool) -> None:
        match = self.store.find_match(sensor, synthetic, placement=self.placement)
        current = self.engine.calibration_info
        if match is not None:
            if current is None or current.get("key") != match["key"]:
                self._apply(match, auto=True)
                self.last_auto = {"time": self.clock(), "action": "loaded", "key": match["key"]}
            return
        if current is not None:
            if current.get("sensor_part") != sensor_part(sensor, synthetic) or bool(current.get("synthetic")) != synthetic:
                self.engine.clear_calibration()
                self.last_auto = {"time": self.clock(), "action": "cleared", "key": current.get("key"),
                                  "reason": "the active calibration does not match the detected sensor configuration"}
                return
        self.last_auto = {"time": self.clock(), "action": "none", "key": None}

    def _apply(self, data: dict, auto: bool = False) -> None:
        b = data.get("baseline") or {}
        mean, std = _to_array(b.get("mean_profile") or []), _to_array(b.get("std_profile") or [])
        if mean.size == 0 or mean.shape != std.shape:
            raise CalibrationError("calibration file has no valid baseline profile")
        th = data.get("thresholds") or {}
        try:
            low, mov, rap = float(th["low"]), float(th["movement"]), float(th["rapid"])
        except (KeyError, TypeError, ValueError):
            raise CalibrationError("calibration file has no valid thresholds") from None
        if not (0 < low < mov < rap):
            raise CalibrationError("calibration thresholds must satisfy 0 < low < movement < rapid")
        info = {"key": data.get("key"), "name": display_name(data), "created": data.get("created"),
                "synthetic": bool(data.get("synthetic", False)), "auto_loaded": auto,
                "sensor_part": sensor_part(data.get("sensor"), bool(data.get("synthetic", False)))}
        self.engine.apply_calibration(mean, std, data.get("noise_variance"), data.get("noise_diff"),
                                      {"low": low, "movement": mov, "rapid": rap}, info)
        if data.get("placement"):
            self.placement = data["placement"]

    # ------------------------------------------------------- API (requests)
    def start(self, placement: str, durations: Optional[dict] = None, lead_in_s: float = 0.0,
              grace_s: float = 0.0, auto: bool = False) -> dict:
        with self._lock:
            if self.session is not None and self.session.state in ("recording", "waiting"):
                raise CalibrationError("a calibration step is running; cancel it first")
            self.session = CalibrationSession(placement, durations,
                                              frame_rate_hz=self.engine.config.processing.frame_rate_hz,
                                              clock=self.clock, defaults=self.engine.base_thresholds,
                                              settle_s=self.engine.config.processing.window_s,
                                              lead_in_s=lead_in_s, grace_s=grace_s, auto=auto)
            return self.state()

    def _require_session(self) -> CalibrationSession:
        if self.session is None or self.session.state == "cancelled":
            raise CalibrationError("no calibration in progress; start one first")
        return self.session

    def step_start(self) -> dict:
        with self._lock:
            s = self._require_session()
            eng = self.engine
            st = eng.source_status
            if not eng.running or st is None or st.finished:
                raise CalibrationError("no data source is running; connect the receiver (or start a replay) first")
            if eng.source_kind == "live" and not st.connected:
                raise CalibrationError("the serial source is disconnected")
            s.begin()
            return self.state()

    def step_cancel(self) -> dict:
        with self._lock:
            self._require_session().cancel_step()
            return self.state()

    def step_redo(self, key: Optional[str] = None) -> dict:
        with self._lock:
            self._require_session().redo(key)
            return self.state()

    def cancel(self) -> dict:
        with self._lock:
            self._require_session().cancel()
            self.session = None
            return self.state()

    def save(self) -> dict:
        with self._lock:
            s = self._require_session()
            if s.state != "computed" or s.result is None:
                raise CalibrationError("nothing to save: finish all calibration steps first")
            result = dict(s.result)
            if result.get("sensor") is None:
                raise CalibrationError("sensor configuration unknown; cannot key the calibration")
            result["key"] = sensor_key(result["sensor"], s.placement, bool(result.get("synthetic")))
            path = self.store.save(result)
            self._apply(result)
            self.placement = s.placement
            s.result = result
            s.state = "saved"
            return {"key": result["key"], "path": path.name, "calibration": summary(result)}

    def load(self, key: str) -> dict:
        with self._lock:
            data = self.store.load(key)
            eng = self.engine
            if eng.source_kind is not None and bool(data.get("synthetic")) and not eng.synthetic:
                raise CalibrationError("this calibration was computed from SYNTHETIC data and cannot be used "
                                       "with real CSI")
            warnings_out = []
            sensor = eng.sensor_config()
            if sensor is not None and sensor_part(sensor, bool(eng.synthetic)) != sensor_part(
                    data.get("sensor"), bool(data.get("synthetic"))):
                warnings_out.append("this calibration was made for a different sensor configuration")
            self._apply(data)
            return {"key": key, "calibration": summary(data), "warnings": warnings_out}

    def delete(self, key: str) -> dict:
        with self._lock:
            path = self.store.path_for(key)
            if not path.is_file():
                raise CalibrationError("calibration not found")
            current = self.engine.calibration_info
            if current is not None and current.get("key") == key:
                self.engine.clear_calibration()
            self.store.delete(key)
            return {"key": key}

    def clear(self) -> dict:
        with self._lock:
            self.engine.clear_calibration()
            return self.state()

    def state(self) -> dict:
        with self._lock:
            s = self.session
            if s is not None:
                st = self.engine.source_status
                ended = s.state in ("recording", "waiting") and (st is None or st.finished or not self.engine.running)
                s.check_timeout(source_ended=ended)
            return {
                "session": s.to_dict() if s is not None else None,
                "active": self.engine.calibration_info,
                "placement": self.placement,
                "last_auto": self.last_auto,
                "step_specs": [{"key": p.key, "title": p.title, "instructions": p.instructions, "cue": p.cue,
                                "person_present": p.person_present, "default_s": p.default_s,
                                "min_s": p.min_s, "max_s": p.max_s} for p in STEPS],
            }
