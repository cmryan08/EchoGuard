"""Simulation mode: behaviour only (synthetic data never measures accuracy)."""

import json

import numpy as np
import pytest

from backend.csi_parser import parse_any, valid_subcarrier_amplitudes
from backend.engine import Engine
from backend.monitor import FallMonitor
from simulation import SCENARIOS, SIM_MAC, SimParams, SimulationSource, scenario_segments


def lines(scenario, seconds, **kw):
    src = SimulationSource(SimParams(scenario=scenario, seed=3, **kw), realtime=False, duration_s=seconds, t0=5000.0)
    return src, list(src.lines())


def test_lines_parse_with_official_parser():
    src, out = lines("walking", 2.0)
    kinds = [parse_any(line, ts).kind for ts, line in out]
    assert kinds[0] == "header"
    assert kinds.count("csi") >= 190 and "malformed" not in kinds
    pkt = parse_any(out[5][1], out[5][0]).packet
    assert pkt.mac == SIM_MAC and pkt.mac != "1a:00:00:00:00:00"
    assert pkt.chip_family == "classic" and pkt.len == 128 and pkt.channel == 11
    amps = valid_subcarrier_amplitudes(pkt)
    assert amps.size == 52 and np.isfinite(amps).all() and amps.mean() > 5
    assert src.status.finished and src.status.extra["synthetic"] is True


def test_source_is_marked_synthetic_and_params_validated():
    src = SimulationSource(SimParams(scenario="demo"), realtime=False, duration_s=0.1)
    assert src.kind == "simulation" and src.synthetic is True
    assert src.status.port == "simulation:demo"
    for bad in ({"scenario": "nope"}, {"noise": 1.0}, {"rate_hz": 5}, {"motion_strength": -1},
                {"event_duration_s": 0}):
        with pytest.raises(ValueError):
            SimParams(**{"scenario": "demo", **bad}).validate()
    assert set(SCENARIOS) == {"empty_room", "walking", "sitting", "rapid_movement", "potential_fall", "demo",
                              "desk_day"}
    assert [s.key for s in scenario_segments("potential_fall")] == ["walking", "fall_impact", "lying_still"]


def test_deterministic_with_seed_and_rate():
    _, a = lines("walking", 1.0, rate_hz=50)
    _, b = lines("walking", 1.0, rate_hz=50)
    assert [l for _, l in a] == [l for _, l in b]
    assert 45 <= len(a) <= 52


def test_motion_strength_increases_variability():
    def spread(**kw):
        _, out = lines("walking", 4.0, **kw)
        A = np.array([valid_subcarrier_amplitudes(parse_any(l, t).packet) for t, l in out[1:]])
        A = A / A.mean(axis=1, keepdims=True)
        return float(np.mean(np.std(A, axis=0)))
    assert spread(motion_strength=2.0) > 2 * spread(motion_strength=0.0)


def run_engine(scenario, seconds):
    eng = Engine()
    mon = FallMonitor(eng)
    eng.start(SimulationSource(SimParams(scenario=scenario, seed=7), realtime=False, duration_s=seconds))
    assert eng.join(60)
    return eng, mon


def test_potential_fall_triggers_end_to_end():
    eng, mon = run_engine("potential_fall", 12.0)
    snap = eng.snapshot()
    assert snap["source"]["kind"] == "simulation" and snap["source"]["synthetic"] is True
    assert len(mon.fall_events) == 1
    ev = mon.fall_events[0]
    assert ev["synthetic"] is True and ev["source_kind"] == "simulation"
    assert all(c["passed"] for c in ev["criteria"])
    al = snap["alert"]
    assert al["state"] == "active" and al["alert"]["synthetic"] is True
    assert snap["activity"]["key"] == "potential_fall"
    assert any(e["kind"] == "alert" for e in mon.events.recent())
    json.dumps(snap)


def test_empty_room_does_not_trigger():
    eng, mon = run_engine("empty_room", 30.0)
    assert mon.fall_events == []
    assert eng.snapshot()["alert"]["state"] == "idle"
    acts = [a for _, a in eng.frames()]
    assert acts.count("empty_low") > 0.9 * len(acts)


@pytest.mark.parametrize("scenario", ["walking", "sitting", "rapid_movement"])
def test_other_scenarios_do_not_trigger(scenario):
    _, mon = run_engine(scenario, 30.0)
    assert mon.fall_events == []


def test_desk_day_segments_are_synthetic_and_short():
    segs = scenario_segments("desk_day")
    keys = [s.key for s in segs]
    assert {"empty_room", "walking", "standing_still", "sitting_down", "sitting", "standing_up"} == set(keys)
    sits = [s.duration_s for s in segs if s.key == "sitting"]
    assert len(sits) == 2 and all(60 <= d <= 90 for d in sits)
    assert "SYNTHETIC" in SCENARIOS["desk_day"]
    assert sum(s.duration_s for s in segs) < 400


def test_sitting_and_standing_have_different_static_multipath():
    def mean_profile(key):
        sim = CSISimulator(SimParams(scenario="desk_day", seed=1, noise=0.0))
        start = 0.0
        for s in sim.segments:
            if s.key == key:
                break
            start += s.duration_s
        A = np.array([np.abs(sim.csi(start + 5 + i * 0.1)[0][sim.valid]) for i in range(50)])
        A = A / A.mean(axis=1, keepdims=True)
        return A.mean(axis=0)
    diff = np.abs(mean_profile("sitting") - mean_profile("standing_still"))
    assert diff.max() > 0.05


def test_synthetic_calibration_separates_sitting_and_standing():
    """Calibration on the simulator yields a posture model whose (within-calibration,
    optimistic) self-check separates sitting and standing. Synthetic only."""
    r = synthetic_calibration(SimParams(seed=4))
    assert r["synthetic"] is True and r["source_kind"] == "simulation"
    pm = r["posture_model"]
    assert pm is not None and pm["best_fisher"] >= 1.0
    sc = pm["self_check"]
    assert sc["available"] is True and sc["sit_stand"]["accuracy"] >= 0.9
    assert not any("look similar" in w for w in r["warnings"])
    assert r["thresholds"]["calibrated"] is True
