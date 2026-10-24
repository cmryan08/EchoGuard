import math

import numpy as np
import pytest

from backend.csi_parser import (
    C5C6_FIELDS,
    CLASSIC_FIELDS,
    FAMILY_C5C6,
    FAMILY_CLASSIC,
    VALID_LLTF_INDICES,
    ParseError,
    RxGain,
    RxStats,
    TxStats,
    decode_complex,
    header_family,
    lltf_amplitudes,
    parse_any,
    parse_line,
    parse_stats_line,
    valid_subcarrier_amplitudes,
)


def _classic_line(values, first_word=0, seq=1, rssi=-40):
    meta = f"CSI_DATA,{seq},1a:00:00:00:00:00,{rssi},11,1,0,1,0,1,0,0,0,0,-93,0,11,2,123456,0,67,0,{len(values)},{first_word}"
    return meta + ',"[' + ",".join(str(v) for v in values) + ']"'


def test_documented_classic_example(classic_doc_line):
    pkt = parse_line(classic_doc_line, host_ts=10.0)
    assert pkt.chip_family == FAMILY_CLASSIC
    assert pkt.host_ts == 10.0
    assert pkt.seq == 0
    assert pkt.mac == "94:d9:b3:80:8c:81"
    assert pkt.rssi == -30
    assert pkt.rate == 11
    assert pkt.sig_mode == 1
    assert pkt.mcs == 6
    assert pkt.bandwidth == 1
    assert pkt.smoothing == 0
    assert pkt.not_sounding == 1
    assert pkt.aggregation == 0
    assert pkt.stbc == 1
    assert pkt.fec_coding == 0
    assert pkt.sgi == 0
    assert pkt.noise_floor == -93
    assert pkt.ampdu_cnt == 0
    assert pkt.channel == 13
    assert pkt.secondary_channel == 2
    assert pkt.local_timestamp == 2751923
    assert pkt.ant == 0
    assert pkt.sig_len == 67
    assert pkt.rx_state == 0
    assert pkt.len == 128
    assert pkt.first_word_invalid is True
    assert pkt.fft_gain is None and pkt.agc_gain is None
    assert len(pkt.raw) == 128
    assert pkt.n_subcarriers == 64
    assert pkt.raw[:4] == [67, 48, 4, 0]


def test_documented_c5c6_example(c5c6_doc_line):
    pkt = parse_line(c5c6_doc_line)
    assert pkt.chip_family == FAMILY_C5C6
    assert pkt.seq == 7
    assert pkt.mac == "1a:00:00:00:00:00"
    assert (pkt.rssi, pkt.rate, pkt.noise_floor) == (-23, 11, -96)
    assert (pkt.fft_gain, pkt.agc_gain, pkt.channel) == (32, 4, 11)
    assert pkt.local_timestamp == 372852
    assert pkt.sig_len == 47
    assert pkt.rx_state == 0
    assert pkt.len == 256
    assert pkt.first_word_invalid is False
    assert pkt.sig_mode is None and pkt.mcs is None
    assert pkt.n_subcarriers == 128


def test_complex_decoding_order_matches_official_parser(classic_doc_line):
    # Official: complex(raw[2k+1], raw[2k]) -> real = odd index, imag = even index.
    pkt = parse_line(classic_doc_line)
    # raw[0:2] = [67, 48] -> 48 + 67j
    assert pkt.csi[0] == complex(48, 67)
    # raw[12:14] = [20, 1] -> 1 + 20j, |.| = sqrt(401)
    assert pkt.raw[12:14] == [20, 1]
    assert pkt.csi[6] == complex(1, 20)
    assert math.isclose(float(pkt.amplitude[6]), math.sqrt(401), rel_tol=1e-6)
    assert math.isclose(float(pkt.phase[6]), math.atan2(20, 1), rel_tol=1e-6)


def test_decode_complex_small():
    c = decode_complex([3, 4, -1, 2])
    assert list(c) == [complex(4, 3), complex(2, -1)]
    with pytest.raises(ValueError):
        decode_complex([1, 2, 3])


def test_variant_detection_by_field_count():
    assert len(CLASSIC_FIELDS) == 25
    assert len(C5C6_FIELDS) == 15
    assert parse_line(_classic_line([1, 2] * 64)).chip_family == FAMILY_CLASSIC


def test_lltf_valid_indices():
    assert len(VALID_LLTF_INDICES) == 52
    assert VALID_LLTF_INDICES[0] == 1 and VALID_LLTF_INDICES[25] == 26
    assert VALID_LLTF_INDICES[26] == 38 and VALID_LLTF_INDICES[-1] == 63
    assert 0 not in VALID_LLTF_INDICES and 27 not in VALID_LLTF_INDICES and 37 not in VALID_LLTF_INDICES


def test_lltf_amplitudes_and_first_word_masking():
    # subcarrier k has imag=0, real=k+1 -> amplitude k+1
    values = []
    for k in range(64):
        values += [0, k + 1]
    pkt = parse_line(_classic_line(values, first_word=0))
    amps = lltf_amplitudes(pkt)
    assert amps.shape == (64,)
    assert np.allclose(amps, np.arange(1, 65))
    valid = valid_subcarrier_amplitudes(pkt)
    assert valid.shape == (52,)
    assert np.allclose(valid, VALID_LLTF_INDICES + 1)

    masked = parse_line(_classic_line(values, first_word=1))
    amps = lltf_amplitudes(masked)
    assert np.isnan(amps[0]) and np.isnan(amps[1])
    assert np.allclose(amps[2:], np.arange(3, 65))
    valid = valid_subcarrier_amplitudes(masked)
    assert np.isnan(valid[0])  # subcarrier 1 masked
    assert np.isfinite(valid[1:]).all()


def test_lltf_short_packet_pads_nan():
    pkt = parse_line(_classic_line([0, 5] * 10))
    amps = lltf_amplitudes(pkt)
    assert np.allclose(amps[:10], 5)
    assert np.isnan(amps[10:]).all()


def test_lltf_rejects_c5c6(c5c6_doc_line):
    pkt = parse_line(c5c6_doc_line)
    with pytest.raises(ValueError):
        lltf_amplitudes(pkt)
    assert valid_subcarrier_amplitudes(pkt).shape == (128,)


@pytest.mark.parametrize(
    "line,reason",
    [
        (_classic_line([1, 2] * 64)[:200], "truncated_data"),
        ("CSI_DATA,1,1a:00:00:00:00:00,-40,11,\"[1,2]\"", "field_count"),
        (_classic_line([1, 2] * 64).replace(",-40,", ",abc,", 1), "non_numeric"),
        (_classic_line([1, 2] * 64).replace(",128,0,", ",130,0,"), "len_mismatch"),
        (_classic_line([1, 2, 3]), "odd_value_count"),
        (_classic_line([1, 2] * 4).replace('"[1,2,1,2,1,2,1,2]"', '"[1,2,x,2,1,2,1,2]"'), "bad_data"),
        (_classic_line([1, 2]).replace("1a:00:00:00:00:00", "zz:00"), "bad_mac"),
        (_classic_line([1, 2]).replace('"[1,2]"', '"[]"').replace(",2,0,\"", ",0,0,\""), "empty_data"),
    ],
)
def test_malformed_lines_raise(line, reason):
    with pytest.raises(ParseError) as ei:
        parse_line(line)
    assert ei.value.reason == reason
    parsed = parse_any(line)
    assert parsed.kind == "malformed"
    assert parsed.error.reason == reason


def test_non_csi_lines_return_none():
    assert parse_line("I (123) wifi: something") is None
    assert parse_line("") is None
    assert parse_any("").kind == "empty"
    assert parse_any("I (1) boot: ESP-IDF v5.5").kind == "other"


def test_header_recognition():
    assert header_family(",".join(CLASSIC_FIELDS)) == FAMILY_CLASSIC
    assert header_family(",".join(C5C6_FIELDS)) == FAMILY_C5C6
    # official C5/C6 firmware header uses 'rx_format' instead of 'rx_state'
    assert header_family(",".join(C5C6_FIELDS).replace("rx_state", "rx_format")) == FAMILY_C5C6
    assert parse_any(",".join(CLASSIC_FIELDS) + "\r\n").kind == "header"
    assert header_family("type,foo") is None


def test_stats_lines():
    rx = parse_stats_line("EG_RX_STATS,12000,1187,99.5,3,2,-41", host_ts=5.0)
    assert isinstance(rx, RxStats)
    assert (rx.uptime_ms, rx.csi_rx_total, rx.queue_drops, rx.seq_gaps, rx.last_rssi) == (12000, 1187, 3, 2, -41)
    assert rx.csi_rx_rate_hz == pytest.approx(99.5)
    assert rx.host_ts == 5.0
    tx = parse_stats_line("\x1b[0mEG_TX_STATS,5000,498,2,100.0\r\n")
    assert isinstance(tx, TxStats)
    assert (tx.uptime_ms, tx.sent_ok, tx.send_fail) == (5000, 498, 2)
    assert tx.rate_hz == pytest.approx(100.0)
    assert parse_stats_line("EG_RX_STATS,1,2") is None
    assert parse_stats_line("EG_RX_STATS,a,b,c,d,e,f") is None
    assert parse_any("EG_RX_STATS,12000,1187,99.5,3,2,-41").kind == "stats"


def test_rx_gain_line():
    g = parse_stats_line("EG_RX_GAIN,15000,37,-4,1250,40,0,0,1\r\n", host_ts=7.0)
    assert isinstance(g, RxGain)
    assert (g.uptime_ms, g.agc_gain, g.fft_gain, g.baseline_agc, g.baseline_fft) == (15000, 37, -4, 40, 0)
    assert g.compensate_gain == pytest.approx(1.25)
    assert g.compensation_applied is False and g.force_gain is True
    assert g.host_ts == 7.0
    # baseline not yet known (first 100 packets after boot): sentinels -> None
    u = parse_stats_line("\x1b[0mEG_RX_GAIN,900,30,2,180,-1,-128,1,0")
    assert u.baseline_agc is None and u.baseline_fft is None
    assert u.compensate_gain == pytest.approx(0.18)
    assert u.compensation_applied is True and u.force_gain is False
    # malformed: wrong field count or non-numeric
    assert parse_stats_line("EG_RX_GAIN,1,2,3") is None
    assert parse_stats_line("EG_RX_GAIN,1,2,3,4,5,6,7,8,9") is None
    assert parse_stats_line("EG_RX_GAIN,1,a,3,4,5,6,7,8") is None
    p = parse_any("EG_RX_GAIN,15000,37,-4,1250,40,0,0,1")
    assert p.kind == "stats" and isinstance(p.stats, RxGain)
    assert parse_any("EG_RX_GAIN,1,2").kind == "other"


def test_crlf_ansi_and_log_prefix_tolerated(classic_doc_line):
    for variant in (
        classic_doc_line + "\r\n",
        "\x1b[0;32m" + classic_doc_line + "\x1b[0m\r\n",
        "I (2999) csi_recv: " + classic_doc_line,
        (classic_doc_line + "\r\n").encode(),
    ):
        pkt = parse_line(variant)
        assert pkt is not None and pkt.len == 128 and pkt.seq == 0
        assert pkt.raw_line.startswith("CSI_DATA,0,")


def test_fixture_file_counts(classic_fixture, c5c6_fixture):
    kinds = [parse_any(l).kind for l in classic_fixture.read_text().splitlines()]
    assert kinds.count("csi") == 13
    assert kinds.count("malformed") == 4
    assert kinds.count("header") == 1
    assert kinds.count("stats") == 1
    kinds = [parse_any(l).kind for l in c5c6_fixture.read_text().splitlines()]
    assert kinds.count("csi") == 7
    assert kinds.count("malformed") == 1
    assert kinds.count("header") == 1
