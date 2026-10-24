"""Offline replay of recordings through the live pipeline (no threads, no UI).

Used by the CSV export, ``python -m backend.cli evaluate`` and the optional
Random Forest training. Lines are read with :class:`backend.sources.ReplaySource`
at ``speed=0`` (original timestamps kept), parsed by the official-format
parser and processed by a fresh :class:`backend.processing.SignalProcessor`,
exactly as the Engine does. Standard library + numpy only (no pandas / flask),
so it also runs in a minimal numpy + scikit-learn interpreter.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import numpy as np

from .activity import classify_activity
from .config import AppConfig
from .csi_parser import parse_any
from .processing import SignalProcessor
from .sources import ReplaySource


@dataclass
class Step:
    """One CSI packet and (about every 100 ms) the frame it produced."""

    packet: object
    frame: object            # MotionFrame or None
    activity: Optional[str]  # threshold classification of ``frame`` (None without frame)
    label: Optional[str]     # take label (ground truth) for labeled recordings
    take_id: Optional[str]
    session_id: Optional[str]


def apply_calibration_file(processor: SignalProcessor, config: AppConfig, calibration: Optional[dict]) -> None:
    """Use a stored calibration dict (``data/calibration/*.json``) offline."""
    if not calibration:
        return
    b = calibration.get("baseline") or {}
    mean = np.array([np.nan if v is None else v for v in b.get("mean_profile") or []], dtype=float)
    std = np.array([np.nan if v is None else v for v in b.get("std_profile") or []], dtype=float)
    if mean.size and mean.shape == std.shape:
        processor.set_baseline(mean, std, calibration.get("noise_variance"), calibration.get("noise_diff"),
                               name=str(calibration.get("key", "")))
    th = calibration.get("thresholds") or {}
    if all(k in th for k in ("low", "movement", "rapid")):
        config.thresholds.low = float(th["low"])
        config.thresholds.movement = float(th["movement"])
        config.thresholds.rapid = float(th["rapid"])
        config.thresholds.calibrated = True


class OfflineReplay:
    """Iterate :class:`Step` objects for a take CSV, session directory,
    RawRecorder CSV or raw serial log. ``config`` is copied, never modified."""

    def __init__(self, path, config: Optional[AppConfig] = None, calibration: Optional[dict] = None,
                 line_rate_hz: float = 100.0) -> None:
        self.path = Path(path)
        self.config = copy.deepcopy(config) if config is not None else AppConfig()
        self.processor = SignalProcessor(self.config.processing)
        apply_calibration_file(self.processor, self.config, calibration)
        self.source = ReplaySource(self.path, speed=0, loop=False, line_rate_hz=line_rate_hz)

    @property
    def synthetic(self) -> bool:
        return bool(self.source.synthetic)

    def __iter__(self) -> Iterator[Step]:
        extra = self.source.status.extra
        th = self.config.thresholds
        for ts, line in self.source.lines():
            parsed = parse_any(line, ts)
            if parsed.kind != "csi":
                continue
            frame = self.processor.process(parsed.packet)
            act = classify_activity(frame, th).value if frame is not None else None
            yield Step(parsed.packet, frame, act, extra.get("label"), extra.get("take_id"), extra.get("session_id"))
