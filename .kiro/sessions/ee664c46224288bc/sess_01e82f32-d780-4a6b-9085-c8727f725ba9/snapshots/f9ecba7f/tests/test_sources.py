import threading
import time

import serial

from backend.buffer import CSIBuffer
from backend.recorder import RAW_FIELDS, RawRecorder, export_amplitudes_csv
from backend.sources import ReplaySource, SerialSource


def _collect(source, n=None, timeout=5.0):
    out = []
    deadline = time.time() + timeout
    for item in source.lines():
        out.append(item)
        if (n is not None and len(out) >= n) or time.time() > deadline:
            source.stop()
            break
    return out


def test_replay_raw_log_fixture(classic_fixture):
    src = ReplaySource(classic_fixture, speed=0)
    assert src.recording_format == "raw_log"
    lines = _collect(src)
    expected = classic_fixture.read_text().splitlines()
    assert [l for _, l in lines] == [l.rstrip("\r") for l in expected]
    # synthesised timeline at the default 100 lines/s
    assert lines[1][0] - lines[0][0] == __import__("pytest").approx(0.01)
    assert src.status.finished


def test_replay_raw_log_paced(classic_fixture):
    src = ReplaySource(classic_fixture, speed=1.0, line_rate_hz=200.0)
    t0 = time.time()
    lines = _collect(src)
    dt = time.time() - t0
    n = len(lines)
    assert dt >= (n - 1) / 200.0 * 0.8


def test_recorder_replay_round_trip(tmp_path, classic_fixture):
    rec_path = tmp_path / "session" / "raw.csv"
    raw_lines = classic_fixture.read_text().splitlines()
    with RawRecorder(rec_path, extra_fields={"label": "walking"}) as rec:
        for i, line in enumerate(raw_lines):
            rec.write(1000.0 + i * 0.02, line)
    header = rec_path.read_text().splitlines()[0]
    assert header.split(",")[: len(RAW_FIELDS)] == RAW_FIELDS
    assert header.endswith("label")

    src = ReplaySource(rec_path, speed=0)
    assert src.recording_format == "recording"
    replayed = _collect(src)
    assert [l for _, l in replayed] == [l.rstrip("\r") for l in raw_lines]
    # original relative timing preserved
    assert replayed[5][0] - replayed[0][0] == __import__("pytest").approx(0.1, abs=1e-4)

    # same pipeline: identical parse results as the raw fixture
    a, b = CSIBuffer(), CSIBuffer()
    for ts, l in replayed:
        a.ingest(ts, l)
    for i, l in enumerate(raw_lines):
        b.ingest(float(i), l)
    sa, sb = a.snapshot(), b.snapshot()
    assert (sa.csi_packets, sa.malformed, sa.seq_gaps) == (sb.csi_packets, sb.malformed, sb.seq_gaps) == (13, 4, 4)

    out = tmp_path / "amps.csv"
    rows = export_amplitudes_csv(a.packets(), out)
    assert rows == 13
    head = out.read_text().splitlines()[0].split(",")
    assert head[:3] == ["host_ts_unix", "seq", "chip_family"]
    assert len([h for h in head if h.startswith("lltf_")]) == 52


def test_replay_loop(c5c6_fixture):
    src = ReplaySource(c5c6_fixture, speed=0, loop=True)
    n_file = len(c5c6_fixture.read_text().splitlines())
    lines = _collect(src, n=n_file * 2 + 1)
    assert len(lines) == n_file * 2 + 1
    ts = [t for t, _ in lines]
    assert all(b > a for a, b in zip(ts, ts[1:]))


class FakeSerial:
    def __init__(self, chunks, fail_at_end=True):
        self.chunks = list(chunks)
        self.fail_at_end = fail_at_end
        self.closed = False

    def readline(self):
        if self.chunks:
            return self.chunks.pop(0)
        if self.fail_at_end:
            raise serial.SerialException("device disconnected")
        time.sleep(0.01)
        return b""

    def close(self):
        self.closed = True


def test_serial_reconnect_with_fake_serial():
    sessions = [
        serial.SerialException("could not open port"),
        FakeSerial([b"CSI_DATA,1,a", b"bc\r\n", b"line2\n"]),  # split line across reads
        FakeSerial([b"after-reconnect\n"], fail_at_end=False),
    ]
    opened = []

    def factory(port, baud, timeout):
        opened.append((port, baud))
        item = sessions.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    src = SerialSource("/dev/cu.fake", baud=921600, backoff_initial=0.01, backoff_max=0.02, serial_factory=factory)
    lines = _collect(src, n=3)
    assert [l for _, l in lines] == ["CSI_DATA,1,abc", "line2", "after-reconnect"]
    assert src.status.reconnect_count == 1
    assert src.status.bytes_read == len(b"CSI_DATA,1,abc\r\nline2\nafter-reconnect\n")
    assert opened[0] == ("/dev/cu.fake", 921600)
    assert not src.status.connected  # closed after stop


def test_serial_source_with_pyserial_loop_url(classic_doc_line):
    def factory(port, baud, timeout):
        ser = serial.serial_for_url("loop://", baudrate=baud, timeout=timeout)
        ser.write(b"I (1) boot\r\n" + classic_doc_line.encode() + b"\r\n")
        return ser

    src = SerialSource("loop://", read_timeout=0.05, serial_factory=factory)
    buf = CSIBuffer()
    for ts, line in _collect(src, n=2):
        buf.ingest(ts, line)
    snap = buf.snapshot()
    assert snap.csi_packets == 1
    assert snap.non_csi_lines == 1
    assert src.status.connected is False


def test_serial_auto_port_error_is_reported_not_raised():
    from backend.ports import PortSelectionError

    def resolver():
        raise PortSelectionError("No likely ESP32 serial port found.")

    src = SerialSource("auto", backoff_initial=0.01, backoff_max=0.01, port_resolver=resolver)
    t = threading.Thread(target=lambda: list(src.lines()), daemon=True)
    t.start()
    time.sleep(0.1)
    src.stop()
    t.join(timeout=1.0)
    assert not t.is_alive()
    assert "No likely ESP32" in src.status.last_error
    assert src.status.connected is False
