import json

import pytest

from backend.activity import Activity
from backend.alerts import (ALERT_ACTIVE, ALERT_CANCELLED, ALERT_IDLE, ALERT_NOTIFIED, AlertError, AlertManager,
                            EventLog)
from backend.config import AlertConfig


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


EVENT = {"event_id": "fall-1", "summary": "motion spike 9.00 followed by 3.0 s of stillness",
         "criteria": [{"name": "motion_spike", "passed": True}, {"name": "stillness_duration", "passed": True}],
         "synthetic": True, "source_kind": "simulation"}


def test_countdown_then_simulated_notification(tmp_path):
    clock = Clock()
    log = EventLog(tmp_path / "events.jsonl", clock=clock)
    am = AlertManager(AlertConfig(countdown_s=30), log, clock=clock)
    assert am.state == ALERT_IDLE and not am.active
    alert = am.trigger(EVENT)
    assert alert["state"] == ALERT_ACTIVE and am.active
    clock.t += 10
    st = am.status()
    assert st["alert"]["remaining_s"] == pytest.approx(20.0)
    assert 0.3 < st["alert"]["progress"] < 0.4
    clock.t += 21
    st = am.status()
    assert st["state"] == ALERT_NOTIFIED and st["active"] is True
    msg = st["alert"]["notification"]
    assert "SIMULATED caregiver notification" in msg and "no message was sent" in msg and "SYNTHETIC" in msg
    kinds = [e["kind"] for e in log.recent()]
    assert kinds[:2] == ["notification", "alert"]
    # dismiss after notification
    out = am.cancel("tester")
    assert out["state"] == ALERT_CANCELLED and out["cancelled_after_notification"] is True
    assert not am.active


def test_cancel_before_countdown_ends():
    clock = Clock()
    am = AlertManager(AlertConfig(countdown_s=30), EventLog(None, clock=clock), clock=clock)
    am.trigger(EVENT)
    clock.t += 5
    out = am.cancel()
    assert out["state"] == ALERT_CANCELLED and out["notified_at"] is None
    clock.t += 60
    assert am.status()["state"] == ALERT_CANCELLED   # never notifies after cancel
    with pytest.raises(AlertError):
        am.cancel()
    # a new event starts a new alert
    assert am.trigger(EVENT)["alert_id"] == "A0002"


def test_second_trigger_while_active_is_logged_not_stacked():
    clock = Clock()
    log = EventLog(None, clock=clock)
    am = AlertManager(AlertConfig(countdown_s=30), log, clock=clock)
    assert am.trigger(EVENT) is not None
    assert am.trigger(EVENT) is None
    assert log.recent(1)[0]["kind"] == "detector"


def test_zero_countdown_notifies_immediately():
    am = AlertManager(AlertConfig(countdown_s=0), EventLog(None))
    am.trigger(EVENT)
    assert am.state == ALERT_NOTIFIED


def test_event_log_persistence_and_bounds(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path, maxlen=10)
    for i in range(25):
        log.add("activity", f"change {i}", activity="movement")
    assert len(log) == 10
    assert log.recent(3)[0]["message"] == "change 24"
    lines = path.read_text().splitlines()
    assert len(lines) == 25                      # file is append-only, memory is bounded
    entry = json.loads(lines[0])
    assert entry["kind"] == "activity" and entry["data"] == {"activity": "movement"}
    # reloading restores the newest entries and continues the ids
    log2 = EventLog(path, maxlen=10)
    assert log2.recent(1)[0]["message"] == "change 24"
    assert log2.add("system", "restart")["id"] == 26
    assert [e["kind"] for e in log2.recent(5, kinds={"system"})] == ["system"]


def test_event_log_rotation(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path, max_file_bytes=500)
    for i in range(20):
        log.add("activity", "x" * 50)
    assert (tmp_path / "events.jsonl.1").exists()
    assert path.stat().st_size < 1000


def test_fall_monitor_sets_activity_override_and_cancel():
    from backend.engine import Engine
    from backend.monitor import FallMonitor

    eng = Engine()
    clock = Clock()
    mon = FallMonitor(eng, clock=clock)
    mon.alerts.trigger(EVENT)
    mon.poll()
    assert eng.activity_override == Activity.POTENTIAL_FALL
    assert eng.snapshot()["activity"]["key"] == "potential_fall"
    assert eng.snapshot()["alert"]["state"] == ALERT_ACTIVE
    mon.cancel_alert()
    assert eng.activity_override is None
    assert eng.snapshot()["alert"]["state"] == ALERT_CANCELLED
