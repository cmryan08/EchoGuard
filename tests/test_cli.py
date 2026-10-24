import json

from backend import cli
from test_train_rf import make_sim_session


def test_evaluate_reports_per_take(tmp_path, capsys):
    s = make_sim_session(tmp_path, "sim", seconds=12.0)
    rc = cli.main(["evaluate", "--session", str(s.dir), "--config", str(tmp_path / "none.json")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC DATA" in out and "not an accuracy estimate" in out
    assert "POTENTIAL FALL" in out and "[PASS] motion_spike" in out

    rc = cli.main(["evaluate", "--session", str(s.dir), "--json", "--config", str(tmp_path / "none.json")])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    fired = {t["label"]: t["fired"] for t in data["takes"]}
    assert fired == {"empty_room": False, "walking": False, "simulated_fall": True}
    assert data["synthetic"] is True


def test_evaluate_missing_session(tmp_path, capsys):
    assert cli.main(["evaluate", "--session", str(tmp_path / "nope")]) == 2


def test_monitor_replay_prints_motion(capsys, classic_fixture, tmp_path):
    rc = cli.main(["monitor", "--replay", str(classic_fixture), "--speed", "0", "--interval", "0.2",
                   "--config", str(tmp_path / "none.json")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CSI pkts" in out and "motion" in out and "0 potential-fall event(s)" in out
