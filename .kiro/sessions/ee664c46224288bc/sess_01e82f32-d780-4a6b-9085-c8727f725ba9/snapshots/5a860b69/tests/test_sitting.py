"""Sitting tracker, posture classifier hysteresis and history store.

All inputs are generated test readings / profiles, not measurements.
"""

import time
from types import SimpleNamespace

import numpy as np
import pytest

from backend import posture
from backend.config import AppConfig, PostureConfig
from backend.posture import PostureClassifier, PostureReading, fit_posture_model
from backend.sitting import (CHECKPOINT_S, PostureService, PostureStore, SittingTracker, day_bounds, local_date,
                             parse_date, summarize_days)

#: 2024-03-05 09:00 local time
T0 = time.mktime((2024, 3, 5, 9, 0, 0, 0, 0, -1))
DAY = "2024-03-05"


def reading(ts, state):
    return PostureReading(ts=ts, raw_label=state, state=state, probabilities={}, confidence=1.0, reason="test")


def feed(tracker, t, seconds, state, step=1.0):
    """Readings every ``step`` s for ``seconds``; returns the next timestamp and the events."""
    events = []
    n = int(round(seconds / step))
    for i in range(n):
        events += tracker.update(reading(t + i * step, state))
    return t + n * step, events


def tracker(**cfg):
    return SittingTracker(PostureConfig(**cfg), clock=lambda: T0)


# ------------------------------------------------------------- tracker
def test_streak_accumulates_and_tolerates_short_unknown_gaps():
    tr = tracker(unknown_grace_s=10)
    t, _ = feed(tr, T0, 300, "sitting")
    t, _ = feed(tr, t, 5, "unknown")
    t, _ = feed(tr, t, 300, "sitting")
    st = tr.current_streak()
    assert st["active"] and st["start_ts"] == T0
    assert st["seconds"] == pytest.approx(604, abs=1.5)
    t, ev = feed(tr, t, 15, "unknown")
    assert not tr.current_streak()["active"]
    assert [e["ended_by"] for e in ev if e["kind"] == "streak_end"] == ["unknown"]


def test_data_gap_counts_as_unknown():
    tr = tracker(unknown_grace_s=10)
    t, _ = feed(tr, T0, 100, "sitting")
    feed(tr, t + 5, 10, "sitting")                  # 6 s without readings: tolerated
    assert tr.current_streak()["active"] and tr.current_streak()["start_ts"] == T0
    t, _ = feed(tr, t + 30, 10, "sitting")          # 30 s gap: new streak
    assert tr.current_streak()["start_ts"] > T0 + 100


def test_stand_break_ends_streak_but_short_upright_does_not():
    tr = tracker(stand_break_s=60)
    t, _ = feed(tr, T0, 300, "sitting")
    t, _ = feed(tr, t, 30, "standing")
    t, _ = feed(tr, t, 20, "moving")                # 50 s upright < 60 s
    t, _ = feed(tr, t, 100, "sitting")
    assert tr.current_streak()["active"] and tr.current_streak()["start_ts"] == T0
    t, ev = feed(tr, t, 61, "moving")               # moving counts as upright
    ends = [e for e in ev if e["kind"] == "streak_end"]
    assert len(ends) == 1 and ends[0]["ended_by"] == "stand_break"
    assert ends[0]["duration_s"] == pytest.approx(450, abs=1.5)
    assert not tr.current_streak()["active"]


def test_away_ends_streak_only_after_limit():
    tr = tracker(away_end_streak_s=120)
    t, _ = feed(tr, T0, 60, "sitting")
    t, _ = feed(tr, t, 100, "away")
    t, _ = feed(tr, t, 10, "sitting")
    assert tr.current_streak()["start_ts"] == T0
    t, ev = feed(tr, t, 125, "away")
    assert [e["ended_by"] for e in ev if e["kind"] == "streak_end"] == ["away"]


def test_reminder_fires_and_stand_break_clears_it():
    tr = tracker(reminder_after_min=1, stand_break_s=60)
    t, ev = feed(tr, T0, 60, "sitting")
    assert not tr.reminder_status()["active"] and not [e for e in ev if e["kind"] == "reminder"]
    assert tr.reminder_status()["due_in_s"] == pytest.approx(1, abs=1.01)
    t, ev = feed(tr, t, 21, "sitting")
    fired = [e for e in ev if e["kind"] == "reminder"]
    assert len(fired) == 1
    rem_ts = fired[0]["ts"]
    status = tr.reminder_status()
    assert status["active"] and status["since_ts"] == rem_ts and "Time to stand up" in status["message"]
    stand_start = t
    t, ev = feed(tr, t, 61, "standing")
    assert not tr.reminder_status()["active"]
    rec = tr.reminders[0]
    assert rec["kind"] == "reminder" and rec["cleared_by"] == "stand_break"
    assert rec["responded_after_s"] == pytest.approx(stand_start - rem_ts, abs=1.01)
    summ = tr.summary(DAY)
    assert summ["reminders"]["count"] == 1 and summ["reminders"]["responded"] == 1
    assert summ["reminders"]["median_response_s"] == pytest.approx(stand_start - rem_ts, abs=1.01)


def test_snooze_suppresses_then_fires_again():
    tr = tracker(reminder_after_min=1)
    t, _ = feed(tr, T0, 62, "sitting")
    assert tr.reminder_status()["active"]
    st = tr.snooze(5)
    assert st["active"] is False and st["snoozed"] is True
    assert st["snooze_remaining_s"] == pytest.approx(300, abs=1)
    t, ev = feed(tr, t, 290, "sitting")
    assert not [e for e in ev if e["kind"] == "reminder"] and not tr.reminder_status()["active"]
    t, ev = feed(tr, t, 15, "sitting")
    assert len([e for e in ev if e["kind"] == "reminder"]) == 1
    assert [r["kind"] for r in tr.reminders] == ["reminder", "snooze", "reminder"]
    assert tr.summary(DAY)["reminders"]["count"] == 2


def test_snooze_before_reminder_and_without_data():
    tr = tracker(reminder_after_min=1)
    st = tr.snooze(10)                    # no readings yet: uses the clock
    assert st["snoozed_until"] == pytest.approx(T0 + 600)
    feed(tr, T0, 120, "sitting")
    assert not tr.reminder_status()["active"]


def test_stand_hours_transitions_and_bouts():
    tr = tracker(stand_hour_min_s=60, stand_break_s=60)
    t, _ = feed(tr, T0, 20 * 60, "sitting")                 # 09:00-09:20
    t, _ = feed(tr, t, 120, "standing")                     # stand break (2 min)
    t, _ = feed(tr, t, 3, "moving")                         # short movement: no transition
    t, _ = feed(tr, t, 10 * 60, "sitting")
    t, _ = feed(tr, t, 30, "standing")                      # 30 s upright: no stand hour on its own
    t, _ = feed(tr, t, T0 + 3600 + 30 * 60 - t, "sitting")  # through 10:30
    s = tr.summary(DAY)
    assert s["stand_hour_flags"][9] is True and s["stand_hour_flags"][10] is False
    assert s["stand_hours"] == 1 and s["stand_goal_hours"] == 12
    assert s["transitions"] == 2
    assert s["totals_s"]["standing"] == pytest.approx(150, abs=2)
    assert s["upright_s"] == pytest.approx(153, abs=2)
    assert len(s["hourly_min"]["sitting"]) == 24 and s["hourly_min"]["upright"][9] == pytest.approx(2.55, abs=0.05)
    # the 30 s stand did not break the second streak: two sitting bouts
    assert s["sitting_bouts"] == 2
    assert s["longest_sitting_s"] == pytest.approx(5400 - 20 * 60 - 123, abs=3)
    assert sum(s["totals_s"].values()) == pytest.approx(s["tracked_s"], abs=0.5)


def test_day_rollover_at_local_midnight():
    t_mid = time.mktime((2024, 3, 5, 23, 59, 0, 0, 0, -1))
    tr = SittingTracker(PostureConfig(), clock=lambda: t_mid)
    feed(tr, t_mid, 121, "sitting")
    days = summarize_days(tr.all_bouts(), ["2024-03-05", "2024-03-06"], tr.config)
    assert days["2024-03-05"]["totals_s"]["sitting"] == pytest.approx(60, abs=0.5)
    assert days["2024-03-06"]["totals_s"]["sitting"] == pytest.approx(60, abs=0.5)
    assert days["2024-03-05"]["hourly_min"]["sitting"][23] == pytest.approx(1.0, abs=0.01)
    assert days["2024-03-06"]["hourly_min"]["sitting"][0] == pytest.approx(1.0, abs=0.01)
    # the streak is counted on the day it started
    assert days["2024-03-05"]["sitting_bouts"] == 1 and days["2024-03-06"]["sitting_bouts"] == 0
    assert local_date(t_mid + 61) == "2024-03-06"
    a, b = day_bounds(parse_date("2024-03-06"))
    assert a == pytest.approx(t_mid + 60) and b - a in (23 * 3600, 24 * 3600, 25 * 3600)


@pytest.mark.parametrize("bad", ["2024-13-01", "2024-02-30", "2024-1-1", "20240101", "x", None, 20240101])
def test_parse_date_is_strict(bad):
    with pytest.raises(ValueError):
        parse_date(bad)


# ----------------------------------------------------------- classifier
def frame(ts, motion=0.5, ok=True, reasons=()):
    return SimpleNamespace(ts=ts, motion_score=motion, quality_ok=ok, quality_reasons=list(reasons))


LAYOUT = ("classic", 52)
PROFILE = np.ones(52)
FAKE_MODEL = {"n_subcarriers": 52}


class Scripted:
    """Stands in for classify_profile: returns the probabilities set in ``now``."""

    def __init__(self):
        self.now = ("sitting", 0.9)

    def __call__(self, model, profile, classes=None):
        label, p = self.now
        other = "standing" if label == "sitting" else "sitting"
        return label, {label: p, other: 1.0 - p}


@pytest.fixture
def scripted(monkeypatch):
    s = Scripted()
    monkeypatch.setattr(posture, "classify_profile", s)
    return s


def run(clf, t, seconds, motion=0.5, model=FAKE_MODEL, step=0.1, layout=LAYOUT):
    states = []
    for i in range(int(round(seconds / step))):
        r = clf.update(frame(t + i * step, motion), PROFILE, layout, model, 2.5)
        states.append((r.ts, r.state))
    return t + round(seconds / step) * step, states


def first_ts(states, state):
    return next(ts for ts, s in states if s == state)


def test_not_calibrated_never_guesses_posture():
    clf = PostureClassifier(PostureConfig())
    t, states = run(clf, T0, 20, model=None)
    assert {s for _, s in states} == {"unknown"}
    assert clf.last.reason == "not calibrated" and clf.last.raw_label is None and clf.last.probabilities == {}
    t, states = run(clf, t, 3, motion=8.0, model=None)
    assert states[-1][1] == "moving"
    t, states = run(clf, t, 1, model=None)
    assert states[-1][1] == "unknown" and clf.last.reason == "not calibrated"


def test_quality_and_layout_give_unknown(scripted):
    clf = PostureClassifier(PostureConfig())
    t, _ = run(clf, T0, 10)
    assert clf.state == "sitting"
    r = clf.update(frame(t, ok=False, reasons=["low_rate: 5 Hz"]), PROFILE, LAYOUT, FAKE_MODEL, 2.5)
    assert r.state == "unknown" and "insufficient data quality" in r.reason
    t, _ = run(clf, t + 0.1, 2, layout=("c5c6", 64))
    assert clf.state == "unknown" and "layout" in clf.last.reason


def test_hysteresis_no_flapping(scripted):
    clf = PostureClassifier(PostureConfig(hold_s=5.0, min_confidence=0.6))
    t, states = run(clf, T0, 10)
    # 1 s window fill + 5 s hold
    assert first_ts(states, "sitting") - T0 == pytest.approx(6.0, abs=0.25)
    flips = []
    for i in range(15):                                     # 2 s alternation for 30 s
        scripted.now = ("standing", 0.7) if i % 2 == 0 else ("sitting", 0.7)
        t, states = run(clf, t, 2)
        flips += [s for _, s in states]
    assert set(flips) == {"sitting"}
    scripted.now = ("standing", 0.55)                       # below min_confidence
    t, states = run(clf, t, 10)
    assert {s for _, s in states} == {"sitting"} and "low confidence" in clf.last.reason
    scripted.now = ("standing", 0.9)
    t0 = t
    t, states = run(clf, t, 7)
    assert first_ts(states, "standing") - t0 == pytest.approx(5.0, abs=0.25)


def test_faster_confirmation_after_movement_burst(scripted):
    clf = PostureClassifier(PostureConfig(hold_s=5.0, transition_confirm_s=2.0))
    t, _ = run(clf, T0, 10)
    assert clf.state == "sitting"
    t, states = run(clf, t, 0.5, motion=8.0)               # brief fidget: stays sitting
    assert {s for _, s in states} == {"sitting"}
    t, states = run(clf, t, 2.0, motion=8.0)               # standing up
    assert states[-1][1] == "moving"
    scripted.now = ("standing", 0.95)
    t_end = t
    t, states = run(clf, t, 6)
    # 1 s window fill + 2 s confirmation instead of 5 s
    assert first_ts(states, "standing") - t_end == pytest.approx(3.0, abs=0.25)


def test_moving_without_confirmed_posture_decays_to_unknown(scripted):
    clf = PostureClassifier(PostureConfig(hold_s=5.0, posture_window_s=3.0))
    t, _ = run(clf, T0, 3, motion=8.0)
    assert clf.state == "moving"
    scripted.now = ("standing", 0.55)                      # never confident
    t, states = run(clf, t, 10)
    assert states[-1][1] == "unknown"


def test_real_model_classifies_generated_profiles():
    rng = np.random.default_rng(0)
    base = np.ones(52)
    sit = base.copy()
    sit[10:15] += 0.05
    gen = lambda p, n: [p + rng.normal(0, 0.005, 52) for _ in range(n)]
    model = fit_posture_model({"sitting": gen(sit, 100), "standing": gen(base, 100), "empty": gen(base * 1.02, 100)})
    clf = PostureClassifier(PostureConfig())
    t = T0
    for prof, want in ((sit, "sitting"), (base, "standing")):
        for i in range(100):
            r = clf.update(frame(t), prof + rng.normal(0, 0.005, 52), LAYOUT, model, 2.5)
            t += 0.1
        assert r.state == want and r.raw_label == want and r.confidence > 0.9


# ---------------------------------------------------------------- store
def test_store_round_trip_and_reopen(tmp_path):
    path = tmp_path / "posture.sqlite3"
    store = PostureStore(path)
    tr = SittingTracker(PostureConfig(), store=store, source_kind="live", calibration_key="k1")
    t, _ = feed(tr, T0, 100, "sitting")
    t, _ = feed(tr, t, 70, "standing")
    t, _ = feed(tr, t, 50, "sitting")
    tr.flush()
    expected = tr.summary(DAY)
    store.close()
    store = PostureStore(path)                               # reopen
    t0, t1 = day_bounds(parse_date(DAY))
    rows = store.bouts(t0, t1)
    assert [b.state for b in rows] == ["sitting", "standing", "sitting"]
    assert all(b.source_kind == "live" and b.calibration_key == "k1" and b.row_id for b in rows)
    assert rows[0].start_ts == T0 and rows[-1].end_ts == pytest.approx(t - 1)
    again = summarize_days(rows, [DAY], PostureConfig(), store.reminders(t0, t1))[DAY]
    assert again == expected
    store.close()


def test_open_bout_checkpoint(tmp_path):
    store = PostureStore(tmp_path / "p.sqlite3")
    tr = SittingTracker(PostureConfig(), store=store, source_kind="live")
    feed(tr, T0, CHECKPOINT_S + 5, "sitting")
    rows = store.bouts(T0 - 1, T0 + 3600)
    assert len(rows) == 1 and rows[0].end_ts == pytest.approx(T0 + CHECKPOINT_S)
    store.close()


def test_reminders_persist(tmp_path):
    store = PostureStore(tmp_path / "p.sqlite3")
    tr = SittingTracker(PostureConfig(reminder_after_min=1), store=store, source_kind="live")
    t, _ = feed(tr, T0, 70, "sitting")
    feed(tr, t, 61, "standing")
    rems = store.reminders(T0, T0 + 3600)
    assert len(rems) == 1 and rems[0]["kind"] == "reminder" and rems[0]["responded_after_s"] is not None
    store.close()


# -------------------------------------------------------------- service
class FakeEngine:
    """Just what PostureService uses from backend.engine.Engine."""

    def __init__(self, kind, synthetic, model=FAKE_MODEL):
        self.config = AppConfig()
        self.source_kind = kind
        self.synthetic = synthetic
        self.started_at = 1.0
        self.posture_model = model
        self.calibration_info = {"key": "live-key"} if model else None
        self.processor = SimpleNamespace(last_smoothed=PROFILE, layout=LAYOUT)
        self.running = True
        self.source_status = SimpleNamespace(finished=False)
        self.hooks = []
        self.providers = {}

    def add_frame_hook(self, hook):
        self.hooks.append(hook)

    def add_status_provider(self, name, fn):
        self.providers[name] = fn

    def frames(self, t, seconds, motion=0.5, step=0.1):
        n = int(round(seconds / step))
        for i in range(n):
            for h in self.hooks:
                h(frame(t + i * step, motion), None)
        return t + n * step


@pytest.mark.parametrize("kind,synthetic,persist", [("live", False, True), ("simulation", True, False),
                                                     ("replay", False, False), ("live", True, False)])
def test_service_persists_only_live_real_data(tmp_path, scripted, kind, synthetic, persist):
    store = PostureStore(tmp_path / "p.sqlite3")
    eng = FakeEngine(kind, synthetic)
    svc = PostureService(eng, store)
    t = eng.frames(T0, 40)
    scripted.now = ("standing", 0.95)
    t = eng.frames(t, 20)
    svc.close()
    rows = PostureStore(tmp_path / "p.sqlite3").bouts(T0 - 10, T0 + 3600)
    if persist:
        assert [b.state for b in rows] == ["unknown", "sitting", "standing"]
        assert {b.source_kind for b in rows} == {"live"} and {b.calibration_key for b in rows} == {"live-key"}
    else:
        assert rows == []


def test_service_api_shapes_and_source_change(tmp_path, scripted):
    store = PostureStore(tmp_path / "p.sqlite3")
    eng = FakeEngine("live", False)
    svc = PostureService(eng, store)
    t = eng.frames(T0, 30)
    cur = svc.current()
    assert cur["state"] == "sitting" and cur["calibrated"] is True and cur["persisting"] is True
    assert cur["synthetic"] is False and cur["source_kind"] == "live" and cur["data_source"] == "store"
    assert cur["streak"]["active"] and cur["sitting_streak_s"] > 20
    assert cur["today"]["date"] == DAY and cur["today"]["totals_s"]["sitting"] > 20
    assert "not a medical device" in cur["disclaimer"]
    status = eng.providers["posture"]()
    assert status["state"] == "sitting" and status["calibrated"] is True
    today = svc.today()
    assert today["date"] == DAY and len(today["hourly_min"]["sitting"]) == 24
    hist = svc.history(3)
    assert [d["date"] for d in hist["daily"]] == ["2024-03-03", "2024-03-04", DAY]
    assert hist["daily"][-1]["sitting_min"] > 0.3
    # a new source resets the in-memory state; the live history stays in the store
    eng.started_at, eng.source_kind, eng.synthetic = 2.0, "simulation", True
    cur = svc.current()
    assert cur["synthetic"] is True and cur["persisting"] is False and cur["data_source"] == "memory"
    assert cur["streak"]["active"] is False and cur["today"]["totals_s"]["sitting"] == 0
    eng.started_at, eng.source_kind, eng.synthetic = 3.0, None, False
    assert svc.today(parse_date(DAY))["totals_s"]["sitting"] > 20
    svc.close()


def test_service_without_model_reports_not_calibrated(tmp_path):
    eng = FakeEngine("live", False, model=None)
    svc = PostureService(eng, None)
    eng.frames(T0, 10)
    cur = svc.current()
    assert cur["state"] == "unknown" and cur["reason"] == "not calibrated" and cur["calibrated"] is False
    eng.running = False
    assert svc.current()["reason"] == "not calibrated"
