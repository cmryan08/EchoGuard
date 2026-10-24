"""Heuristic fall detector: generated MotionFrame sequences (no hardware)."""

from backend.config import FallConfig, MotionThresholds
from backend.detector import ST_MONITORING, ST_REFRACTORY, ST_SPIKE, ST_STILLNESS, FallDetector
from backend.processing import MotionFrame

TH = MotionThresholds(low=1.5, movement=2.5, rapid=6.0)
RATE = 10.0  # frames per second


def frame(ts, score, ok=True, rate=100.0):
    return MotionFrame(ts=ts, motion_score=score, variance=0.01, diff_energy=0.01, baseline_deviation=1.0,
                       mean_amplitude=30.0, rssi=-45.0, packet_rate=rate, quality_ok=ok,
                       quality_reasons=[] if ok else ["low_packet_rate: test"])


def run(det, segments, t0=100.0, th=TH):
    """segments: list of (seconds, score[, ok[, rate]]). Returns (events, end_ts)."""
    events, t = [], t0
    for seg in segments:
        dur, score = seg[0], seg[1]
        ok = seg[2] if len(seg) > 2 else True
        rate = seg[3] if len(seg) > 3 else 100.0
        for _ in range(int(round(dur * RATE))):
            ev = det.update(frame(t, score, ok, rate), th)
            if ev is not None:
                events.append(ev)
            t = round(t + 1.0 / RATE, 6)
    return events, t


FALL = [(3, 2.0), (0.5, 9.0), (0.5, 4.0), (4, 0.8)]


def test_spike_then_stillness_triggers_with_all_criteria():
    det = FallDetector(FallConfig())
    events, _ = run(det, FALL)
    assert len(events) == 1
    ev = events[0]
    names = [c.name for c in ev.criteria]
    assert names == ["motion_spike", "sudden_rise", "stillness_onset", "stillness_level", "stillness_duration",
                     "data_quality", "packet_rate"]
    assert all(c.passed for c in ev.criteria)
    by = {c.name: c for c in ev.criteria}
    assert by["motion_spike"].value == 9.0 and by["motion_spike"].threshold == 6.0
    assert by["stillness_duration"].value >= 3.0
    assert by["stillness_onset"].value <= 2.0
    assert ev.thresholds["spike_source"] == "thresholds.rapid"
    d = ev.to_dict()
    assert d["passed"] is True and "motion spike" in d["summary"]


def test_spike_without_stillness_does_not_trigger():
    det = FallDetector(FallConfig())
    events, _ = run(det, [(3, 2.0), (0.5, 9.0), (10, 3.5)])   # keeps moving
    assert events == []
    assert "no stillness" in det.last_rejection["reason"]


def test_stillness_without_prior_spike_does_not_trigger():
    det = FallDetector(FallConfig())
    events, _ = run(det, [(5, 3.0), (30, 0.5)])   # sat down calmly, then still for a long time
    assert events == []
    assert det.state() == ST_MONITORING


def test_sustained_rapid_activity_is_not_sudden():
    det = FallDetector(FallConfig())
    # rapid activity from the very start never has a lower baseline before it
    events, _ = run(det, [(3, 7.0), (0.5, 4.0), (5, 0.5)], th=TH)
    assert events == []


def test_stillness_starting_too_late_is_rejected():
    det = FallDetector(FallConfig(max_gap_s=2.0))
    events, _ = run(det, [(3, 2.0), (0.5, 9.0), (2.5, 3.0), (5, 0.5)])
    assert events == []
    assert det.last_rejection is not None


def test_insufficient_quality_blocks_detection():
    det = FallDetector(FallConfig())
    events, _ = run(det, [(3, 2.0), (0.5, 9.0), (0.5, 4.0), (1, 0.8), (0.2, 0.8, False), (4, 0.8)])
    assert events == []
    assert "quality" in det.last_rejection["reason"]
    # low packet rate also counts as insufficient quality
    det2 = FallDetector(FallConfig(min_packet_rate_hz=50))
    events2, _ = run(det2, [(3, 2.0), (0.5, 9.0), (0.5, 4.0), (1, 0.8), (0.3, 0.8, True, 30.0), (4, 0.8)])
    assert events2 == []
    # bad quality before the spike also blocks
    det3 = FallDetector(FallConfig())
    events3, _ = run(det3, [(3, 2.0), (0.3, 2.0, False), (0.5, 9.0), (0.5, 4.0), (4, 0.8)])
    assert events3 == []


def test_refractory_period():
    det = FallDetector(FallConfig(refractory_s=30.0))
    events, _ = run(det, FALL + FALL)            # second fall ~8 s later: suppressed
    assert len(events) == 1
    assert det.state() == ST_REFRACTORY
    events2, _ = run(det, [(25, 0.8)] + FALL, t0=200.0)  # after the refractory window: detected again
    assert len(events2) == 1


def test_config_thresholds_respected():
    # explicit spike threshold above the burst: no event
    det = FallDetector(FallConfig(spike_threshold=10.0))
    assert run(det, FALL)[0] == []
    # longer stillness requirement than available: no event
    det = FallDetector(FallConfig(stillness_duration_s=6.0))
    assert run(det, FALL)[0] == []
    # explicit stillness threshold below the post-event level: no event
    det = FallDetector(FallConfig(stillness_threshold=0.5))
    assert run(det, FALL)[0] == []
    # calibrated thresholds are used when the config says 0 (derive)
    det = FallDetector(FallConfig())
    cal = MotionThresholds(low=1.0, movement=2.0, rapid=12.0, calibrated=True)
    assert run(det, FALL, th=cal)[0] == []
    events = run(FallDetector(FallConfig()), [(3, 2.0), (0.5, 15.0), (0.5, 4.0), (4, 0.8)], th=cal)[0]
    assert len(events) == 1 and events[0].calibrated is True
    # disabled detector never fires
    assert run(FallDetector(FallConfig(enabled=False)), FALL)[0] == []


def test_status_reports_progress():
    det = FallDetector(FallConfig())
    run(det, [(3, 2.0), (0.3, 9.0)])
    st = det.status()
    assert st["state"] == ST_SPIKE and st["candidate"]["peak_score"] == 9.0
    run(det, [(1.0, 0.8)], t0=103.3)
    st = det.status()
    assert st["state"] == ST_STILLNESS
    assert 0.5 <= st["candidate"]["stillness_elapsed_s"] <= 1.0
    assert st["thresholds"]["spike"] == 6.0 and st["thresholds"]["stillness"] == 1.5
