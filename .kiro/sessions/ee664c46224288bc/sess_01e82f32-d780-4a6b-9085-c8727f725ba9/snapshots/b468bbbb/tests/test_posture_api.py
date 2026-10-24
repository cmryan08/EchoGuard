"""Sitting / standing API routes (Flask test client). Simulation data is SYNTHETIC."""

import collections
import datetime
import json
import threading
import time

import pytest

from backend.config import AppConfig
from backend.engine import Engine
from dashboard.server import create_app
from simulation import SimulationSource

DESK_SITTING_2 = 20 + 8 + 12 + 1.5 + 75 + 1.5 + 30 + 20 + 15 + 1.5   # start of the 2nd sitting segment


class PausingSim(SimulationSource):
    """Non-realtime simulation that stops emitting (without finishing) after
    ``pause_at`` s of simulated time, so the API can be queried mid-scenario."""

    def __init__(self, pause_at, **kw):
        super().__init__(realtime=False, **kw)
        self.pause_at = pause_at
        self.reached = threading.Event()

    def lines(self):
        for ts, line in super().lines():
            yield ts, line
            if ts - self.t0 >= self.pause_at:
                self.reached.set()
                self._stop.wait(60)
                return


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
def config_path(tmp_path):
    return tmp_path / "config.json"


@pytest.fixture
def app(engine, data_dir, config_path):
    app = create_app(engine, data_dir=data_dir, config_path=config_path)
    app.testing = True
    yield app
    app.config["POSTURE"].close()


@pytest.fixture
def client(app):
    return app.test_client()


def noon_today():
    return time.mktime(datetime.date.today().timetuple()) + 12 * 3600


def test_not_calibrated_before_any_calibration(client, data_dir):
    resp = client.get("/api/posture")
    assert resp.status_code == 200 and resp.headers["Cache-Control"] == "no-store"
    d = resp.get_json()
    assert d["state"] == "unknown" and d["reason"] == "not calibrated" and d["calibrated"] is False
    assert d["synthetic"] is False and d["source_kind"] is None and d["probabilities"] == {}
    for key in ("streak", "sitting_streak_s", "reminder", "today", "upright_note", "disclaimer", "states"):
        assert key in d
    assert d["reminder"]["active"] is False and d["reminder"]["reminder_after_min"] == 30
    assert "not a medical device" in d["disclaimer"]
    status = client.get("/api/status").get_json()
    assert status["posture"]["state"] == "unknown" and status["posture"]["calibrated"] is False
    assert (data_dir / "posture.sqlite3").is_file()


def test_uncalibrated_simulation_reports_unknown_or_moving(client, engine):
    src = PausingSim(25.0, scenario="desk_day", seed=3, t0=noon_today())
    seen = collections.Counter()
    engine.add_frame_hook(lambda f, a: seen.update([client.application.config["POSTURE"].classifier.state]))
    engine.start(src)
    assert src.reached.wait(60)
    assert set(seen) <= {"unknown", "moving"} and seen["moving"] > 0
    d = client.get("/api/posture").get_json()
    assert d["state"] in ("unknown", "moving") and d["synthetic"] is True and d["source_kind"] == "simulation"


def test_today_history_export_shapes(client):
    today = client.get("/api/posture/today").get_json()
    assert today["date"] == datetime.date.today().isoformat()
    assert set(today["totals_s"]) == {"sitting", "standing", "moving", "away", "unknown"}
    assert all(len(v) == 24 for v in today["hourly_min"].values()) and "upright" in today["hourly_min"]
    assert len(today["stand_hour_flags"]) == 24 and today["stand_goal_hours"] == 12
    for key in ("upright_s", "transitions", "longest_sitting_s", "sitting_bouts", "average_sitting_s",
                "stand_hours", "reminders", "synthetic", "source_kind", "data_source"):
        assert key in today
    assert set(today["reminders"]) == {"count", "responded", "median_response_s"}
    d = client.get("/api/posture/today?date=2024-02-29").get_json()
    assert d["date"] == "2024-02-29" and d["tracked_s"] == 0
    hist = client.get("/api/posture/history?days=3").get_json()
    assert hist["days"] == 3 and len(hist["daily"]) == 3 and hist["daily"][-1]["date"] == today["date"]
    assert set(hist["daily"][0]) >= {"date", "sitting_min", "standing_min", "upright_min", "stand_hours"}
    resp = client.get("/api/posture/export?format=csv&days=7")
    assert resp.status_code == 200 and resp.mimetype == "text/csv"
    cd = resp.headers["Content-Disposition"]
    assert cd.startswith("attachment;") and ".csv" in cd
    text = resp.get_data(as_text=True)
    assert text.startswith("# title,")
    assert ("Research prototype, not a medical device; sitting/standing are estimated from Wi-Fi signals "
            "and may be wrong") in text
    assert "# app_version" in text and "# date_range" in text and "# calibration_key" in text
    assert "# daily summary" in text and "# bouts" in text
    resp = client.get("/api/posture/export?format=json&days=2")
    assert resp.status_code == 200 and resp.headers["Content-Disposition"].startswith("attachment;")
    body = json.loads(resp.get_data(as_text=True))
    assert "not a medical device" in body["metadata"]["disclaimer"] and len(body["daily"]) == 2


@pytest.mark.parametrize("url", [
    "/api/posture/today?date=2024-13-01",
    "/api/posture/today?date=2024-1-1",
    "/api/posture/today?date=today",
    "/api/posture/today?date=2024-02-30",
    "/api/posture/history?days=0",
    "/api/posture/history?days=91",
    "/api/posture/history?days=x",
    "/api/posture/history?days=2.5",
    "/api/posture/history?end=2024/01/01",
    "/api/posture/export?format=xml",
    "/api/posture/export?format=csv&days=366",
    "/api/posture/export?format=csv&days=0",
    "/api/posture/export?end=bad",
])
def test_query_validation(client, url):
    resp = client.get(url)
    assert resp.status_code == 400 and resp.get_json()["ok"] is False


@pytest.mark.parametrize("body", [
    {"reminder_after_min": 0},
    {"reminder_after_min": 181},
    {"reminder_after_min": "soon"},
    {"reminder_after_min": None},
    {"reminder_after_min": True},
    {"stand_goal_hours": 1.5},
    {"stand_goal_hours": 25},
    {"min_confidence": 0.3},
    {"hold_s": 2, "transition_confirm_s": 3},
    {"bogus": 1},
])
def test_settings_validation(client, engine, body):
    before = engine.config.posture.reminder_after_min
    resp = client.post("/api/posture/settings", json=body)
    assert resp.status_code == 400 and resp.get_json()["ok"] is False
    assert engine.config.posture.reminder_after_min == before


def test_settings_update_is_saved(client, engine, config_path):
    d = client.get("/api/posture/settings").get_json()
    assert d["settings"]["reminder_after_min"] == 30 and d["ranges"]["reminder_after_min"] == {"min": 1.0, "max": 180.0}
    resp = client.post("/api/posture/settings", json={"reminder_after_min": 45, "stand_goal_hours": 10})
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()
    assert out["ok"] and out["settings"]["reminder_after_min"] == 45.0 and out["settings"]["stand_goal_hours"] == 10
    assert engine.config.posture.reminder_after_min == 45.0
    saved = AppConfig.load(config_path)
    assert saved.posture.reminder_after_min == 45.0 and saved.posture.stand_goal_hours == 10
    assert client.get("/api/posture").get_json()["reminder"]["reminder_after_min"] == 45.0


def test_snooze_route(client):
    for bad in ({"minutes": 4}, {"minutes": 61}, {"minutes": "x"}):
        assert client.post("/api/posture/reminder/snooze", json=bad).status_code == 400
    resp = client.post("/api/posture/reminder/snooze", json={"minutes": 15})
    assert resp.status_code == 200
    rem = resp.get_json()["reminder"]
    assert rem["snoozed"] is True and rem["snooze_remaining_s"] == pytest.approx(900, abs=2)


def test_simulation_calibrate_requires_simulation(client):
    resp = client.post("/api/simulation/calibrate", json={})
    assert resp.status_code == 400 and "simulation" in resp.get_json()["error"]


def test_desk_day_with_synthetic_calibration(client, engine, app):
    """Acceptance: with a simulation-derived calibration the desk_day scenario
    is tracked as sitting during sitting segments and standing / moving during
    standing and walking segments (SYNTHETIC data, labelled as such)."""
    t0 = noon_today()
    first = PausingSim(1.0, scenario="desk_day", seed=9, t0=t0)
    engine.start(first)
    assert first.reached.wait(30)
    resp = client.post("/api/simulation/calibrate", json={})
    assert resp.status_code == 200, resp.get_json()
    cal = resp.get_json()
    assert cal["synthetic"] is True and cal["key"].endswith("__synthetic")
    assert cal["posture_self_check"]["accuracy"] >= 0.9

    src = PausingSim(DESK_SITTING_2 + 40, scenario="desk_day", seed=9, t0=t0)
    svc = app.config["POSTURE"]
    seen = collections.defaultdict(collections.Counter)

    def record(frame, activity):
        rel = frame.ts - src.t0
        _, seg, tl = src.sim.segment_at(rel)
        if rel >= 0 and tl >= 8.0:
            seen[seg.key][svc.classifier.state] += 1

    engine.add_frame_hook(record)
    engine.start(src)
    assert src.reached.wait(120)

    def frac(key, states):
        c = seen[key]
        return sum(c[s] for s in states) / max(1, sum(c.values()))

    assert sum(seen["sitting"].values()) > 500
    assert frac("sitting", ["sitting"]) >= 0.95, seen
    assert frac("standing_still", ["standing", "moving"]) >= 0.95, seen
    assert frac("walking", ["standing", "moving"]) >= 0.95, seen
    assert frac("empty_room", ["away"]) >= 0.9, seen

    d = client.get("/api/posture").get_json()
    assert d["state"] == "sitting" and d["calibrated"] is True
    assert d["synthetic"] is True and d["source_kind"] == "simulation" and d["persisting"] is False
    assert d["calibration"]["synthetic"] is True and d["probabilities"]["sitting"] > 0.6
    assert d["streak"]["active"] and 20 < d["sitting_streak_s"] < 45
    today = client.get("/api/posture/today").get_json()
    assert today["synthetic"] is True and today["data_source"] == "memory"
    assert today["totals_s"]["sitting"] > 90 and today["upright_s"] > 60
    assert today["transitions"] == 1 and today["sitting_bouts"] == 2
    csv_text = client.get("/api/posture/export?format=csv&days=1").get_data(as_text=True)
    assert "SYNTHETIC" in csv_text and "\nsitting," in csv_text
    # nothing from the simulation is persisted
    store = svc.store
    assert store.bouts(0, 4e9) == []
