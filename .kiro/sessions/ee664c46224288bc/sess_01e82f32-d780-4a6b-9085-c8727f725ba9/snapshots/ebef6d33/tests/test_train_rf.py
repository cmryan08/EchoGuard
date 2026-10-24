"""Random Forest tooling: session-grouped splits and data policy.

The training run itself needs scikit-learn (optional, requirements-ml.txt) and
is skipped when it is not installed. All data here is generated in tmp_path.
"""

import numpy as np
import pytest

from backend.ml import train_rf
from backend.ml.train_rf import PolicyError, apply_policy, confusion_and_scores, group_kfold, load_dataset
from backend.recording import SessionStore, TakeWriter
from csi_synth import classic_line, stream
from simulation import SimParams, SimulationSource


def _add_take(store_session, index, label, lines):
    s = store_session
    fname = f"{index:03d}_{label}_test.csv"
    w = TakeWriter(s.dir / fname, s.session_id, f"t{index:03d}", label)
    for ts, line in lines:
        w.write(ts, line)
    w.close()
    s.meta["takes"].append({"take_id": f"t{index:03d}", "index": index, "label": label, "file": fname})
    s.save()


def make_real_like_session(root, placement, seed=0, now=None):
    """A session whose lines use the real TX MAC (generated test signal, not a capture)."""
    store = SessionStore(root)
    s = store.create(placement, synthetic=False, source_kind="live", now=now)
    quiet = [(t, classic_line(a, seq=i)) for i, (t, a) in enumerate(stream(400, noise=0.4, seed=seed))]
    busy = [(t, classic_line(a, seq=i)) for i, (t, a) in
            enumerate(stream(400, noise=0.4, perturb=0.3, perturb_hz=2.0, seed=seed + 1, t0=2000.0))]
    _add_take(s, 1, "empty_room", quiet)
    _add_take(s, 2, "walking", busy)
    return s


def make_sim_session(root, placement, seed=0, now=None, seconds=8.0):
    store = SessionStore(root)
    s = store.create(placement, synthetic=True, source_kind="simulation", now=now)
    for i, (label, scen) in enumerate((("empty_room", "empty_room"), ("walking", "walking"),
                                       ("simulated_fall", "potential_fall")), start=1):
        src = SimulationSource(SimParams(scenario=scen, seed=seed + i), realtime=False, duration_s=seconds,
                               t0=1000.0 * i)
        _add_take(s, i, label, list(src.lines()))
    return s


def test_group_kfold_never_mixes_sessions():
    rng = np.random.default_rng(0)
    groups = np.array(rng.choice(["s1", "s2", "s3", "s4", "s5"], size=200), dtype=object)
    seen = []
    for train, test in group_kfold(groups, 3):
        assert not set(groups[train]) & set(groups[test])
        assert len(train) + len(test) == len(groups)
        seen += list(set(groups[test]))
    assert sorted(seen) == ["s1", "s2", "s3", "s4", "s5"]   # every session held out exactly once
    folds = list(group_kfold(np.array(["a"] * 5 + ["b"] * 3, dtype=object), 10))
    assert len(folds) == 2                                     # leave-one-session-out
    with pytest.raises(PolicyError):
        list(group_kfold(np.array(["a"] * 5, dtype=object), 3))


def test_confusion_and_scores():
    out = confusion_and_scores(["a", "a", "b", "b"], ["a", "b", "b", "b"], ["a", "b"])
    assert out["confusion_matrix"] == [[1, 1], [0, 2]]
    assert out["per_class"]["a"]["recall"] == 0.5 and out["per_class"]["b"]["precision"] == pytest.approx(2 / 3)


def test_refuses_single_real_session(tmp_path):
    make_real_like_session(tmp_path, "lab")
    ds = load_dataset(tmp_path, log=lambda *_: None)
    assert len(ds.sessions) == 1 and not ds.synthetic_sessions
    with pytest.raises(PolicyError, match="at least 2 real"):
        apply_policy(ds)


def test_refuses_synthetic_without_flag(tmp_path):
    make_sim_session(tmp_path, "sim-a", now=1_700_000_000)
    make_sim_session(tmp_path, "sim-b", seed=10, now=1_700_000_100)
    ds = load_dataset(tmp_path, log=lambda *_: None)
    assert len(ds.synthetic_sessions) == 2
    with pytest.raises(PolicyError, match="SYNTHETIC"):
        apply_policy(ds)
    with pytest.raises(PolicyError):
        apply_policy(ds, exclude_synthetic=True)   # nothing real left
    assert apply_policy(ds, allow_synthetic=True) is ds


def test_simulator_mac_marks_session_synthetic(tmp_path):
    # session.json claims real data but the packets come from the simulator MAC
    store = SessionStore(tmp_path)
    s = store.create("fake", synthetic=False, source_kind="live")
    src = SimulationSource(SimParams(scenario="walking", seed=1), realtime=False, duration_s=5)
    _add_take(s, 1, "walking", list(src.lines()))
    ds = load_dataset(tmp_path, log=lambda *_: None)
    assert ds.synthetic_sessions == [s.session_id]


def test_two_real_sessions_pass_policy(tmp_path):
    make_real_like_session(tmp_path, "room-a", now=1_700_000_000)
    make_real_like_session(tmp_path, "room-b", seed=5, now=1_700_000_100)
    ds = apply_policy(load_dataset(tmp_path, log=lambda *_: None))
    assert len(ds.real_sessions) == 2
    assert set(ds.y.tolist()) == {"empty_room", "walking"}
    assert ds.X.shape[1] == len(train_rf.feature_names())


def test_cli_refuses_without_flag(tmp_path, capsys):
    pytest.importorskip("sklearn")
    make_sim_session(tmp_path / "rec", "sim-a", now=1_700_000_000)
    make_sim_session(tmp_path / "rec", "sim-b", seed=10, now=1_700_000_100)
    rc = train_rf.main(["--recordings", str(tmp_path / "rec"), "--out-dir", str(tmp_path / "models")])
    assert rc == 2
    assert "REFUSED" in capsys.readouterr().err


def test_tiny_training_run_with_allow_synthetic(tmp_path, capsys):
    pytest.importorskip("sklearn")
    pytest.importorskip("joblib")
    rec = tmp_path / "rec"
    make_sim_session(rec, "sim-a", now=1_700_000_000)
    make_sim_session(rec, "sim-b", seed=10, now=1_700_000_100)
    rc = train_rf.main(["--recordings", str(rec), "--out-dir", str(tmp_path / "models"), "--trees", "20",
                        "--allow-synthetic"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC DATA" in out and "held out" in out
    models = list((tmp_path / "models").glob("rf_*_synthetic.joblib"))
    assert len(models) == 1
    import json
    meta = json.loads(models[0].with_suffix(".json").read_text())
    assert meta["synthetic"] is True and "NOT evidence" in meta["note"]
    for fold in meta["evaluation"]["folds"]:
        assert not set(fold["test_sessions"]) & set(fold["train_sessions"])
