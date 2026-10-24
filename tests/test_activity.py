from backend.activity import ACTIVITY_LABELS, Activity, activity_label, classify_activity
from backend.config import MotionThresholds
from backend.processing import MotionFrame


def frame(score, ok=True):
    return MotionFrame(ts=0.0, motion_score=score, variance=0.0, diff_energy=0.0, baseline_deviation=0.0,
                       mean_amplitude=1.0, rssi=-40.0, packet_rate=100.0, quality_ok=ok,
                       quality_reasons=[] if ok else ["low_packet_rate: 5 Hz < 20 Hz"])


def test_display_names_exact():
    assert ACTIVITY_LABELS == {
        "empty_low": "Empty room / low activity",
        "movement": "Movement detected",
        "rapid_movement": "Rapid movement",
        "potential_fall": "Potential fall",
        "unknown": "Unknown / insufficient data",
    }
    assert Activity.POTENTIAL_FALL.label == "Potential fall"
    assert activity_label("nonsense") == "Unknown / insufficient data"


def test_threshold_mapping():
    th = MotionThresholds(low=1.5, movement=2.5, rapid=6.0)
    assert classify_activity(frame(0.2), th) is Activity.EMPTY_LOW
    assert classify_activity(frame(2.0), th) is Activity.EMPTY_LOW
    assert classify_activity(frame(2.5), th) is Activity.MOVEMENT
    assert classify_activity(frame(5.9), th) is Activity.MOVEMENT
    assert classify_activity(frame(6.0), th) is Activity.RAPID_MOVEMENT
    assert classify_activity(frame(50.0), th) is Activity.RAPID_MOVEMENT


def test_custom_thresholds_respected():
    th = MotionThresholds(low=0.5, movement=1.0, rapid=2.0)
    assert classify_activity(frame(1.5), th) is Activity.MOVEMENT
    assert classify_activity(frame(2.5), th) is Activity.RAPID_MOVEMENT


def test_unknown_on_bad_quality_or_missing_data():
    assert classify_activity(frame(10.0, ok=False)) is Activity.UNKNOWN
    assert classify_activity(frame(None)) is Activity.UNKNOWN
    assert classify_activity(None) is Activity.UNKNOWN


def test_classifier_never_reports_potential_fall():
    for s in (0.0, 1.0, 3.0, 10.0, 1000.0):
        assert classify_activity(frame(s)) is not Activity.POTENTIAL_FALL
