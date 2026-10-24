"""Motion activity classes and the threshold-based classifier.

``potential_fall`` is never produced here: it is set by the fall detector
(Milestone 4) while an alert is active. Nothing here is medically validated.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .config import MotionThresholds


class Activity(str, Enum):
    EMPTY_LOW = "empty_low"
    MOVEMENT = "movement"
    RAPID_MOVEMENT = "rapid_movement"
    POTENTIAL_FALL = "potential_fall"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return ACTIVITY_LABELS[self.value]


ACTIVITY_LABELS = {
    "empty_low": "Empty room / low activity",
    "movement": "Movement detected",
    "rapid_movement": "Rapid movement",
    "potential_fall": "Potential fall",
    "unknown": "Unknown / insufficient data",
}


def activity_label(key: str) -> str:
    return ACTIVITY_LABELS.get(key, ACTIVITY_LABELS["unknown"])


def classify_activity(frame, thresholds: Optional[MotionThresholds] = None) -> Activity:
    """Map a :class:`backend.processing.MotionFrame` to an :class:`Activity`.

    Returns ``UNKNOWN`` when there is no frame, data quality is insufficient or
    the motion score could not be computed.
    """
    th = thresholds or MotionThresholds()
    if frame is None or not frame.quality_ok or frame.motion_score is None:
        return Activity.UNKNOWN
    score = frame.motion_score
    if score >= th.rapid:
        return Activity.RAPID_MOVEMENT
    if score >= th.movement:
        return Activity.MOVEMENT
    return Activity.EMPTY_LOW
