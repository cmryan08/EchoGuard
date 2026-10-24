"""Posture model tests on generated (numpy) profiles - not real measurements."""

import json

import numpy as np
import pytest

from backend.posture import (SELF_CHECK_LABEL, SIT_STAND, block_average, classify_profile, fit_posture_model,
                             quality_warnings, score_profile, temporal_self_check, validate_model)

N = 52
DIFF = [4, 11, 27, 40]          # subcarriers where sitting differs from standing


def _base():
    k = np.arange(N)
    return 1.0 + 0.3 * np.sin(k / 5.0)


def _profiles(mean, n=80, noise=0.01, seed=0):
    rng = np.random.default_rng(seed)
    return [mean + rng.normal(0.0, noise, N) for _ in range(n)]


def _classes(separable=True, seed=0):
    base = _base()
    sit = base.copy()
    if separable:
        sit[DIFF] += 0.08
    empty = base * 1.02
    return {"sitting": _profiles(sit, seed=seed + 1), "standing": _profiles(base, seed=seed + 2),
            "empty": _profiles(empty, seed=seed + 3)}


def test_fit_selects_discriminative_subcarriers():
    model = fit_posture_model(_classes(), k=16)
    assert model["classes"] == ["sitting", "standing", "empty"]
    assert set(model["selected"][:4]) == set(DIFF)
    assert len(model["selected"]) == 16 == model["k"]
    assert model["best_fisher"] > 3
    assert model["counts"] == {"sitting": 80, "standing": 80, "empty": 80}
    assert all(len(model["means"][c]) == N == len(model["stds"][c]) for c in model["classes"])
    assert min(v for v in model["stds"]["sitting"] if v is not None) >= model["std_floor"]
    validate_model(model)
    json.dumps(model)


def test_classify_profiles():
    model = fit_posture_model(_classes(), k=16)
    test = _classes(seed=10)
    for cls in ("sitting", "standing"):
        for p in test[cls][:10]:
            label, probs = classify_profile(model, p)
            assert label == cls
            assert abs(sum(probs.values()) - 1.0) < 1e-9 and probs[cls] > 0.9
    ll = score_profile(model, test["sitting"][0])
    assert set(ll) == {"sitting", "standing", "empty"} and ll["sitting"] > ll["standing"]
    label, probs = classify_profile(model, test["standing"][0], SIT_STAND)
    assert label == "standing" and set(probs) == set(SIT_STAND)


def test_nan_safe():
    data = _classes()
    for cls in data:
        for p in data[cls]:
            p[0] = np.nan                    # a masked subcarrier in every profile
    model = fit_posture_model(data, k=16)
    assert model["fisher"][0] is None and 0 not in model["selected"]
    p = _classes(seed=5)["sitting"][0].copy()
    p[DIFF[:2]] = np.nan                     # some selected subcarriers missing
    assert classify_profile(model, p)[0] == "sitting"
    assert classify_profile(model, [None] * N) == (None, {})
    assert classify_profile(model, np.full(N, np.nan)) == (None, {})
    with pytest.raises(ValueError):
        score_profile(model, np.ones(N - 1))


def test_fewer_finite_subcarriers_than_k():
    data = _classes()
    for cls in data:
        for p in data[cls]:
            p[10:] = np.nan
    model = fit_posture_model(data, k=16)
    assert model["k"] == 10 and sorted(model["selected"]) == list(range(10))


def test_fit_rejects_bad_input():
    with pytest.raises(ValueError):
        fit_posture_model({"sitting": _classes()["sitting"]})
    with pytest.raises(ValueError):
        fit_posture_model({"sitting": [np.ones(N)], "standing": [np.ones(N), np.ones(N)]})
    with pytest.raises(ValueError):
        fit_posture_model({"sitting": [np.ones(N)] * 3, "standing": [np.ones(N - 2)] * 3})
    with pytest.raises(ValueError):
        fit_posture_model(_classes(), k=0)
    for bad in (None, {}, {"kind": "diagonal-gaussian"}, {**fit_posture_model(_classes()), "selected": [99]}):
        with pytest.raises(ValueError):
            validate_model(bad)


def test_block_average():
    rows = [np.full(3, float(i)) for i in range(10)]
    blocks = block_average(rows, 4)
    assert len(blocks) == 2 and blocks[0][0] == 1.5 and blocks[1][0] == 5.5
    assert len(block_average(rows[:3], 4)) == 1 and block_average(rows[:3], 4)[0][0] == 1.0
    assert block_average([], 4) == []


def test_self_check_separable():
    sc = temporal_self_check(_classes(), frame_rate_hz=10.0)
    assert sc["label"] == SELF_CHECK_LABEL and "optimistic" in sc["label"]
    assert sc["available"] is True and sc["block_frames"] == 20
    # 80 profiles per class -> 2 blocks per half, 2 folds -> 4 test blocks per class
    assert all(v["blocks"] == 4 for v in sc["per_class"].values())
    assert sc["sit_stand"]["accuracy"] == 1.0 and sc["overall_accuracy"] == 1.0
    model = fit_posture_model(_classes())
    model["self_check"] = sc
    assert quality_warnings(model) == []


def test_self_check_identical_warns():
    data = _classes(separable=False)
    model = fit_posture_model(data)
    model["self_check"] = sc = temporal_self_check(data, frame_rate_hz=10.0)
    assert sc["available"] is True
    assert sc["sit_stand"]["accuracy"] <= 0.75          # near chance (0.5) with 8 test blocks
    assert model["best_fisher"] < 1.0
    w = quality_warnings(model)
    assert len(w) == 1 and "sitting and standing look similar" in w[0] and "recalibrate" in w[0]


def test_self_check_too_few_profiles():
    data = {"sitting": _classes()["sitting"][:3], "standing": _classes()["standing"][:10]}
    sc = temporal_self_check(data)
    assert sc["available"] is False and "too few" in sc["reason"]
