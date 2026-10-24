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
    assert (s.lead_in_s, s.grace_s, s.transition_s, s.auto) == (5, 5, 3, True)
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
