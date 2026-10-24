"""Sitting vs standing posture model built from calibration amplitude profiles.

Pure numpy (no scikit-learn). The inputs are the smoothed, mean-normalised
amplitude profiles (``SignalProcessor.last_smoothed``, one per good motion
frame) recorded during the calibration steps ``sitting``, ``standing_still``
and ``empty_baseline``. A person sitting or standing at a fixed spot changes
the static multipath pattern, so the per-subcarrier profile shifts.

Model (:func:`fit_posture_model`): per class and per subcarrier the mean and
std (std floored at :data:`backend.processing.STD_FLOOR`), a Fisher score per
subcarrier between sitting and standing
(``|mu_sit - mu_stand| / sqrt(var_sit + var_stand)``) and the indices of the
``k`` most discriminative subcarriers. :func:`score_profile` evaluates a
diagonal Gaussian log-likelihood over the selected subcarriers (non-finite
entries are ignored), :func:`classify_profile` turns it into a label and
softmax probabilities.

:func:`temporal_self_check` trains on one half of each step and tests on the
other half (both ways) using 2 s block averages. It is a within-calibration
self-check: optimistic, not a validated accuracy. The model is a location
dependent heuristic reference, not a medical measurement.

Models are plain JSON-compatible dicts (lists, ``None`` for non-finite
values), so they can be stored in calibration files and reused by other
components without conversion.
"""

from __future__ import annotations

import math
import warnings
from typing import Iterable, Optional

import numpy as np

from .processing import STD_FLOOR

MODEL_KIND = "diagonal-gaussian"
MODEL_FORMAT = 1
#: Posture classes, in display order.
CLASSES = ("sitting", "standing", "empty")
#: The two classes the subcarrier selection discriminates.
SIT_STAND = ("sitting", "standing")
#: Calibration step key -> posture class.
STEP_CLASSES = {"sitting": "sitting", "standing_still": "standing", "empty_baseline": "empty"}
DEFAULT_K = 16
SELF_CHECK_BLOCK_S = 2.0
SELF_CHECK_LABEL = "within-calibration self-check, optimistic, not a validated accuracy"
#: Warn below these values.
MIN_SELF_CHECK_ACCURACY = 0.8
MIN_FISHER = 1.0
SIMILAR_WARNING = ("sitting and standing look similar at this placement; move the boards so the line between "
                   "them passes near the chair at seated torso height, then recalibrate")
_LOG_2PI = math.log(2.0 * math.pi)


def json_list(arr) -> list:
    """Float list rounded to 6 decimals, ``None`` for non-finite values."""
    return [round(float(v), 6) if v is not None and math.isfinite(v) else None for v in np.asarray(arr, dtype=float)]


def _to_array(values) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in values], dtype=float)


def _stack(profiles, name: str) -> np.ndarray:
    rows = [np.asarray(p, dtype=float).ravel() for p in profiles]
    if not rows:
        raise ValueError(f"no profiles for class '{name}'")
    widths = {r.size for r in rows}
    if len(widths) != 1:
        raise ValueError(f"profiles of class '{name}' have different lengths")
    return np.vstack(rows)


def _ordered(classes: Iterable[str]) -> list:
    classes = list(classes)
    return [c for c in CLASSES if c in classes] + [c for c in classes if c not in CLASSES]


# --------------------------------------------------------------------- fit
def fit_posture_model(profiles_by_class: dict, k: int = DEFAULT_K, std_floor: float = STD_FLOOR) -> dict:
    """Per-class diagonal Gaussian over the ``k`` subcarriers that best
    separate sitting from standing.

    ``profiles_by_class`` maps class names (``sitting`` and ``standing`` are
    required, ``empty`` optional) to sequences of 1-D profiles of equal length.
    Raises :class:`ValueError` for missing classes or inconsistent shapes.
    """
    if not isinstance(profiles_by_class, dict):
        raise ValueError("profiles_by_class must be a dict")
    for name in SIT_STAND:
        if len(profiles_by_class.get(name) or []) < 2:
            raise ValueError(f"need at least 2 profiles for class '{name}'")
    k = int(k)
    if k < 1:
        raise ValueError("k must be at least 1")
    names = _ordered(n for n, p in profiles_by_class.items() if p is not None and len(p) > 0)
    means, stds, counts = {}, {}, {}
    width = None
    for name in names:
        P = _stack(profiles_by_class[name], name)
        if width is None:
            width = P.shape[1]
        elif P.shape[1] != width:
            raise ValueError("profiles of different classes have different lengths")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            mu = np.nanmean(P, axis=0)
            sd = np.nanstd(P, axis=0)
        means[name] = mu
        stds[name] = np.maximum(sd, std_floor)       # NaN stays NaN
        counts[name] = int(P.shape[0])
    mu_s, mu_t = means["sitting"], means["standing"]
    var = stds["sitting"] ** 2 + stds["standing"] ** 2
    with np.errstate(invalid="ignore", divide="ignore"):
        fisher = np.abs(mu_s - mu_t) / np.sqrt(var)
    finite = np.flatnonzero(np.isfinite(fisher))
    order = finite[np.argsort(-fisher[finite], kind="stable")]
    selected = [int(i) for i in order[:k]]
    return {
        "kind": MODEL_KIND,
        "format": MODEL_FORMAT,
        "classes": names,
        "n_subcarriers": int(width),
        "means": {n: json_list(means[n]) for n in names},
        "stds": {n: json_list(stds[n]) for n in names},
        "counts": counts,
        "fisher": json_list(fisher),
        "selected": selected,
        "k": len(selected),
        "best_fisher": round(float(fisher[selected[0]]), 6) if selected else None,
        "std_floor": std_floor,
    }


def validate_model(model) -> dict:
    """Check the structure of a (possibly loaded) model; returns it or raises ValueError."""
    if not isinstance(model, dict) or model.get("kind") != MODEL_KIND:
        raise ValueError("not a posture model")
    try:
        width = int(model["n_subcarriers"])
        classes = list(model["classes"])
        selected = [int(i) for i in model["selected"]]
        for c in classes:
            if len(model["means"][c]) != width or len(model["stds"][c]) != width:
                raise ValueError(f"class '{c}' has the wrong number of subcarriers")
    except (KeyError, TypeError) as e:
        raise ValueError(f"posture model is incomplete: {e}") from None
    if not all(c in classes for c in SIT_STAND):
        raise ValueError("posture model needs the sitting and standing classes")
    if not selected or any(not (0 <= i < width) for i in selected):
        raise ValueError("posture model has invalid subcarrier indices")
    return model


# ------------------------------------------------------------------- score
def score_profile(model: dict, profile, classes: Optional[Iterable[str]] = None) -> dict:
    """Diagonal Gaussian log-likelihood per class over the selected subcarriers.

    Only subcarriers that are finite in the profile and in every scored class
    are used, so the values are comparable between classes. Returns ``{}``
    when no selected subcarrier is usable. Raises :class:`ValueError` when the
    profile length differs from the model.
    """
    x = _to_array(np.asarray(profile, dtype=object).ravel()) if profile is not None else np.array([])
    width = int(model["n_subcarriers"])
    if x.size != width:
        raise ValueError(f"profile has {x.size} subcarriers, the posture model expects {width}")
    names = list(model["classes"]) if classes is None else [c for c in classes if c in model["classes"]]
    if not names:
        return {}
    idx = np.asarray(model["selected"], dtype=int)
    xs = x[idx]
    mus = {c: _to_array(model["means"][c])[idx] for c in names}
    sds = {c: _to_array(model["stds"][c])[idx] for c in names}
    mask = np.isfinite(xs)
    for c in names:
        mask &= np.isfinite(mus[c]) & np.isfinite(sds[c]) & (sds[c] > 0)
    if not mask.any():
        return {}
    out = {}
    for c in names:
        z = (xs[mask] - mus[c][mask]) / sds[c][mask]
        out[c] = float(np.sum(-0.5 * z * z - np.log(sds[c][mask]) - 0.5 * _LOG_2PI))
    return out


def classify_profile(model: dict, profile, classes: Optional[Iterable[str]] = None):
    """``(label, {class: probability})`` via a softmax of the log-likelihoods;
    ``(None, {})`` when the profile has no usable selected subcarrier."""
    ll = score_profile(model, profile, classes)
    if not ll:
        return None, {}
    names = list(ll)
    v = np.array([ll[c] for c in names], dtype=float)
    p = np.exp(v - v.max())
    p /= p.sum()
    probs = {c: float(pi) for c, pi in zip(names, p)}
    return max(probs, key=probs.get), probs


# -------------------------------------------------------------- self-check
def block_average(profiles, block_n: int) -> list:
    """NaN-safe means of consecutive blocks of ``block_n`` profiles (full blocks
    only; all profiles as one block when there are fewer than ``block_n``)."""
    rows = list(profiles)
    if not rows:
        return []
    block_n = max(1, int(block_n))
    chunks = [rows[i:i + block_n] for i in range(0, len(rows) - block_n + 1, block_n)] or [rows]
    out = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for chunk in chunks:
            out.append(np.nanmean(np.vstack([np.asarray(r, dtype=float) for r in chunk]), axis=0))
    return out


def _tally(results: dict) -> dict:
    per = {}
    tot = cor = 0
    for c, (n, ok) in results.items():
        per[c] = {"blocks": n, "correct": ok, "accuracy": round(ok / n, 4) if n else None}
        tot += n
        cor += ok
    return {"per_class": per, "accuracy": round(cor / tot, 4) if tot else None, "blocks": tot}


def temporal_self_check(profiles_by_class: dict, frame_rate_hz: float = 10.0, k: int = DEFAULT_K,
                        block_s: float = SELF_CHECK_BLOCK_S, std_floor: float = STD_FLOOR) -> dict:
    """Temporal split: fit on the first half of each class, test 2 s block
    averages of the second half, and vice versa.

    Returns accuracy per class and overall for all classes, and for the
    sitting-vs-standing decision alone (``sit_stand``). Labelled as a
    within-calibration self-check (optimistic, not a validated accuracy).
    """
    block_n = max(1, int(round(float(frame_rate_hz) * float(block_s))))
    out = {"label": SELF_CHECK_LABEL,
           "method": f"temporal split (first half / second half of each step, both ways), "
                     f"{block_s:g} s block averages",
           "block_s": block_s, "block_frames": block_n, "available": False, "reason": None}
    names = _ordered(n for n, p in profiles_by_class.items() if p is not None and len(p) > 0)
    halves = {}
    for n in names:
        rows = list(profiles_by_class[n])
        if len(rows) < 4:
            out["reason"] = f"too few profiles for class '{n}' ({len(rows)}, need 4)"
            return out
        mid = len(rows) // 2
        halves[n] = (rows[:mid], rows[mid:])
    if not all(n in halves for n in SIT_STAND):
        out["reason"] = "sitting and standing profiles are required"
        return out
    every = {n: [0, 0] for n in names}
    pair = {n: [0, 0] for n in SIT_STAND}
    for train_i, test_i in ((0, 1), (1, 0)):
        model = fit_posture_model({n: h[train_i] for n, h in halves.items()}, k=k, std_floor=std_floor)
        for n, h in halves.items():
            for block in block_average(h[test_i], block_n):
                label, _ = classify_profile(model, block)
                every[n][0] += 1
                every[n][1] += int(label == n)
                if n in pair:
                    label2, _ = classify_profile(model, block, SIT_STAND)
                    pair[n][0] += 1
                    pair[n][1] += int(label2 == n)
    allc = _tally(every)
    out.update(available=True, per_class=allc["per_class"], overall_accuracy=allc["accuracy"],
               blocks=allc["blocks"], chance_accuracy=round(1.0 / len(names), 4),
               sit_stand={**_tally(pair), "chance_accuracy": 0.5})
    return out


def quality_warnings(model: dict) -> list:
    """Warnings when sitting and standing are hard to tell apart."""
    out = []
    best = model.get("best_fisher")
    sc = model.get("self_check") or {}
    acc = (sc.get("sit_stand") or {}).get("accuracy") if sc.get("available") else None
    reasons = []
    if acc is not None and acc < MIN_SELF_CHECK_ACCURACY:
        reasons.append(f"sitting-vs-standing self-check {acc:.0%} (below {MIN_SELF_CHECK_ACCURACY:.0%}; "
                       "within-calibration, optimistic)")
    if best is None or best < MIN_FISHER:
        reasons.append(f"best subcarrier Fisher score {best if best is not None else 0:.2f} (below {MIN_FISHER:g})")
    if reasons:
        out.append(f"{SIMILAR_WARNING} ({'; '.join(reasons)})")
    if sc and not sc.get("available"):
        out.append(f"posture self-check unavailable: {sc.get('reason')}")
    return out
