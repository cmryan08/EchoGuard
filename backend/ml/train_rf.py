"""Optional Random Forest activity classifier with session-held-out evaluation.

    python -m backend.ml.train_rf --recordings data/recordings
    python -m backend.ml.train_rf --recordings data/recordings --allow-synthetic   # pipeline test only

Pipeline:

1. every take of every session is replayed offline through the same parser
   and :class:`backend.processing.SignalProcessor` as live data
   (:mod:`backend.offline`); the take CSV's ``label`` column is the class,
2. windows of ``--window`` seconds (step ``--step``) of quality-ok motion
   frames become feature vectors (mean/std/min/max of motion score, variance,
   diff energy and baseline deviation, the largest score rise, relative
   amplitude and RSSI variability),
3. evaluation uses **grouped folds by session_id** (leave-one-session-out
   when there are few sessions): a session is never split between training
   and test, so neighbouring, highly correlated windows cannot leak,
4. per-class precision / recall / F1 and the confusion matrix are computed on
   the held-out sessions only, then a final model is fitted on all data and
   saved to ``data/models/`` with metadata.

Safety rules: training is refused with fewer than 2 real sessions, and
whenever a session is synthetic (simulation, or packets from the simulator
MAC) unless ``--allow-synthetic`` is given; in that case every output is
labelled SYNTHETIC and must not be read as accuracy evidence. Even with real
data, the numbers only describe the recorded sessions (same room, people
and placement); they are not a validated fall-detection accuracy.

scikit-learn / joblib are optional (``make setup-ml``) and imported lazily.
This module imports neither flask nor pandas.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

from backend.config import DATA_DIR, AppConfig
from backend.offline import OfflineReplay
from backend.recording import LABELS, RecordingError, Session

SIM_MAC = "02:51:4d:00:00:00"   # simulation.generator.SIM_MAC (kept literal to avoid importing simulation)
DEFAULT_MODELS_DIR = DATA_DIR / "models"
FRAME_FEATURES = ("motion_score", "variance", "diff_energy", "baseline_deviation")
SYNTHETIC_NOTE = "SYNTHETIC DATA: pipeline check only, NOT evidence of real-world accuracy."
MISSING_SKLEARN = ("scikit-learn is not installed in this Python. Install the optional ML dependencies with "
                   "`make setup-ml` (pip install -r requirements-ml.txt), then re-run.")


class PolicyError(RuntimeError):
    """Training refused (not enough real sessions, synthetic data, ...)."""


def feature_names() -> List[str]:
    names = []
    for f in FRAME_FEATURES:
        names += [f"{f}_mean", f"{f}_std", f"{f}_min", f"{f}_max"]
    return names + ["motion_score_max_rise", "motion_score_delta", "amplitude_rel_std", "rssi_std"]


def window_features(frames: list) -> np.ndarray:
    """Feature vector of a window of MotionFrames (all quality-ok)."""
    out = []
    for f in FRAME_FEATURES:
        v = np.array([getattr(fr, f) if getattr(fr, f) is not None else np.nan for fr in frames], dtype=float)
        out += [np.nanmean(v), np.nanstd(v), np.nanmin(v), np.nanmax(v)]
    s = np.array([fr.motion_score for fr in frames], dtype=float)
    run_min = np.minimum.accumulate(s)
    out += [float(np.max(s - run_min)), float(s[-1] - s[0])]
    amp = np.array([fr.mean_amplitude if fr.mean_amplitude is not None else np.nan for fr in frames], dtype=float)
    m = np.nanmean(amp)
    out.append(float(np.nanstd(amp) / m) if m and math.isfinite(m) else 0.0)
    rssi = np.array([fr.rssi if fr.rssi is not None else np.nan for fr in frames], dtype=float)
    out.append(float(np.nanstd(rssi)))
    return np.nan_to_num(np.array(out, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)


def take_windows(path, config: Optional[AppConfig] = None, window_s: float = 2.0, step_s: float = 0.5):
    """(features [n, d], label, synthetic_mac_seen, n_frames) for one take CSV."""
    rep = OfflineReplay(path, config)
    frames, label, sim_mac = [], None, False
    for step in rep:
        label = label or step.label
        if step.packet.mac == SIM_MAC:
            sim_mac = True
        if step.frame is not None:
            frames.append(step.frame)
    feats = []
    if frames:
        t_start, t_end = frames[0].ts, frames[-1].ts
        t = t_start + window_s
        while t <= t_end + 1e-9:
            win = [f for f in frames if t - window_s < f.ts <= t]
            t += step_s
            if len(win) < 3 or not all(f.quality_ok and f.motion_score is not None for f in win):
                continue
            feats.append(window_features(win))
    X = np.vstack(feats) if feats else np.zeros((0, len(feature_names())))
    return X, label, sim_mac, len(frames)


@dataclass
class Dataset:
    X: np.ndarray
    y: np.ndarray
    groups: np.ndarray
    sessions: List[dict] = field(default_factory=list)
    feature_names: List[str] = field(default_factory=feature_names)

    @property
    def synthetic_sessions(self) -> List[str]:
        return [s["session_id"] for s in self.sessions if s["synthetic"]]

    @property
    def real_sessions(self) -> List[str]:
        return [s["session_id"] for s in self.sessions if not s["synthetic"]]

    def subset(self, keep_sessions) -> "Dataset":
        keep_sessions = set(keep_sessions)
        m = np.array([g in keep_sessions for g in self.groups], dtype=bool)
        return Dataset(self.X[m], self.y[m], self.groups[m],
                       [s for s in self.sessions if s["session_id"] in keep_sessions], list(self.feature_names))


def load_dataset(recordings_dir, config: Optional[AppConfig] = None, window_s: float = 2.0,
                 step_s: float = 0.5, log: Callable = print) -> Dataset:
    root = Path(recordings_dir)
    Xs, ys, gs, sessions = [], [], [], []
    if not root.is_dir():
        raise PolicyError(f"recordings directory not found: {root}")
    for d in sorted(root.iterdir()):
        if not (d.is_dir() and (d / "session.json").is_file()):
            continue
        try:
            s = Session.load(d)
        except RecordingError as e:
            log(f"skipping {d.name}: {e}")
            continue
        info = {"session_id": s.session_id, "synthetic": bool(s.meta.get("synthetic", False)),
                "source_kind": s.meta.get("source_kind"), "windows": 0, "labels": {}}
        for take in s.take_files():
            X, label, sim_mac, _ = take_windows(take, config, window_s, step_s)
            if sim_mac:
                info["synthetic"] = True
            if label not in LABELS or X.shape[0] == 0:
                continue
            Xs.append(X)
            ys += [label] * X.shape[0]
            gs += [s.session_id] * X.shape[0]
            info["windows"] += X.shape[0]
            info["labels"][label] = info["labels"].get(label, 0) + X.shape[0]
        if info["windows"]:
            sessions.append(info)
    X = np.vstack(Xs) if Xs else np.zeros((0, len(feature_names())))
    return Dataset(X, np.array(ys, dtype=object), np.array(gs, dtype=object), sessions)


def apply_policy(ds: Dataset, allow_synthetic: bool = False, exclude_synthetic: bool = False) -> Dataset:
    """Enforce the data rules; returns the dataset to use or raises PolicyError."""
    if exclude_synthetic and ds.synthetic_sessions:
        ds = ds.subset(ds.real_sessions)
    if not ds.sessions:
        raise PolicyError("no labeled takes with enough good-quality data were found")
    if ds.synthetic_sessions and not allow_synthetic:
        raise PolicyError(
            f"{len(ds.synthetic_sessions)} session(s) contain SYNTHETIC data ({', '.join(ds.synthetic_sessions)}). "
            "Synthetic data cannot provide accuracy evidence. Use --exclude-synthetic to ignore them, or "
            "--allow-synthetic only to test the pipeline (outputs will be marked synthetic).")
    if not allow_synthetic and len(ds.real_sessions) < 2:
        raise PolicyError(
            f"need at least 2 real recording sessions for session-held-out evaluation, found "
            f"{len(ds.real_sessions)}. Record more sessions (different days / placements).")
    if len(ds.sessions) < 2:
        raise PolicyError("need at least 2 sessions: evaluation always holds out complete sessions")
    if len(set(ds.y.tolist())) < 2:
        raise PolicyError("need at least 2 different labels")
    return ds


def group_kfold(groups, n_splits: int):
    """Yield (train_idx, test_idx) with whole groups (sessions) per fold.

    Groups are assigned greedily, largest first, to the fold with the fewest
    samples. Never puts one group in both train and test."""
    groups = np.asarray(groups, dtype=object)
    uniq, counts = np.unique(groups, return_counts=True)
    n_splits = int(max(2, min(n_splits, uniq.size)))
    if uniq.size < 2:
        raise PolicyError("need at least 2 groups (sessions) for grouped splits")
    folds: List[list] = [[] for _ in range(n_splits)]
    sizes = [0] * n_splits
    for g, c in sorted(zip(uniq.tolist(), counts.tolist()), key=lambda t: (-t[1], str(t[0]))):
        k = int(np.argmin(sizes))
        folds[k].append(g)
        sizes[k] += c
    for fold in folds:
        test = np.isin(groups, fold)
        yield np.flatnonzero(~test), np.flatnonzero(test)


def confusion_and_scores(y_true, y_pred, labels) -> dict:
    labels = list(labels)
    idx = {l: i for i, l in enumerate(labels)}
    cm = np.zeros((len(labels), len(labels)), dtype=int)
    for t, p in zip(y_true, y_pred):
        cm[idx[t], idx[p]] += 1
    per = {}
    for i, l in enumerate(labels):
        tp = cm[i, i]
        fp = cm[:, i].sum() - tp
        fn = cm[i, :].sum() - tp
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        f1 = (2 * prec * rec / (prec + rec)) if prec and rec else (0.0 if prec is not None and rec is not None else None)
        per[l] = {"precision": prec, "recall": rec, "f1": f1, "support": int(cm[i, :].sum())}
    acc = float(np.trace(cm) / cm.sum()) if cm.sum() else None
    return {"labels": labels, "confusion_matrix": cm.tolist(), "per_class": per, "accuracy_heldout": acc}


def _require_sklearn():
    try:
        from sklearn.ensemble import RandomForestClassifier  # noqa: F401
        import joblib  # noqa: F401
    except ImportError:
        raise SystemExit(MISSING_SKLEARN) from None


def make_model(n_estimators: int = 200, seed: int = 0):
    from sklearn.ensemble import RandomForestClassifier

    return RandomForestClassifier(n_estimators=n_estimators, class_weight="balanced_subsample",
                                  min_samples_leaf=2, random_state=seed, n_jobs=1)


def evaluate_grouped(ds: Dataset, n_splits: int, model_factory: Callable) -> dict:
    labels = sorted(set(ds.y.tolist()))
    y_pred = np.empty(ds.y.shape, dtype=object)
    folds = []
    for train, test in group_kfold(ds.groups, n_splits):
        train_g, test_g = set(ds.groups[train].tolist()), set(ds.groups[test].tolist())
        assert not (train_g & test_g), "a session appears in both train and test"
        model = model_factory()
        model.fit(ds.X[train], ds.y[train])
        y_pred[test] = model.predict(ds.X[test])
        folds.append({"test_sessions": sorted(test_g), "train_sessions": sorted(train_g), "n_test": int(test.size),
                      "unseen_labels": sorted(set(ds.y[test].tolist()) - set(ds.y[train].tolist()))})
    out = confusion_and_scores(ds.y.tolist(), y_pred.tolist(), labels)
    out["folds"] = folds
    out["split"] = "grouped by session_id (whole sessions held out)"
    return out


def format_report(ev: dict, synthetic: bool) -> str:
    lines = []
    if synthetic:
        lines.append("=" * 72)
        lines.append(SYNTHETIC_NOTE)
        lines.append("=" * 72)
    lines.append(f"Evaluation: {ev['split']}, {len(ev['folds'])} folds")
    for f in ev["folds"]:
        extra = f"  (labels never seen in training: {', '.join(f['unseen_labels'])})" if f["unseen_labels"] else ""
        lines.append(f"  held out {', '.join(f['test_sessions'])}: {f['n_test']} windows{extra}")
    lines.append("")
    lines.append(f"{'label':<16} {'precision':>9} {'recall':>7} {'f1':>6} {'support':>8}")
    fmt = lambda v: f"{v:.2f}" if isinstance(v, float) else "-"  # noqa: E731
    for l in ev["labels"]:
        p = ev["per_class"][l]
        lines.append(f"{l:<16} {fmt(p['precision']):>9} {fmt(p['recall']):>7} {fmt(p['f1']):>6} {p['support']:>8}")
    lines.append("")
    lines.append("Confusion matrix (rows = true, columns = predicted, held-out sessions only):")
    labels = ev["labels"]
    w = max(6, max(len(l) for l in labels))
    lines.append(" " * (w + 1) + " ".join(f"{l[:w]:>{w}}" for l in labels))
    for l, row in zip(labels, ev["confusion_matrix"]):
        lines.append(f"{l[:w]:<{w}} " + " ".join(f"{v:>{w}}" for v in row))
    lines.append("")
    lines.append("These numbers describe only the recorded sessions (room, people, placement). They are not a "
                 "validated fall-detection accuracy and not medical evidence.")
    if synthetic:
        lines.append(SYNTHETIC_NOTE)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m backend.ml.train_rf",
                                description="Train an experimental Random Forest on labeled EchoGuard recordings")
    p.add_argument("--recordings", default=str(DATA_DIR / "recordings"), help="recordings root (session dirs)")
    p.add_argument("--out-dir", default=str(DEFAULT_MODELS_DIR), help="where to save the model + metadata")
    p.add_argument("--window", type=float, default=2.0, help="feature window length in seconds")
    p.add_argument("--step", type=float, default=0.5, help="window step in seconds")
    p.add_argument("--folds", type=int, default=5, help="max grouped folds (<= number of sessions)")
    p.add_argument("--trees", type=int, default=200, help="number of trees")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--allow-synthetic", action="store_true",
                   help="allow synthetic sessions (pipeline test only; outputs are marked SYNTHETIC)")
    p.add_argument("--exclude-synthetic", action="store_true", help="ignore synthetic sessions")
    p.add_argument("--no-save", action="store_true", help="evaluate only, do not save a model")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.window <= 0 or args.step <= 0 or args.folds < 2 or args.trees < 1:
        print("--window/--step must be > 0, --folds >= 2, --trees >= 1", file=sys.stderr)
        return 2
    _require_sklearn()
    print(f"Loading labeled takes from {args.recordings} (replayed through the live processing pipeline)...")
    try:
        ds = load_dataset(args.recordings, AppConfig(), args.window, args.step)
        for s in ds.sessions:
            print(f"  {s['session_id']}: {s['windows']} windows {s['labels']}" + ("  [SYNTHETIC]" if s["synthetic"] else ""))
        ds = apply_policy(ds, allow_synthetic=args.allow_synthetic, exclude_synthetic=args.exclude_synthetic)
    except PolicyError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    synthetic = bool(ds.synthetic_sessions)
    ev = evaluate_grouped(ds, args.folds, lambda: make_model(args.trees, args.seed))
    print()
    print(format_report(ev, synthetic))
    if args.no_save:
        return 0
    import joblib

    model = make_model(args.trees, args.seed)
    model.fit(ds.X, ds.y)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "rf_" + time.strftime("%Y%m%d-%H%M%S") + ("_synthetic" if synthetic else "")
    meta = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "model": "RandomForestClassifier", "experimental": True,
        "synthetic": synthetic, "note": SYNTHETIC_NOTE if synthetic else
        "Held-out-session metrics on the recorded sessions only; not a validated accuracy.",
        "feature_names": ds.feature_names, "labels": ev["labels"], "window_s": args.window, "step_s": args.step,
        "sessions": ds.sessions, "evaluation": ev, "n_windows": int(ds.X.shape[0]),
    }
    joblib.dump({"model": model, "meta": meta}, out_dir / f"{stem}.joblib")
    (out_dir / f"{stem}.json").write_text(json.dumps(meta, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"\nSaved {out_dir / (stem + '.joblib')} and {stem}.json" + ("  [SYNTHETIC]" if synthetic else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
