import math

import numpy as np
import pytest

from backend.config import ProcessingConfig
from backend.processing import Q_LAYOUT, Q_LOW_RATE, Q_STALE, SignalProcessor
from csi_synth import base_profile, c5c6_packet, classic_packet, stream


def run(proc, gen, **kw):
    frames = []
    for ts, amps in gen:
        f = proc.process(classic_packet(amps, ts, **kw))
        if f is not None:
            frames.append(f)
    return frames


def test_constant_stream_has_near_zero_motion():
    proc = SignalProcessor()
    frames = run(proc, stream(300))
    last = frames[-1]
    assert last.quality_ok, last.quality_reasons
    assert last.variance < 1e-6
    assert last.diff_energy < 1e-6
    assert last.motion_score < 0.01
    assert last.n_subcarriers == 52
    assert last.packet_rate == pytest.approx(100, rel=0.05)


def test_time_varying_perturbation_raises_motion_score():
    quiet = run(SignalProcessor(), stream(400, noise=0.3, seed=1))[-1]
    moving = run(SignalProcessor(), stream(400, noise=0.3, perturb=0.25, seed=1))[-1]
    assert quiet.quality_ok and moving.quality_ok
    assert moving.motion_score > 5 * quiet.motion_score
    assert moving.variance > 5 * quiet.variance
    assert moving.diff_energy > quiet.diff_energy


def test_frames_are_rate_limited():
    proc = SignalProcessor(ProcessingConfig(frame_rate_hz=10))
    frames = run(proc, stream(1000))  # 10 s at 100 Hz
    assert 90 <= len(frames) <= 101


def test_hampel_suppresses_single_spike():
    prof = base_profile()
    proc = SignalProcessor()
    no_filter = SignalProcessor(ProcessingConfig(hampel_window=1))
    for i, (ts, amps) in enumerate(stream(200, noise=0.2, seed=3)):
        if i == 150:
            amps = amps.copy()
            amps[10] = prof[10] * 8  # impulsive interference on one subcarrier
        for p in (proc, no_filter):
            p.process(classic_packet(amps, ts))
        if i == 150:
            spiked = proc.last_profile[10]
            raw_norm = no_filter.last_profile[10]
    assert proc.outliers_replaced >= 1
    assert raw_norm > 4 * spiked  # unfiltered normalised value is far larger
    f_hampel = proc.compute_frame()
    f_raw = no_filter.compute_frame()
    assert f_hampel.variance < f_raw.variance


def test_low_packet_rate_is_insufficient_quality():
    proc = SignalProcessor()
    frames = run(proc, stream(30, rate_hz=5))  # 6 s at 5 Hz
    last = frames[-1]
    assert not last.quality_ok
    assert any(r.startswith(Q_LOW_RATE) for r in last.quality_reasons)


def test_stale_data_is_insufficient_quality():
    proc = SignalProcessor()
    run(proc, stream(200))
    t_last = 1000.0 + 199 / 100.0
    fresh = proc.compute_frame(now=t_last)
    assert fresh.quality_ok
    stale = proc.compute_frame(now=t_last + 5.0)
    assert not stale.quality_ok
    assert any(r.startswith(Q_STALE) for r in stale.quality_reasons)
    assert stale.motion_score is None  # nothing in the window: no invented value


def test_layout_change_resets_cleanly():
    proc = SignalProcessor()
    run(proc, stream(200))
    assert proc.layout == ("classic", 52)
    t = 1000.0 + 2.0
    amps64 = np.full(64, 30.0)
    amps64[:4] = 0  # guard subcarriers: constantly zero, must be dropped
    frames = []
    for i in range(200):
        f = proc.process(c5c6_packet(amps64, t + i / 100.0, seq=i))
        if f is not None:
            frames.append(f)
    assert proc.layout_changes == 1
    assert proc.layout == ("c5c6", 64)
    assert any(r.startswith(Q_LAYOUT) for r in frames[0].quality_reasons)
    last = frames[-1]
    assert last.quality_ok, last.quality_reasons
    assert last.n_subcarriers == 64
    assert np.isnan(proc.last_profile[:4]).all()
    assert np.isfinite(proc.last_profile[4:]).all()
    assert last.variance == pytest.approx(0.0, abs=1e-9)


def test_nan_masked_subcarriers_handled():
    proc = SignalProcessor()
    frames = run(proc, stream(200, noise=0.3, seed=5), first_word=1)
    assert np.isnan(proc.last_profile[0])  # LLTF subcarrier 1 masked
    last = frames[-1]
    assert last.quality_ok
    for v in (last.motion_score, last.variance, last.diff_energy, last.baseline_deviation, last.mean_amplitude):
        assert v is not None and math.isfinite(v)


def test_all_zero_packet_is_skipped_not_crashing():
    proc = SignalProcessor()
    proc.process(classic_packet(np.zeros(52), 1000.0))
    assert proc.invalid_packets == 1
    frame = proc.compute_frame()
    assert not frame.quality_ok and frame.motion_score is None


def test_calibrated_baseline_normalises_empty_room_to_about_one():
    gen_cal = list(stream(500, noise=0.4, seed=7))
    cal = SignalProcessor()
    frames = run(cal, iter(gen_cal))
    good = [f for f in frames if f.quality_ok]
    noise_var = float(np.median([f.variance for f in good]))
    noise_diff = float(np.median([f.diff_energy for f in good]))
    profiles = np.vstack([classic_profile for classic_profile in _profiles(gen_cal)])
    mean, std = profiles.mean(axis=0), profiles.std(axis=0)

    proc = SignalProcessor()
    proc.set_baseline(mean, std, noise_variance=noise_var, noise_diff=noise_diff, name="test")
    empty = run(proc, stream(500, noise=0.4, seed=8))[-1]
    assert empty.calibrated and empty.baseline_source == "calibration"
    assert 0.5 < empty.motion_score < 2.0
    assert empty.baseline_deviation < 3.0

    proc2 = SignalProcessor()
    proc2.set_baseline(mean, std, noise_variance=noise_var, noise_diff=noise_diff)
    moved = run(proc2, stream(500, noise=0.4, seed=8, perturb=0.25))[-1]
    assert moved.motion_score > 5 * empty.motion_score
    assert moved.baseline_deviation > empty.baseline_deviation


def _profiles(gen):
    p = SignalProcessor()
    for ts, amps in gen:
        p.process(classic_packet(amps, ts))
        yield p.last_smoothed.copy()


def test_baseline_with_wrong_layout_is_ignored():
    proc = SignalProcessor()
    proc.set_baseline(np.ones(10), np.ones(10))
    frame = run(proc, stream(200))[-1]
    assert not frame.calibrated and frame.baseline_source == "adaptive"
