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
def client(engine):
    app = create_app(engine)
    app.testing = True
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
    {"kind": "simulation"},
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
