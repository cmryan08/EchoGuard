import re
from pathlib import Path

import pytest

from backend.engine import Engine
from backend.ports import PortCandidate
from dashboard import server as server_mod
from dashboard.server import create_app

STATIC = Path(server_mod.__file__).resolve().parent / "static"
FIXTURE = "tests/fixtures/classic_esp32s3_sample.txt"


@pytest.fixture
def engine():
    eng = Engine()
    yield eng
    eng.stop()


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def app(engine, data_dir):
    app = create_app(engine, data_dir=data_dir)
    app.testing = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_index_serves_html_with_disclaimer(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "<title>EchoGuard" in html
    assert "Research prototype. Not a medical device. Predictions are not medically validated." in html
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    for asset in ("/static/app.js", "/static/style.css"):
        assert client.get(asset).status_code == 200


def test_static_assets_have_no_external_urls():
    for path in STATIC.iterdir():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"https?://", text), f"external URL in {path.name}"


def test_status_without_source(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.get_json()
    for key in ("source", "config", "counters", "packet_rate_hz", "rssi", "noise_floor", "quality",
                "activity", "motion", "thresholds", "calibration"):
        assert key in data
    assert data["packet_rate_hz"] is None
    assert data["rssi"]["last"] is None
    assert data["motion"] is None
    assert data["activity"]["label"] == "Unknown / insufficient data"
    assert resp.headers["Cache-Control"] == "no-store"


def test_replay_source_via_api(client, engine):
    resp = client.post("/api/source", json={"kind": "replay", "path": FIXTURE, "speed": 0, "loop": False})
    assert resp.status_code == 200, resp.get_json()
    assert engine.join(5.0)
    data = client.get("/api/status").get_json()
    assert data["source"]["kind"] == "replay"
    assert data["counters"]["packets"] == 13
    heat = client.get("/api/heatmap?n=50").get_json()
    assert len(heat["rows"]) == 13
    series = client.get("/api/series?seconds=10").get_json()
    assert series["ts"]
    stop = client.post("/api/stop", json={})
    assert stop.status_code == 200 and stop.get_json()["ok"] is True


@pytest.mark.parametrize("path", [
    "../../etc/passwd",
    "/etc/passwd",
    "tests/fixtures/../../README.md",
    "tests/fixtures/../../backend/csi_parser.py",
    "data/../Makefile",
    "tests/fixtures/missing.txt",
    "tests/test_engine.py",
    "",
    123,
])
def test_replay_path_rejected(client, path):
    resp = client.post("/api/source", json={"kind": "replay", "path": path})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


@pytest.mark.parametrize("body", [
    {"kind": "serial", "port": "/etc/passwd"},
    {"kind": "serial", "port": "/dev/../etc/passwd"},
    {"kind": "serial", "port": "COM3; rm -rf /"},
    {"kind": "serial", "port": "loop://"},
    {"kind": "serial", "port": ""},
    {"kind": "serial", "port": "auto", "baud": 10},
    {"kind": "serial", "port": "auto", "baud": "fast"},
    {"kind": "serial", "port": "auto", "baud": 115200.5},
    {"kind": "replay", "path": FIXTURE, "speed": -1},
    {"kind": "replay", "path": FIXTURE, "speed": 1000},
    {"kind": "replay", "path": FIXTURE, "loop": "yes"},
    {"kind": "simulation", "scenario": "nope"},
    {"kind": "simulation", "scenario": "demo", "noise": 5},
    {"kind": "simulation", "scenario": "demo", "rate_hz": 1},
    {"kind": "simulation", "scenario": "demo", "motion_strength": "big"},
    {"kind": "simulation", "scenario": "demo", "seed": 1.5},
    {"kind": "wifi"},
    {},
])
def test_invalid_source_requests_rejected(client, engine, body):
    resp = client.post("/api/source", json=body)
    assert resp.status_code == 400
    assert resp.get_json()["error"]
    assert engine.snapshot()["source"]["kind"] is None


def test_non_json_body_rejected(client):
    resp = client.post("/api/source", data="kind=serial&port=auto", content_type="application/x-www-form-urlencoded")
    assert resp.status_code == 400


def test_query_validation(client):
    assert client.get("/api/series?seconds=abc").status_code == 400
    assert client.get("/api/series?seconds=100000").status_code == 400
    assert client.get("/api/heatmap?n=0").status_code == 400
    assert client.get("/api/heatmap?n=1.5").status_code == 400
    assert client.get("/api/heatmap?n=10").status_code == 200
    assert client.get("/api/nope").status_code == 404
    assert client.get("/api/source").status_code == 405


def test_replay_files_lists_fixtures(client):
    files = client.get("/api/replay_files").get_json()["files"]
    paths = [f["path"] for f in files]
    assert FIXTURE in paths
    assert all(not p.endswith(".gitkeep") for p in paths)


def test_ports_listing(client, monkeypatch):
    fake = [PortCandidate(device="/dev/cu.usbmodem1101", vid=0x303A, pid=0x1001, likely_esp32=True,
                          bridge="Espressif", reason="USB VID 303A"),
            PortCandidate(device="/dev/cu.Bluetooth-Incoming-Port", excluded=True, reason="system")]
    monkeypatch.setattr(server_mod, "list_candidates", lambda: fake)
    data = client.get("/api/ports").get_json()
    assert data["error"] is None
    assert data["ports"][0]["device"] == "/dev/cu.usbmodem1101"
    assert data["ports"][0]["likely_esp32"] is True
    assert data["ports"][1]["excluded"] is True


# ------------------------------------------------------------- Milestone 3
import json as _json  # noqa: E402
import time as _time  # noqa: E402

from backend.calibration import sensor_key  # noqa: E402
from test_calibration import DURATIONS, SEPARABLE, SENSOR, Feeder, run_all  # noqa: E402


def _start_looping_replay(client):
    resp = client.post("/api/source", json={"kind": "replay", "path": FIXTURE, "speed": 1, "loop": True})
    assert resp.status_code == 200, resp.get_json()


def test_session_take_happy_path(client, engine, data_dir):
    resp = client.post("/api/session/create", json={"placement": "Living room", "notes": "api test"})
    assert resp.status_code == 200, resp.get_json()
    sid = resp.get_json()["session"]["session_id"]
    assert sid.endswith("-living-room")
    # no source yet -> cannot record
    resp = client.post("/api/take/start", json={"label": "walking"})
    assert resp.status_code == 400 and "no data source" in resp.get_json()["error"]

    _start_looping_replay(client)
    resp = client.post("/api/take/start", json={"label": "walking", "duration": None})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["take"]["label"] == "walking"
    deadline = _time.time() + 5
    while _time.time() < deadline and client.get("/api/recording").get_json()["active_take"]["packets"] < 3:
        _time.sleep(0.05)
    status = client.get("/api/status").get_json()
    assert status["recording"]["active"] is True
    resp = client.post("/api/take/stop", json={})
    assert resp.status_code == 200
    take = resp.get_json()["take"]
    assert take["packets"] >= 3 and take["stop_reason"] == "user"

    sdir = data_dir / "recordings" / sid
    meta = _json.loads((sdir / "session.json").read_text())
    assert meta["takes"][0]["file"] == take["file"]
    assert (sdir / take["file"]).is_file()

    sessions = client.get("/api/sessions").get_json()
    assert [s["session_id"] for s in sessions["sessions"]] == [sid]
    files = client.get("/api/replay_files").get_json()["files"]
    kinds = {(f["kind"], f["path"]) for f in files}
    assert any(k == "session" for k, _ in kinds)
    take_entry = next(f for f in files if f["kind"] == "take")
    assert take_entry["label"] == "walking"

    # replay the take through the same pipeline: ground truth label in status
    resp = client.post("/api/source", json={"kind": "replay", "path": str(sdir / take["file"]), "speed": 0})
    assert resp.status_code == 200, resp.get_json()
    assert engine.join(5)
    snap = client.get("/api/status").get_json()
    assert snap["ground_truth"]["label_name"] == "Walking"
    # whole session replay
    resp = client.post("/api/source", json={"kind": "replay", "path": str(sdir), "speed": 0})
    assert resp.status_code == 200, resp.get_json()
    assert engine.join(5)
    assert client.get("/api/status").get_json()["source"]["extra"]["format"] == "session"

    # delete needs confirmation
    assert client.post("/api/take/delete", json={"take_id": take["take_id"]}).status_code == 400
    resp = client.post("/api/take/delete", json={"take_id": take["take_id"], "confirm": True})
    assert resp.status_code == 200
    assert not (sdir / take["file"]).exists()


@pytest.mark.parametrize("body", [
    {"label": "dancing"},
    {"label": "Walking"},
    {"label": "../walking"},
    {"label": None},
    {"label": "walking", "duration": -1},
    {"label": "walking", "duration": "long"},
])
def test_take_start_invalid(client, body):
    client.post("/api/session/create", json={"placement": "lab"})
    _start_looping_replay(client)
    resp = client.post("/api/take/start", json=body)
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


@pytest.mark.parametrize("path,body", [
    ("/api/session/create", {"placement": "../../etc"}),
    ("/api/session/create", {"placement": "a/b"}),
    ("/api/session/create", {"placement": ""}),
    ("/api/session/create", {"placement": "ok", "notes": 5}),
    ("/api/session/select", {"session_id": "../../etc"}),
    ("/api/session/select", {"session_id": "20240101-000000-nope"}),
    ("/api/take/delete", {"take_id": "../../x", "confirm": True}),
    ("/api/take/stop", {}),
    ("/api/calibration/start", {"placement": "../x"}),
    ("/api/calibration/start", {"placement": "lab", "durations": {"empty_baseline": 5}}),
    ("/api/calibration/start", {"placement": "lab", "durations": {"nope": 20}}),
    ("/api/calibration/start", {"placement": "lab", "durations": [1, 2]}),
    ("/api/calibration/step/start", {}),
    ("/api/calibration/save", {}),
    ("/api/calibration/load", {"key": "../../etc/passwd"}),
    ("/api/calibration/load", {"key": "missing__classic"}),
    ("/api/calibration/delete", {"key": "../x", "confirm": True}),
    ("/api/calibration/delete", {"key": "x"}),
    ("/api/calibration/step/redo", {"step": 3}),
])
def test_m3_routes_reject_bad_input(client, path, body):
    resp = client.post(path, json=body)
    assert resp.status_code == 400, (path, body, resp.get_json())
    assert resp.get_json()["ok"] is False


def test_m3_routes_require_json(client):
    for path in ("/api/session/create", "/api/take/start", "/api/calibration/start", "/api/calibration/delete"):
        assert client.post(path, data="x=1", content_type="application/x-www-form-urlencoded").status_code == 400


def test_calibration_api_flow(client, app, engine, data_dir):
    state = client.get("/api/calibration/state").get_json()
    assert state["session"] is None and len(state["step_specs"]) == 4
    resp = client.post("/api/calibration/start", json={"placement": "Kitchen", "durations": DURATIONS})
    assert resp.status_code == 200, resp.get_json()
    st = resp.get_json()["state"]["session"]
    assert st["state"] == "ready" and st["current_step"] == "empty_baseline"
    # step start needs a running source
    assert client.post("/api/calibration/step/start", json={}).status_code == 400
    _start_looping_replay(client)
    resp = client.post("/api/calibration/step/start", json={})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["state"]["session"]["state"] == "recording"
    resp = client.post("/api/calibration/step/cancel", json={})
    assert resp.get_json()["state"]["session"]["state"] == "ready"
    client.post("/api/stop", json={})
    assert engine.join(5)

    # feed generated steps directly (the engine hook does the same with live frames)
    mgr = app.config["CALIBRATION"]
    with mgr._lock:
        run_all(mgr.session, Feeder(mgr.session), SEPARABLE)
    st = client.get("/api/calibration/state").get_json()["session"]
    assert st["state"] == "computed", st["error"]
    assert st["result"]["thresholds"]["movement"] > 0

    resp = client.post("/api/calibration/save", json={})
    assert resp.status_code == 200, resp.get_json()
    key = resp.get_json()["key"]
    assert key == sensor_key(SENSOR, "kitchen")
    assert (data_dir / "calibration" / f"{key}.json").is_file()
    status = client.get("/api/status").get_json()
    assert status["calibration"]["key"] == key and status["thresholds"]["calibrated"] is True

    listing = client.get("/api/calibrations").get_json()
    assert [c["key"] for c in listing["calibrations"]] == [key] and listing["active"]["key"] == key
    assert client.post("/api/calibration/clear", json={}).status_code == 200
    assert client.get("/api/status").get_json()["calibration"]["key"] is None
    assert client.post("/api/calibration/load", json={"key": key}).status_code == 200
    assert client.get("/api/status").get_json()["calibration"]["key"] == key
    resp = client.post("/api/calibration/delete", json={"key": key, "confirm": True})
    assert resp.status_code == 200
    assert not (data_dir / "calibration" / f"{key}.json").exists()
    assert client.get("/api/status").get_json()["calibration"]["key"] is None
    assert client.post("/api/calibration/cancel", json={}).status_code == 200
    assert client.get("/api/calibration/state").get_json()["session"] is None


def test_replay_session_dir_must_have_session_json(client, data_dir):
    d = data_dir / "recordings" / "20240101-000000-x"
    d.mkdir(parents=True)
    resp = client.post("/api/source", json={"kind": "replay", "path": str(d)})
    assert resp.status_code == 400


# ------------------------------------------------------------- Milestone 4/5
def _wait(pred, timeout=30.0):
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        v = pred()
        if v:
            return v
        _time.sleep(0.05)
    return None


def test_simulation_info_route(client):
    data = client.get("/api/simulation").get_json()
    assert {s["key"] for s in data["scenarios"]} >= {"empty_room", "walking", "sitting", "rapid_movement",
                                                      "potential_fall", "demo"}
    assert data["params"]["rate_hz"]["max"] == 200
    assert "Synthetic" in data["notice"]


def test_simulation_source_alert_and_cancel(client, app, engine, data_dir):
    from simulation import SimParams, SimulationSource

    # fast (non-realtime) simulated fall through the same engine + monitor the route uses
    engine.start(SimulationSource(SimParams(scenario="potential_fall", seed=7), realtime=False, duration_s=12))
    assert engine.join(60)
    status = client.get("/api/status").get_json()
    assert status["source"]["kind"] == "simulation" and status["source"]["synthetic"] is True
    assert status["alert"]["state"] == "active"
    assert status["activity"]["key"] == "potential_fall"
    alerts = client.get("/api/alerts").get_json()
    crit = alerts["alert"]["alert"]["event"]["criteria"]
    assert {c["name"] for c in crit} >= {"motion_spike", "stillness_duration", "data_quality"}
    assert all(c["passed"] for c in crit)
    assert alerts["detector"]["events_detected"] == 1

    resp = client.post("/api/alerts/cancel", json={})
    assert resp.status_code == 200 and resp.get_json()["alert"]["state"] == "cancelled"
    assert client.get("/api/status").get_json()["alert"]["state"] == "cancelled"
    assert client.post("/api/alerts/cancel", json={}).status_code == 400   # nothing left to cancel

    events = client.get("/api/events?n=200").get_json()
    kinds = {e["kind"] for e in events["events"]}
    assert {"connection", "alert", "cancel"} <= kinds
    assert (data_dir / "events.jsonl").is_file()
    assert client.get("/api/events?n=0").status_code == 400


def test_simulation_via_source_route(client, engine):
    resp = client.post("/api/source", json={"kind": "simulation", "scenario": "walking", "noise": 0.05,
                                            "motion_strength": 1.5, "event_duration_s": 2, "rate_hz": 100})
    assert resp.status_code == 200, resp.get_json()
    assert _wait(lambda: client.get("/api/status").get_json()["counters"]["packets"] > 20, 10)
    snap = client.get("/api/status").get_json()
    assert snap["source"]["kind"] == "simulation" and snap["source"]["synthetic"] is True
    assert snap["config"]["tx_mac"] == "02:51:4d:00:00:00"
    assert snap["ground_truth"]["label_name"].endswith("[simulated]")
    client.post("/api/stop", json={})


def test_simulated_recording_is_marked_synthetic(client, engine, data_dir):
    client.post("/api/source", json={"kind": "simulation", "scenario": "walking"})
    assert _wait(lambda: client.get("/api/status").get_json()["counters"]["packets"] > 5, 10)
    sid = client.post("/api/session/create", json={"placement": "sim"}).get_json()["session"]["session_id"]
    assert client.post("/api/take/start", json={"label": "walking"}).status_code == 200
    assert _wait(lambda: client.get("/api/recording").get_json()["active_take"]["packets"] > 20, 10)
    client.post("/api/take/stop", json={})
    meta = _json.loads((data_dir / "recordings" / sid / "session.json").read_text())
    assert meta["synthetic"] is True and meta["source_kind"] == "simulation"
    assert meta["takes"][0]["synthetic"] is True
    client.post("/api/stop", json={})
    assert engine.join(5)

    # export the synthetic session: rows marked synthetic, file name tagged
    resp = client.post("/api/export", json={"what": "session", "path": f"data/recordings/{sid}"})
    # path is relative to the project root; the test data dir lives in tmp_path, so use the absolute path
    if resp.status_code == 400:
        resp = client.post("/api/export", json={"what": "session", "path": str(data_dir / "recordings" / sid)})
    assert resp.status_code == 200, resp.get_json()
    exp = resp.get_json()["export"]
    assert exp["synthetic"] is True and "_synthetic" in exp["name"] and exp["rows"] > 20
    dl = client.get(resp.get_json()["url"])
    assert dl.status_code == 200 and "attachment" in dl.headers["Content-Disposition"]
    text = dl.get_data(as_text=True).splitlines()
    header = text[0].split(",")
    assert header[:7] == ["host_ts_iso", "host_ts_unix", "source_kind", "synthetic", "session_id", "take_id", "label"]
    assert "motion_score" in header and "amp_lltf_1" in header
    row = text[-1].split(",")
    assert row[header.index("synthetic")] == "1" and row[header.index("label")] == "walking"


def test_export_buffer_and_listing(client, engine, data_dir):
    assert client.post("/api/export", json={"what": "buffer"}).status_code == 400   # empty buffer
    client.post("/api/source", json={"kind": "replay", "path": FIXTURE, "speed": 0})
    assert engine.join(5)
    resp = client.post("/api/export", json={"what": "buffer"})
    assert resp.status_code == 200, resp.get_json()
    exp = resp.get_json()["export"]
    assert exp["rows"] == 13 and exp["synthetic"] is False
    assert (data_dir / "exports" / exp["name"]).is_file()
    names = [e["name"] for e in client.get("/api/exports").get_json()["exports"]]
    assert exp["name"] in names


@pytest.mark.parametrize("name", ["../config.json", "..%2Fsecret.csv", "x.txt", ".hidden.csv", "missing.csv"])
def test_export_download_rejects_bad_names(client, name):
    assert client.get(f"/api/exports/{name}").status_code in (400, 404)


@pytest.mark.parametrize("body", [
    {"what": "everything"},
    {"what": "session", "path": "../../etc/passwd"},
    {"what": "session", "path": "tests/fixtures/classic_esp32s3_sample.txt"},
    {},
])
def test_export_rejects_bad_requests(client, body):
    assert client.post("/api/export", json=body).status_code == 400


def test_detector_config_route(client, engine):
    data = client.get("/api/detector/config").get_json()
    assert data["fall"]["stillness_duration_s"] == 3.0 and data["alerts"]["countdown_s"] == 30.0
    assert data["effective"]["spike"] == engine.config.thresholds.rapid
    resp = client.post("/api/detector/config", json={"stillness_duration_s": 4, "countdown_s": 10, "enabled": True})
    assert resp.status_code == 200, resp.get_json()
    assert engine.config.fall.stillness_duration_s == 4.0 and engine.config.alerts.countdown_s == 10.0
    assert resp.get_json()["saved_to"] is None          # no config path in tests
    for bad in ({"stillness_duration_s": 0}, {"countdown_s": -1}, {"nope": 1}, {"enabled": "yes"},
                {"spike_threshold": 2, "stillness_threshold": 3}, {"max_gap_s": "x"}):
        assert client.post("/api/detector/config", json=bad).status_code == 400, bad
    assert engine.config.fall.spike_threshold == 0.0


def test_ui_has_alert_simulation_and_export_controls(client):
    html = client.get("/").get_data(as_text=True)
    for needle in ('id="tab-simulation"', 'id="alert-cancel"', 'id="alert-criteria"', 'id="event-rows"',
                   'id="export-buffer"', 'id="sim-noise"', 'id="sim-strength"', 'id="sim-duration"', 'id="sim-rate"',
                   "SIMULATED DATA - synthetic, not evidence of real-world accuracy"):
        assert needle in html, needle
