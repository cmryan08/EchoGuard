"""Calibration tests with generated (numpy) CSI streams - not real measurements."""

import dataclasses
import json
import math
import threading

import numpy as np
import pytest

from backend.calibration import (CALIBRATION_FORMAT, DONE_CUE, MAX_GRACE_S, MAX_LEAD_IN_S, MAX_TRANSITION_S,
                                 STEP_BY_KEY, STEP_KEYS, STEPS, CalibrationError, CalibrationManager,
                                 CalibrationSession, CalibrationStore, StepData, build_timeline,
                                 compute_calibration, mmss, sensor_key, sensor_part)
from backend.config import ProcessingConfig
from backend.engine import Engine
from backend.processing import SignalProcessor
from backend.sources import ReplaySource
from csi_synth import TX_MAC, GatedReplay, base_profile, classic_line, classic_packet, stream

SENSOR = {"tx_mac": TX_MAC, "channel": 11, "secondary_channel": 2, "chip_family": "classic", "csi_len": 128,
          "n_subcarriers": 52}

# step -> generated stream parameters (relative multipath perturbation, noise)
SEPARABLE = {
    "empty_baseline": dict(noise=0.4, perturb=0.0),
    "walking": dict(noise=0.4, perturb=0.12, perturb_hz=1.2),
    "standing_still": dict(noise=0.4, perturb=0.004, perturb_hz=0.3),
    "sitting": dict(noise=0.4, perturb=0.03, perturb_hz=0.8),
}
INSEPARABLE = {k: dict(noise=0.4, perturb=0.0) for k in STEP_KEYS}
DURATIONS = {"empty_baseline": 15, "walking": 5, "standing_still": 5, "sitting": 5}


class Feeder:
    """Runs generated packets through a real SignalProcessor and hands each
    frame + smoothed profile to the session (like the engine hook does)."""

    def __init__(self, session, rate_hz=100.0, synthetic=False):
        self.session = session
        self.proc = SignalProcessor(ProcessingConfig())
        self.t = 1000.0
        self.rate = rate_hz
        self.synthetic = synthetic
        self.seed = 0

    def run(self, seconds, **kw):
        n = int(seconds * self.rate)
        self.seed += 1
        for ts, amps in stream(n, rate_hz=self.rate, t0=self.t, seed=self.seed, **kw):
            frame = self.proc.process(classic_packet(amps, ts))
            if frame is not None:
                self.session.add_frame(frame, self.proc.last_smoothed, self.proc.layout, sensor=SENSOR,
                                       synthetic=self.synthetic, source_kind="replay")
        self.t += n / self.rate + 0.01


def run_all(session, feeder, params, extra_s=1.6):
    for key in STEP_KEYS:
        assert session.current.key == key
        session.start_step()
        feeder.run(session.steps[key].duration_s + extra_s, **params[key])
    return session


def test_step_definitions():
    assert [s.key for s in STEPS] == ["empty_baseline", "walking", "standing_still", "sitting"]
    empty = STEPS[0]
    assert (empty.min_s, empty.default_s, empty.max_s) == (15, 20, 30)
    assert [(s.default_s, s.min_s, s.max_s) for s in STEPS[1:]] == [(15, 5, 60), (20, 5, 60), (20, 5, 60)]
    assert all(s.cue and s.cue.endswith(".") for s in STEPS)
    assert "normally" in STEP_BY_KEY["standing_still"].instructions
    assert "chair you normally use" in STEP_BY_KEY["sitting"].instructions
    s = CalibrationSession("Living room")
    assert s.placement == "living-room"
    assert s.steps["empty_baseline"].duration_s == 20
    assert (s.lead_in_s, s.grace_s, s.transition_s, s.auto) == (15, 10, 5, True)
    assert "t_last" in {f.name for f in dataclasses.fields(StepData)}
    assert StepData("walking", 10.0).t_last is None
    with pytest.raises(CalibrationError):
        CalibrationSession("x", {"empty_baseline": 10})
    with pytest.raises(CalibrationError):
        CalibrationSession("x", {"empty_baseline": 31})
    with pytest.raises(CalibrationError):
        CalibrationSession("x", {"bogus": 10})
    with pytest.raises(ValueError):
        CalibrationSession("../etc")


def test_separable_streams_give_ordered_thresholds():
    s = CalibrationSession("lab", DURATIONS)
    run_all(s, Feeder(s), SEPARABLE)
    assert s.state == "computed", s.error
    r = s.result
    th = r["thresholds"]
    assert 0 < th["low"] < th["movement"] < th["rapid"]
    assert th["calibrated"] is True
    assert r["separable"] is True
    assert not any("not clearly separable" in w for w in r["warnings"])
    st = r["steps"]
    empty_med = st["empty_baseline"]["stats"]["motion_score"]["median"]
    walk_med = st["walking"]["stats"]["motion_score"]["median"]
    assert 0.7 < empty_med < 1.5          # calibrated empty room scores about 1
    assert walk_med > th["movement"] > st["empty_baseline"]["stats"]["motion_score"]["p95"]
    assert st["standing_still"]["stats"]["motion_score"]["median"] < th["movement"]
    assert len(r["baseline"]["mean_profile"]) == 52 == len(r["baseline"]["std_profile"])
    assert r["noise_variance"] > 0 and r["noise_diff"] > 0
    assert r["noise"]["variance"]["p95"] >= r["noise"]["variance"]["median"]
    assert r["synthetic"] is False and r["sensor"] == SENSOR
    assert r["format"] == CALIBRATION_FORMAT == 2
    pm = r["posture_model"]
    assert set(pm["means"]) == set(pm["stds"]) == {"sitting", "standing", "empty"}
    assert all(len(v) == 52 for v in pm["means"].values())
    assert len(pm["selected"]) == 16 == pm["k"] and len(set(pm["selected"])) == 16
    assert len(pm["fisher"]) == 52
    sc = pm["self_check"]
    assert "within-calibration" in sc["label"] and "optimistic" in sc["label"]
    assert sc["available"] is True and set(sc["per_class"]) == {"sitting", "standing", "empty"}
    assert "within-calibration" in pm["note"]
    json.dumps(r)


def test_inseparable_data_warns_and_falls_back():
    s = CalibrationSession("lab", DURATIONS)
    run_all(s, Feeder(s), INSEPARABLE)
    assert s.state == "computed", s.error
    r = s.result
    assert r["separable"] is False
    assert any("not clearly separable" in w for w in r["warnings"])
    th = r["thresholds"]
    assert 0 < th["low"] < th["movement"] < th["rapid"]
    assert th["movement"] >= 2.5 and th["rapid"] >= 6.0


def test_insufficient_quality_refuses():
    s = CalibrationSession("lab", DURATIONS)
    f = Feeder(s, rate_hz=10.0)                 # below the 20 Hz quality gate
    s.start_step()
    f.run(17, **SEPARABLE["empty_baseline"])
    sd = s.steps["empty_baseline"]
    assert sd.status == "failed"
    assert "sufficient data quality" in sd.failure and "low_packet_rate" in sd.failure
    assert s.state == "ready" and s.current.key == "empty_baseline"
    with pytest.raises(CalibrationError, match="cannot compute"):
        compute_calibration(s.steps, "lab")
    # redo with good data proceeds to the next step
    s.start_step()
    Feeder(s).run(16.6, **SEPARABLE["empty_baseline"])
    assert s.steps["empty_baseline"].status == "done"
    assert s.current.key == "walking"


def test_redo_and_cancel_step():
    s = CalibrationSession("lab", DURATIONS)
    f = Feeder(s)
    s.start_step()
    f.run(3, **SEPARABLE["empty_baseline"])
    s.cancel_step()
    assert s.state == "ready" and s.steps["empty_baseline"].total == 0
    run_all(s, f, SEPARABLE)
    assert s.state == "computed"
    s.redo("walking")
    assert s.state == "ready" and s.current.key == "walking" and s.result is None
    s.start_step()
    f.run(6.6, **SEPARABLE["walking"])
    assert s.state == "computed"            # the other steps are kept


def test_timeout_without_data():
    now = [0.0]
    s = CalibrationSession("lab", DURATIONS, clock=lambda: now[0])
    s.start_step()
    now[0] = 100.0
    s.check_timeout()
    assert s.steps["empty_baseline"].status == "failed"
    assert "no motion frames" in s.steps["empty_baseline"].failure


def test_sensor_key():
    k = sensor_key(SENSOR, "Living room")
    assert k == "living-room__classic_1a0000000000_ch11-2_len128"
    assert sensor_key(SENSOR, "x", synthetic=True).endswith("__synthetic")
    assert sensor_part({**SENSOR, "channel": 6}) != sensor_part(SENSOR)
    assert sensor_part(None) is None
    with pytest.raises(CalibrationError):
        sensor_key({}, "x")


def _result(placement="lab", synthetic=False, sensor=SENSOR):
    s = CalibrationSession(placement, DURATIONS)
    run_all(s, Feeder(s, synthetic=synthetic), SEPARABLE)
    r = dict(s.result)
    r["sensor"] = sensor
    r["synthetic"] = synthetic
    r["key"] = sensor_key(sensor, placement, synthetic)
    return r


@pytest.fixture(scope="module")
def real_result():
    return _result()


def test_store_round_trip_and_matching(tmp_path, real_result):
    store = CalibrationStore(tmp_path / "calibration")
    path = store.save(real_result)
    assert path.name == "lab__classic_1a0000000000_ch11-2_len128.json"
    loaded = store.load(real_result["key"])
    assert loaded["thresholds"] == real_result["thresholds"]
    assert loaded["baseline"]["mean_profile"] == real_result["baseline"]["mean_profile"]
    listing = store.list()
    assert [c["key"] for c in listing] == [real_result["key"]]
    assert store.find_match(SENSOR, synthetic=False)["key"] == real_result["key"]
    assert store.find_match({**SENSOR, "channel": 6}, synthetic=False) is None
    assert store.find_match(SENSOR, synthetic=True) is None
    for bad in ("../x", "a/b", "", "X Y", "..", 5):
        with pytest.raises(CalibrationError):
            store.load(bad)
    store.delete(real_result["key"])
    assert store.list() == []


def _write_log(path, n=600, rate=100.0, perturb=0.0):
    with open(path, "w") as f:
        for i, (ts, amps) in enumerate(stream(n, rate_hz=rate, noise=0.4, perturb=perturb, seed=3)):
            f.write(classic_line(amps, seq=i % 65536, ts_us=int(i * 1e6 / rate)) + "\n")


def _engine_with_manager(tmp_path):
    eng = Engine()
    mgr = CalibrationManager(eng, CalibrationStore(tmp_path / "calibration"))
    return eng, mgr


def test_auto_load_matching_calibration(tmp_path, real_result):
    CalibrationStore(tmp_path / "calibration").save(real_result)
    log = tmp_path / "gen.txt"
    _write_log(log)
    eng, mgr = _engine_with_manager(tmp_path)
    try:
        eng.start(ReplaySource(log, speed=0))
        assert eng.join(10)
        snap = eng.snapshot()
        assert snap["calibration"]["key"] == real_result["key"]
        assert snap["calibration"]["active"] is True
        assert snap["calibration"]["auto_loaded"] is True
        assert snap["thresholds"]["calibrated"] is True
        assert snap["thresholds"]["movement"] == real_result["thresholds"]["movement"]
        assert snap["motion"]["calibrated"] is True
        assert mgr.last_auto["action"] == "loaded"
    finally:
        eng.stop()


def test_synthetic_calibration_not_auto_applied_to_real_source(tmp_path):
    synth = _result(synthetic=True)
    # same sensor and placement, but computed from synthetic data
    store = CalibrationStore(tmp_path / "calibration")
    store.save(synth)
    log = tmp_path / "gen.txt"
    _write_log(log)
    eng, mgr = _engine_with_manager(tmp_path)
    try:
        eng.start(ReplaySource(log, speed=0))
        assert eng.join(10)
        snap = eng.snapshot()
        assert snap["calibration"]["key"] is None
        assert snap["calibration"]["active"] is False
        assert snap["thresholds"]["calibrated"] is False
        with pytest.raises(CalibrationError, match="SYNTHETIC"):
            mgr.load(synth["key"])
    finally:
        eng.stop()


def test_mismatched_calibration_cleared_on_new_sensor(tmp_path, real_result):
    other = dict(real_result)
    other["sensor"] = {**SENSOR, "channel": 6}
    other["key"] = sensor_key(other["sensor"], "lab")
    store = CalibrationStore(tmp_path / "calibration")
    store.save(other)
    log = tmp_path / "gen.txt"
    _write_log(log)
    eng, mgr = _engine_with_manager(tmp_path)
    try:
        res = mgr.load(other["key"])                       # manual load before any source: allowed
        assert res["key"] == other["key"]
        assert eng.calibration_info["key"] == other["key"]
        eng.start(ReplaySource(log, speed=0))
        assert eng.join(10)
        assert eng.calibration_info is None                # channel 11 data: does not match channel 6
        assert mgr.last_auto["action"] == "cleared"
    finally:
        eng.stop()


def test_manager_full_flow_with_engine(tmp_path):
    """Calibration driven by the engine thread from a replayed generated log."""
    log = tmp_path / "cal.txt"
    rate = 100.0
    with open(log, "w") as f:
        i = 0
        for key, secs in (("empty_baseline", 18), ("walking", 8)):
            for ts, amps in stream(int(secs * rate), rate_hz=rate, seed=i, **SEPARABLE[key]):
                f.write(classic_line(amps, seq=i % 65536) + "\n")
                i += 1
    eng, mgr = _engine_with_manager(tmp_path)
    gate = threading.Event()
    try:
        mgr.start("Test bench", DURATIONS, lead_in_s=0, auto=False)
        with pytest.raises(CalibrationError, match="no data source"):
            mgr.step_start()
        # speed=0: emitted as fast as possible but host timestamps keep the
        # original 100 lines/s timeline, so packet-time step durations hold.
        eng.start(GatedReplay(log, gate, speed=0, line_rate_hz=rate))
        mgr.step_start()
        assert mgr.state()["session"]["state"] == "recording"
        gate.set()
        assert eng.join(15)
        state = mgr.state()["session"]
        assert state["steps"][0]["status"] == "done", state["steps"][0]
        assert state["steps"][0]["ok_frames"] >= 140
        # the source ended during the next step's wait -> walking not started yet
        assert state["current_step"] == "walking" and state["state"] == "ready"
    finally:
        gate.set()
        eng.stop()


def test_manager_save_load_delete(tmp_path):
    eng, mgr = _engine_with_manager(tmp_path)
    mgr.start("Bedroom", DURATIONS)
    with pytest.raises(CalibrationError, match="nothing to save"):
        mgr.save()
    s = mgr.session
    run_all(s, Feeder(s), SEPARABLE)
    assert s.state == "computed"
    saved = mgr.save()
    assert saved["key"] == "bedroom__classic_1a0000000000_ch11-2_len128"
    assert (tmp_path / "calibration" / (saved["key"] + ".json")).is_file()
    assert eng.calibration_info["key"] == saved["key"]
    assert eng.config.thresholds.calibrated is True
    assert eng.calibration_info["posture_model"] is True
    assert eng.posture_model is not None and mgr.posture_model is eng.posture_model
    assert eng.posture_model["selected"] == s.result["posture_model"]["selected"]
    assert mgr.state()["session"]["state"] == "saved"
    assert [c["key"] for c in mgr.store.list()] == [saved["key"]]
    mgr.clear()
    assert eng.calibration_info is None and eng.config.thresholds.calibrated is False
    assert eng.posture_model is None
    mgr.load(saved["key"])
    assert eng.processor.baseline is not None
    assert eng.posture_model is not None
    mgr.delete(saved["key"])
    assert eng.calibration_info is None and mgr.store.list() == []
    assert eng.posture_model is None
    assert np.isfinite(eng.config.thresholds.movement)


def test_format1_calibration_still_loads(tmp_path, real_result):
    old = {k: v for k, v in real_result.items() if k != "posture_model"}
    old["format"] = 1
    eng, mgr = _engine_with_manager(tmp_path)
    mgr.store.save(old)
    res = mgr.load(old["key"])
    assert res["key"] == old["key"]
    assert eng.calibration_info["key"] == old["key"] and eng.calibration_info["posture_model"] is False
    assert eng.config.thresholds.calibrated is True
    assert eng.posture_model is None and mgr.posture_model is None
    # a broken posture model is ignored, the rest of the calibration still applies
    broken = dict(real_result, posture_model={"kind": "diagonal-gaussian", "selected": [999]})
    mgr.store.save(broken)
    mgr.load(broken["key"])
    assert eng.posture_model is None and eng.config.thresholds.calibrated is True


# ------------------------------------------------------- timed run sequence
DEFAULT_ORDER = [("lead_in", "empty_baseline"), ("step", "empty_baseline"), ("grace", "walking"),
                 ("step", "walking"), ("transition", "standing_still"), ("step", "standing_still"),
                 ("transition", "sitting"), ("step", "sitting")]


def test_build_timeline_defaults_total_114s():
    s = CalibrationSession("lab", settle_s=1.0)
    tl = s.timeline()
    ph = tl["phases"]
    assert [(p["kind"], p["key"]) for p in ph] == DEFAULT_ORDER
    assert tl["total_s"] == 15 + 21 + 10 + 16 + 5 + 21 + 5 + 21 == 114
    assert tl["total"] == "01:54" and tl["settle_s"] == 1.0 and tl["done_cue"] == DONE_CUE
    assert [p["when"] for p in ph] == ["00:00", "00:15", "00:36", "00:46", "01:02", "01:07", "01:28", "01:33"]
    assert [p["length_s"] for p in ph] == [15, 21, 10, 16, 5, 21, 5, 21]
    assert ph[-1]["until"] == "01:54"
    for a, b in zip(ph, ph[1:]):
        assert a["end_s"] == b["start_s"]
    assert all(p["cue"] and p["instruction"] and p["title"] for p in ph)
    assert ph[0]["title"] == "Leave the room now" and ph[0]["cue"].startswith("Leave the room now.")
    assert ph[1]["cue"] == "Recording the empty room. Stay outside."
    assert ph[2]["title"] == "Come back in" and ph[2]["cue"].startswith("Come back in.")
    assert ph[4]["cue"] == "Stop walking and stand still. Recording starts in 5 seconds."
    assert ph[6]["cue"] == "Sit down on your chair now. Recording starts in 5 seconds."
    assert ph[3]["cue"] == "Come in and walk back and forth between the two boards."
    # the same schedule straight from build_timeline
    durations = {k: STEP_BY_KEY[k].default_s for k in STEP_KEYS}
    assert build_timeline(STEP_KEYS, durations, 5, 5, 1.0, 3) == tl


def test_build_timeline_without_waits_and_mmss():
    durations = {k: STEP_BY_KEY[k].default_s for k in STEP_KEYS}
    tl = build_timeline(STEP_KEYS, durations)
    assert [p["kind"] for p in tl["phases"]] == ["step"] * 4
    assert tl["total_s"] == 75
    tl = build_timeline(["sitting"], durations, lead_in_s=10, settle_s=1.0)
    assert [(p["kind"], p["key"]) for p in tl["phases"]] == [("lead_in", "sitting"), ("step", "sitting")]
    assert tl["phases"][0]["title"] == "Get ready" and tl["total_s"] == 31
    assert (mmss(0), mmss(5.4), mmss(65), mmss(600)) == ("00:00", "00:05", "01:05", "10:00")


@pytest.mark.parametrize("name,hi", [("lead_in_s", MAX_LEAD_IN_S), ("grace_s", MAX_GRACE_S),
                                     ("transition_s", MAX_TRANSITION_S)])
def test_wait_validation(name, hi):
    assert MAX_TRANSITION_S == 30
    for bad in (-1, -0.01, hi + 0.5, True, False, math.nan, math.inf, "5", None):
        with pytest.raises(CalibrationError):
            CalibrationSession("lab", **{name: bad})
    for ok in (0, 2.5, hi):
        assert getattr(CalibrationSession("lab", **{name: ok}), name) == ok
    with pytest.raises(CalibrationError):
        CalibrationSession("lab", auto="yes")


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class StateLog(list):
    def note(self, session):
        if not self or self[-1] != session.state:
            self.append(session.state)


def test_auto_run_with_fake_clock():
    clk = FakeClock()
    s = CalibrationSession("lab", DURATIONS, clock=clk, settle_s=1.0, lead_in_s=5, grace_s=5, transition_s=3)
    f = Feeder(s)
    log = StateLog()
    log.note(s)
    total = s.timeline()["total_s"]
    assert total == 5 + 16 + 5 + 6 + 3 + 6 + 3 + 6
    s.begin()
    log.note(s)
    ph = s.phase()
    assert ph["kind"] == "lead_in" and ph["key"] == "empty_baseline" and ph["remaining_s"] == 5
    assert ph["run_elapsed_s"] == 0 and ph["run_remaining_s"] == total
    assert ph["next_kind"] == "step" and ph["next_cue"] == STEP_BY_KEY["empty_baseline"].cue
    assert ph["cue"].startswith("Leave the room now")
    # frames during the lead-in are ignored
    f.run(2, **SEPARABLE["empty_baseline"])
    assert s.state == "waiting" and s.steps["empty_baseline"].total == 0
    clk.t = 2.0
    d = s.to_dict()
    assert d["phase"]["remaining_s"] == 3 and d["run_elapsed_s"] == 2 and d["run_remaining_s"] == total - 2
    assert d["transition_s"] == 3 and d["running"] is True and d["timeline"]["total_s"] == total
    # the wait ends on the wall clock without any frame
    clk.t = 5.0
    s.check_timeout()
    log.note(s)
    assert s.state == "recording" and s.current.key == "empty_baseline"

    expected_waits = {"empty_baseline": ("grace", "walking", 5), "walking": ("transition", "standing_still", 3),
                      "standing_still": ("transition", "sitting", 3)}
    for key in STEP_KEYS:
        assert s.state == "recording" and s.current.key == key
        assert s.phase()["kind"] == "step" and s.phase()["key"] == key
        f.run(s.steps[key].duration_s + 1.6, **SEPARABLE[key])
        log.note(s)
        assert s.steps[key].status == "done", s.steps[key].failure
        if key == "sitting":
            break
        kind, nxt, wait = expected_waits[key]
        assert s.state == "waiting" and s.wait_kind == kind and s.current.key == nxt
        ph = s.phase()
        assert ph["kind"] == kind and ph["key"] == nxt and ph["remaining_s"] == wait
        assert ph["next_kind"] == "step" and ph["next_key"] == nxt
        if kind == "transition":
            assert ph["cue"].endswith(f"Recording starts in {wait} seconds.")
        f.run(1.0, **SEPARABLE[nxt])                  # ignored while waiting
        assert s.steps[nxt].total == 0 and s.state == "waiting"
        clk.t += wait
        s.tick()
        log.note(s)
    assert log == ["ready", "waiting", "recording", "waiting", "recording", "waiting", "recording",
                   "waiting", "recording", "computed"]
    assert s.running is False and s.phase() is None
    r = s.result
    assert r["posture_model"] is not None and r["format"] == 2
    assert all(s.steps[k].settle_s == 1.0 for k in STEP_KEYS)


def test_last_step_announces_done():
    clk = FakeClock()
    s = CalibrationSession("lab", DURATIONS, clock=clk, lead_in_s=0, grace_s=0, transition_s=0)
    f = Feeder(s)
    s.begin()
    assert s.state == "recording"            # no lead-in: records at once
    for key in STEP_KEYS[:-1]:
        f.run(s.steps[key].duration_s + 1.6, **SEPARABLE[key])
        assert s.state == "recording"        # chained back to back
    ph = s.phase()
    assert ph["key"] == "sitting" and ph["next_kind"] == "done" and ph["next_cue"] == DONE_CUE


def test_failed_step_stops_auto_run():
    clk = FakeClock()
    s = CalibrationSession("lab", DURATIONS, clock=clk, lead_in_s=5, grace_s=5, transition_s=3)
    f = Feeder(s)
    s.begin()
    clk.t = 5.0
    s.tick()
    f.run(DURATIONS["empty_baseline"] + 1.6, **SEPARABLE["empty_baseline"])
    assert s.state == "waiting" and s.wait_kind == "grace"
    clk.t += 5.0
    s.check_timeout()
    assert s.state == "recording" and s.current.key == "walking"
    clk.t += 100.0                            # no walking frames at all
    s.check_timeout()
    assert s.steps["walking"].status == "failed" and "timed out" in s.steps["walking"].failure
    assert s.state == "ready" and s.running is False and s.current.key == "walking"
    assert s.phase() is None and s.to_dict()["run_remaining_s"] is None
    f.run(3.0, **SEPARABLE["walking"])        # nothing records until the user starts again
    assert s.steps["walking"].total == 0 and s.steps["standing_still"].status == "pending"
    # restarting resumes with the failed step
    assert [p["key"] for p in s.timeline()["phases"]][:2] == ["walking", "walking"]
    s.begin()
    assert s.phase()["kind"] == "lead_in" and s.phase()["key"] == "walking"


def test_cancel_during_wait():
    clk = FakeClock()
    s = CalibrationSession("lab", DURATIONS, clock=clk)
    s.begin()
    assert s.state == "waiting"
    s.cancel_step()
    assert s.state == "ready" and s.running is False
    clk.t = 10.0
    s.tick()
    assert s.state == "ready"


# ---------------------------------------------------------- posture model
SIT_BUMP = np.zeros(52)
SIT_BUMP[[8, 9, 30, 31, 44]] = 0.15
POSTURE_SEPARABLE = {**SEPARABLE, "sitting": dict(SEPARABLE["sitting"], profile=base_profile() * (1 + SIT_BUMP))}


def test_posture_model_separates_sitting_and_standing():
    s = CalibrationSession("lab", DURATIONS)
    run_all(s, Feeder(s), POSTURE_SEPARABLE)
    assert s.state == "computed", s.error
    pm = s.result["posture_model"]
    assert {8, 9, 30, 31, 44} <= set(pm["selected"][:8])
    assert pm["best_fisher"] > 1.0
    assert pm["self_check"]["sit_stand"]["accuracy"] == 1.0
    assert not any("look similar" in w for w in s.result["warnings"])


def test_posture_model_warns_when_identical():
    s = CalibrationSession("lab", DURATIONS)
    run_all(s, Feeder(s), INSEPARABLE)
    assert s.state == "computed", s.error
    pm = s.result["posture_model"]
    assert pm["best_fisher"] < 1.0
    assert any("sitting and standing look similar" in w for w in s.result["warnings"])
