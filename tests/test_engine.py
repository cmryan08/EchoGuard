import json
import time

import pytest

from backend.engine import Engine
from backend.recorder import RawRecorder
from backend.sources import LineSource, ReplaySource
from csi_synth import classic_line, stream


class ListSource(LineSource):
    """In-memory source yielding pre-built (ts, line) tuples."""

    kind = "replay"

    def __init__(self, items, fail_after=None):
        super().__init__()
        self.items = list(items)
        self.fail_after = fail_after

    def lines(self):
        self.status.connected = True
        for i, item in enumerate(self.items):
            if self._stop.is_set():
                break
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("simulated source failure")
            self.status.lines_read += 1
            yield item
        self.status.connected = False


@pytest.fixture
def engine():
    eng = Engine()
    yield eng
    eng.stop()


def generated_items(n=300, **kw):
    return [(ts, classic_line(amps, seq=i)) for i, (ts, amps) in enumerate(stream(n, **kw))]


def test_no_source_snapshot_has_no_fabricated_values(engine):
    snap = engine.snapshot()
    json.dumps(snap, allow_nan=False)
    assert snap["source"]["kind"] is None
    assert snap["source"]["synthetic"] is False
    assert snap["packet_rate_hz"] is None
    assert snap["rssi"] == {"last": None, "mean": None, "std": None}
    assert snap["noise_floor"] is None
    assert snap["motion"] is None
    assert snap["last_packet_age_s"] is None
    assert snap["quality"]["ok"] is None
    assert snap["activity"]["key"] == "unknown"
    assert all(v is None for k, v in snap["config"].items())
    assert snap["counters"]["packets"] == 0
    assert engine.heatmap()["rows"] == []
    series = engine.series(30)
    assert series["ts"] == [] and series["motion"] == []


def test_replay_fixture_through_engine(engine, classic_fixture):
    engine.start(ReplaySource(classic_fixture, speed=0))
    assert engine.join(5.0)
    snap = engine.snapshot()
    json.dumps(snap, allow_nan=False)
    c = snap["counters"]
    assert c["packets"] == 13
    assert c["malformed"] == 4
    assert c["seq_gaps"] == 4
    assert snap["source"]["kind"] == "replay"
    assert snap["source"]["synthetic"] is False
    assert snap["source"]["finished"] is True
    assert snap["config"]["chip_family"] == "classic"
    assert snap["config"]["channel"] == 11
    assert snap["config"]["csi_len"] == 128
    assert snap["config"]["rx_stats"]["csi_rx_total"] == 11
    assert snap["rssi"]["last"] is not None
    # 13 packets in ~0.2 s: not enough for a classification
    assert snap["quality"]["ok"] is False
    assert snap["activity"]["key"] == "unknown"

    heat = engine.heatmap(300)
    json.dumps(heat, allow_nan=False)
    assert len(heat["rows"]) == 13
    assert heat["n_subcarriers"] == len(heat["labels"]) == len(heat["rows"][0])
    assert heat["vmin"] is not None and heat["vmax"] >= heat["vmin"]
    series = engine.series(30)
    json.dumps(series, allow_nan=False)
    assert len(series["ts"]) >= 1


def test_rx_gain_exposed_in_snapshot(engine):
    items = generated_items(20)
    items.insert(5, (items[5][0], "EG_RX_GAIN,15000,37,-4,350,40,0,1,0"))
    engine.start(ListSource(items))
    assert engine.join(5.0)
    snap = engine.snapshot()
    json.dumps(snap, allow_nan=False)
    g = snap["config"]["rx_gain"]
    assert g["agc_gain"] == 37 and g["fft_gain"] == -4
    assert g["compensate_gain"] == pytest.approx(0.35)
    assert g["baseline_agc"] == 40 and g["baseline_fft"] == 0
    assert g["compensation_applied"] is True and g["force_gain"] is False
    assert g["compensation_scaled_down"] is True
    # gain lines are not mistaken for transmitter stats
    assert snap["config"]["tx_stats"] is None


def test_rx_gain_absent_with_older_firmware(engine):
    engine.start(ListSource(generated_items(20)))
    assert engine.join(5.0)
    assert engine.snapshot()["config"]["rx_gain"] is None


def test_generated_stream_classified(engine):
    # host timestamps are "now" so the wall-clock staleness check passes
    t0 = time.time() - 3.0
    engine.start(ListSource(generated_items(300, t0=t0)))
    assert engine.join(5.0)
    snap = engine.snapshot()
    assert snap["quality"]["ok"] is True, snap["quality"]
    assert snap["activity"] == {"key": "empty_low", "label": "Empty room / low activity"}
    assert snap["motion"]["motion_score"] < 0.01
    assert snap["config"]["n_subcarriers"] == 52
    series = engine.series(30)
    assert len(series["ts"]) >= 25
    assert set(series["activity"]) <= {"empty_low", "unknown"}


def test_source_exception_is_recorded_not_fatal(engine):
    engine.start(ListSource(generated_items(50), fail_after=20))
    assert engine.join(5.0)
    snap = engine.snapshot()
    assert "simulated source failure" in snap["source"]["engine_error"]
    assert snap["counters"]["packets"] == 20
    # the engine can be restarted afterwards
    engine.switch_source(ListSource(generated_items(30)))
    assert engine.join(5.0)
    snap = engine.snapshot()
    assert snap["source"]["engine_error"] is None
    assert snap["counters"]["packets"] == 30


def test_bad_line_does_not_stop_acquisition(engine):
    items = generated_items(20)
    items.insert(5, (items[5][0], "CSI_DATA,garbage"))
    items.insert(8, (items[8][0], "\x00\x01 random noise"))
    engine.start(ListSource(items))
    assert engine.join(5.0)
    snap = engine.snapshot()
    assert snap["counters"]["packets"] == 20
    assert snap["counters"]["malformed"] == 1


def test_recorder_hook_receives_raw_lines(engine, classic_fixture, tmp_path):
    rec = RawRecorder(tmp_path / "rec.csv")
    engine.set_recorder(rec)
    engine.start(ReplaySource(classic_fixture, speed=0))
    assert engine.join(5.0)
    assert engine.snapshot()["recording"]["active"] is True
    assert engine.set_recorder(None) is rec
    rec.close()
    assert rec.rows_written == 22
    # the recording replays through the same pipeline with identical counters
    engine.start(ReplaySource(tmp_path / "rec.csv", speed=0))
    assert engine.join(5.0)
    c = engine.snapshot()["counters"]
    assert (c["packets"], c["malformed"]) == (13, 4)


def test_frame_hooks_called(engine):
    seen = []
    engine.add_frame_hook(lambda frame, activity: seen.append(activity.value))
    engine.start(ListSource(generated_items(100)))
    assert engine.join(5.0)
    assert len(seen) >= 5
