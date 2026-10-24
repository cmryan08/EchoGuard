import threading

import pytest

from backend.buffer import CSIBuffer
from backend.csi_parser import parse_line


def _line(seq, rssi=-40, n_sub=64):
    vals = ",".join(["0,10"] * n_sub)
    return (f"CSI_DATA,{seq},1a:00:00:00:00:00,{rssi},11,1,0,1,0,1,0,0,0,0,-93,0,11,2,1000,0,67,0,"
            f"{2 * n_sub},0,\"[{vals}]\"")


def test_bounded_size():
    buf = CSIBuffer(maxlen=10)
    for i in range(25):
        buf.ingest(i * 0.01, _line(i))
    assert len(buf) == 10
    assert [p.seq for p in buf.packets()] == list(range(15, 25))
    assert [p.seq for p in buf.packets(last_n=3)] == [22, 23, 24]
    snap = buf.snapshot()
    assert snap.csi_packets == 25
    assert snap.buffered == 10


def test_rate_over_sliding_window():
    buf = CSIBuffer(rate_window_s=2.0)
    for i in range(500):  # 100 Hz for 5 s
        buf.ingest(1000.0 + i * 0.01, _line(i))
    rate = buf.snapshot().packet_rate_hz
    assert rate == pytest.approx(100.0, rel=0.02)
    # no packets for 3 s -> rate decays to zero when a wall-clock 'now' is supplied
    assert buf.snapshot(now=1000.0 + 5 + 3).packet_rate_hz == 0
    assert CSIBuffer().snapshot().packet_rate_hz is None


def test_seq_gaps_and_wraparound():
    buf = CSIBuffer()
    seqs = [1, 2, 3, 5, 6, 10]  # 1 + 3 missing
    for i, s in enumerate(seqs):
        buf.ingest(float(i), _line(s))
    assert buf.snapshot().seq_gaps == 4

    wrap = CSIBuffer()
    for i, s in enumerate([2 ** 32 - 2, 2 ** 32 - 1, 0, 2]):  # wrap, then 1 missing
        wrap.ingest(float(i), _line(s))
    snap = wrap.snapshot()
    assert snap.seq_gaps == 1
    assert snap.seq_resets == 0

    reset = CSIBuffer()
    for i, s in enumerate([500, 501, 0, 1, 1]):  # TX reboot, then a duplicate
        reset.ingest(float(i), _line(s))
    snap = reset.snapshot()
    assert snap.seq_gaps == 0
    assert snap.seq_resets == 1
    assert snap.duplicates == 1


def test_counters_and_diagnostics():
    buf = CSIBuffer()
    buf.ingest(0.0, "I (1) boot: hello")
    buf.ingest(0.1, "type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,"
                    "fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,"
                    "rx_state,len,first_word,data")
    buf.ingest(0.2, _line(0, rssi=-40))
    buf.ingest(0.3, _line(1, rssi=-44))
    buf.ingest(0.4, _line(2)[:150])
    buf.ingest(0.5, "EG_RX_STATS,1000,2,2.0,0,0,-44")
    buf.ingest(0.6, "EG_TX_STATS,1000,100,0,100.0")
    buf.ingest(0.7, "")
    s = buf.snapshot()
    assert s.total_lines == 8
    assert s.csi_packets == 2
    assert s.malformed == 1
    assert s.malformed_reasons == {"truncated_data": 1}
    assert s.non_csi_lines == 4
    assert s.header_family == "classic"
    assert s.last_rssi == -44
    assert s.rssi_mean == pytest.approx(-42.0)
    assert s.rssi_std == pytest.approx(2.0)
    assert s.noise_floor == -93
    assert s.last_len == 128
    assert s.chip_family == "classic"
    assert s.last_rx_stats.csi_rx_total == 2
    assert s.last_tx_stats.sent_ok == 100


def test_add_packet_and_thread_safety():
    buf = CSIBuffer(maxlen=100)
    pkt = parse_line(_line(0), host_ts=0.0)
    buf.add_packet(pkt)
    assert buf.latest() is pkt

    def writer(off):
        for i in range(500):
            buf.ingest(off + i * 0.001, _line(i))

    threads = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for _ in range(200):
        buf.snapshot()
        buf.packets(last_n=10)
    for t in threads:
        t.join()
    assert buf.snapshot().csi_packets == 1 + 4 * 500
    assert len(buf) == 100
