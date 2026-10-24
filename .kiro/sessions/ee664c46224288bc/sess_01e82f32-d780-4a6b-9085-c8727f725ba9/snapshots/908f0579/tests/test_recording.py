import csv
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from backend.csi_parser import parse_any
from backend.engine import Engine
from backend.recording import (LABELS, TAKE_FIELDS, RecordingError, RecordingManager, SessionStore,
                               validate_placement, validate_session_id)
from backend.sources import ReplaySource, SourceStatus
from csi_synth import GatedReplay, classic_line, stream

SENSOR = {"tx_mac": "1a:00:00:00:00:00", "channel": 11, "secondary_channel": 2, "chip_family": "classic",
          "csi_len": 128, "n_subcarriers": 52}


class FakeEngine:
    """Just the surface RecordingManager uses."""

    def __init__(self, kind="replay", synthetic=False, connected=True):
        self.recorder = None
        self.source_kind = kind
        self.synthetic = synthetic
        self.started_at = 1.0
        self.running = True
        self.source_status = SourceStatus(kind="x", connected=connected)

    def set_recorder(self, rec):
        prev, self.recorder = self.recorder, rec
        return prev

    def sensor_config(self):
        return dict(SENSOR)

    def feed(self, lines, t0=None):
        t0 = time.time() if t0 is None else t0
        for i, line in enumerate(lines):
            ts = t0 + i * 0.01
            if self.recorder is not None:
                self.recorder.write(ts, line, parse_any(line, ts))


def gen_lines(n=50, perturb=0.0):
    return [classic_line(a, seq=i) for i, (_, a) in enumerate(stream(n, noise=0.5, perturb=perturb, seed=n))]


def test_labels_exact():
    assert LABELS == {
        "empty_room": "Empty room",
        "standing_still": "Standing still",
        "walking": "Walking",
        "sitting": "Sitting",
        "lying_down": "Lying down",
        "rapid_sitting": "Rapid sitting",
        "simulated_fall": "Simulated fall",
    }
    assert list(LABELS) == ["empty_room", "standing_still", "walking", "sitting", "lying_down",
                            "rapid_sitting", "simulated_fall"]


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "x" * 41, "..", "-lead", None, 5, "semi;colon"])
def test_placement_validation(bad):
    with pytest.raises(RecordingError):
        validate_placement(bad)


def test_placement_slug_and_session_id():
    assert validate_placement("Living Room") == "living-room"
    assert validate_placement("lab_2") == "lab_2"
    for bad in ("../20240101-000000-x", "20240101-000000-X", "foo", "20240101-000000-a/b"):
        with pytest.raises(RecordingError):
            validate_session_id(bad)


def test_session_layout_and_take_csv(tmp_path):
    eng = FakeEngine()
    mgr = RecordingManager(eng, SessionStore(tmp_path / "recordings"))
    with pytest.raises(RecordingError, match="session"):
        mgr.start_take("walking")
    info = mgr.create_session("Living room", "test notes")
    sid = info["session_id"]
    assert sid.endswith("-living-room") and len(sid.split("-")[0]) == 8
    sdir = tmp_path / "recordings" / sid
    meta = json.loads((sdir / "session.json").read_text())
    for key in ("session_id", "created", "sensor", "source_kind", "synthetic", "notes", "app_version", "takes"):
        assert key in meta
    assert meta["sensor"] == SENSOR and meta["synthetic"] is False and meta["notes"] == "test notes"

    with pytest.raises(RecordingError, match="label"):
        mgr.start_take("dancing")
    take = mgr.start_take("walking")
    assert take["take_id"] == "t001"
    lines = gen_lines(30) + ["EG_RX_STATS,3000,11,9.7,0,1,-32", "CSI_DATA,broken"]
    eng.feed(lines)
    with pytest.raises(RecordingError, match="already running"):
        mgr.start_take("sitting")
    entry = mgr.stop_take()
    assert eng.recorder is None
    assert entry["packets"] == 30 and entry["lines"] == 32 and entry["label"] == "walking"
    assert entry["file"].startswith("001_walking_") and entry["file"].endswith(".csv")

    with open(sdir / entry["file"], newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == TAKE_FIELDS
    assert TAKE_FIELDS == ["host_ts_iso", "host_ts_unix", "session_id", "take_id", "label", "raw_line",
                           "rssi", "id", "channel", "len", "local_timestamp"]
    first = dict(zip(rows[0], rows[1]))
    assert first["session_id"] == sid and first["take_id"] == "t001" and first["label"] == "walking"
    assert first["raw_line"] == lines[0]
    assert first["rssi"] == "-40" and first["id"] == "0" and first["channel"] == "11" and first["len"] == "128"
    assert first["host_ts_iso"].endswith("+00:00") and float(first["host_ts_unix"]) > 0
    stats_row = dict(zip(rows[0], rows[31]))
    assert stats_row["raw_line"].startswith("EG_RX_STATS") and stats_row["rssi"] == ""

    meta = json.loads((sdir / "session.json").read_text())
    assert len(meta["takes"]) == 1
    t = meta["takes"][0]
    for key in ("label", "start", "end", "packets", "duration_s", "file", "take_id"):
        assert key in t
    assert t["packets"] == 30 and t["duration_s"] >= 0

    # second take with a fixed duration stops itself
    mgr.start_take("empty_room", duration_s=0.05)
    eng.feed(gen_lines(20))
    st = mgr.status()
    assert st["active_take"] is None and st["last_take"]["stop_reason"] == "duration"
    assert len(st["takes"]) == 2 and st["takes"][1]["file"].startswith("002_empty_room_")

    # delete
    with pytest.raises(RecordingError):
        mgr.delete_take("../t001")
    mgr.delete_take("t001")
    assert not (sdir / entry["file"]).exists()
    assert [t["take_id"] for t in json.loads((sdir / "session.json").read_text())["takes"]] == ["t002"]
    assert mgr.start_take("sitting")["take_id"] == "t003"      # indices are never reused
    mgr.stop_take()


def test_recording_refused_when_live_disconnected(tmp_path):
    eng = FakeEngine(kind="live", connected=False)
    mgr = RecordingManager(eng, SessionStore(tmp_path))
    mgr.create_session("lab")
    with pytest.raises(RecordingError, match="disconnected"):
        mgr.start_take("walking")
    assert mgr.status()["can_record"] is False
    eng.source_status.connected = True
    mgr.start_take("walking")
    mgr.stop_take()


def test_synthetic_and_real_not_mixed(tmp_path):
    eng = FakeEngine(kind="replay", synthetic=False)
    mgr = RecordingManager(eng, SessionStore(tmp_path))
    mgr.create_session("lab")
    eng.synthetic, eng.source_kind = True, "simulation"
    with pytest.raises(RecordingError, match="synthetic"):
        mgr.start_take("walking")
    info = mgr.create_session("lab")
    assert info["synthetic"] is True
    mgr.start_take("simulated_fall")
    mgr.stop_take()


def test_source_change_ends_take(tmp_path):
    eng = FakeEngine()
    mgr = RecordingManager(eng, SessionStore(tmp_path))
    mgr.create_session("lab")
    mgr.start_take("walking")
    eng.feed(gen_lines(5))
    eng.started_at = 2.0                     # a new source was started
    eng.feed(gen_lines(5))
    st = mgr.status()
    assert st["active_take"] is None
    assert st["last_take"]["stop_reason"] == "source_changed" and st["last_take"]["packets"] == 5


def test_session_list_and_select(tmp_path):
    store = SessionStore(tmp_path)
    mgr = RecordingManager(FakeEngine(), store)
    a = mgr.create_session("one")
    b = mgr.create_session("one")                      # same second -> unique id
    assert a["session_id"] != b["session_id"]
    ids = {s["session_id"] for s in mgr.list_sessions()}
    assert ids == {a["session_id"], b["session_id"]}
    assert mgr.select_session(a["session_id"])["session_id"] == a["session_id"]
    with pytest.raises(RecordingError):
        mgr.select_session("../../etc")
    with pytest.raises(RecordingError):
        mgr.select_session("20240101-000000-missing")


def _parsed_csi(lines):
    out = []
    for line in lines:
        p = parse_any(line, 0.0)
        if p.kind == "csi":
            out.append(p.packet)
    return out


def test_engine_take_then_replay_reproduces_packets(tmp_path, classic_fixture):
    eng = Engine()
    mgr = RecordingManager(eng, SessionStore(tmp_path / "recordings"))
    gate = threading.Event()
    try:
        mgr.create_session("bench")
        eng.start(GatedReplay(classic_fixture, gate, speed=0))
        mgr.start_take("standing_still")
        gate.set()
        assert eng.join(10)
        st = mgr.status()                           # source finished -> take closed
        assert st["active_take"] is None
        entry = st["takes"][0]
        assert entry["stop_reason"] == "source_ended"
        assert entry["packets"] == 13
        take_csv = tmp_path / "recordings" / st["session"]["session_id"] / entry["file"]

        original = classic_fixture.read_text(encoding="utf-8").splitlines()
        replayed = [line for _, line in ReplaySource(take_csv, speed=0).lines()]
        assert replayed == original
        a, b = _parsed_csi(original), _parsed_csi(replayed)
        assert len(a) == len(b) == 13
        for p, q in zip(a, b):
            assert p.seq == q.seq and p.rssi == q.rssi and p.raw_line == q.raw_line
            np.testing.assert_array_equal(p.amplitude, q.amplitude)

        # through the engine again: same counters, label shown as ground truth
        eng.start(ReplaySource(take_csv, speed=0))
        assert eng.join(10)
        snap = eng.snapshot()
        assert snap["counters"]["packets"] == 13
        assert snap["counters"]["malformed"] == 4
        assert snap["counters"]["seq_gaps"] == 4
        assert snap["ground_truth"]["label"] == "standing_still"
        assert snap["ground_truth"]["label_name"] == "Standing still"
        assert snap["source"]["synthetic"] is False
    finally:
        eng.stop()


def test_session_replay_plays_all_takes_in_order(tmp_path):
    eng = FakeEngine()
    store = SessionStore(tmp_path)
    mgr = RecordingManager(eng, store)
    sid = mgr.create_session("lab")["session_id"]
    walk, sit = gen_lines(10), gen_lines(12, perturb=0.1)
    mgr.start_take("walking")
    eng.feed(walk)
    mgr.stop_take()
    mgr.start_take("sitting")
    eng.feed(sit)
    mgr.stop_take()

    src = ReplaySource(tmp_path / sid, speed=0)
    assert src.recording_format == "session"
    seen = []
    for _, line in src.lines():
        seen.append((src.status.extra["label"], line))
    assert [line for _, line in seen] == walk + sit
    assert [lab for lab, _ in seen] == ["walking"] * 10 + ["sitting"] * 12
    assert src.status.extra["label_name"] == "Sitting"


def test_synthetic_session_replay_is_marked_synthetic(tmp_path):
    eng = FakeEngine(kind="simulation", synthetic=True)
    mgr = RecordingManager(eng, SessionStore(tmp_path))
    sid = mgr.create_session("sim")["session_id"]
    mgr.start_take("simulated_fall")
    eng.feed(gen_lines(5))
    entry = mgr.stop_take()
    assert ReplaySource(tmp_path / sid).synthetic is True
    assert ReplaySource(tmp_path / sid / entry["file"]).synthetic is True
    meta = json.loads((tmp_path / sid / "session.json").read_text())
    assert meta["synthetic"] is True and meta["takes"][0]["synthetic"] is True


def test_raw_recorder_csv_still_replays(tmp_path):
    from backend.recorder import RawRecorder

    path = tmp_path / "raw.csv"
    with RawRecorder(path) as rec:
        for i, line in enumerate(gen_lines(5)):
            rec.write(100.0 + i, line)
    src = ReplaySource(path, speed=0)
    assert src.recording_format == "recording"
    assert "label" not in src.status.extra
    assert len(list(src.lines())) == 5


def test_ground_truth_absent_for_raw_logs(classic_fixture):
    eng = Engine()
    try:
        eng.start(ReplaySource(classic_fixture, speed=0))
        assert eng.join(10)
        assert eng.snapshot()["ground_truth"] is None
    finally:
        eng.stop()
