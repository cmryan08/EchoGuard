"""Parser for the official Espressif esp-csi ``CSI_DATA`` serial line format.

Two variants are printed by ``examples/get-started/csi_recv`` (and by the
EchoGuard receiver firmware, which keeps the same layout):

* classic ESP32 / ESP32-S3 / ESP32-C3, 25 comma separated fields::

    type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,
    aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,
    secondary_channel,local_timestamp,ant,sig_len,rx_state,len,first_word,data

* ESP32-C5 / ESP32-C6 / ESP32-C61, 15 fields::

    type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,
    local_timestamp,sig_len,rx_state,len,first_word,data

``data`` is a quoted JSON-style list of ``len`` signed integers, interleaved
``[imag0, real0, imag1, real1, ...]`` per subcarrier. The official parser
(``tools/csi_data_read_parse.py``) builds ``complex(raw[2k+1], raw[2k])``, i.e.
``real = raw[2k+1]`` and ``imag = raw[2k]``; this module does the same.

On classic chips the first 128 values are the LLTF (64 subcarriers) in the
order subcarrier 0..31 followed by -32..-1. Index 0 is DC; the 52 subcarriers
that carry legacy 20 MHz data/pilots are indices 1..26 and 38..63. When
``first_word`` (first_word_invalid) is 1 the first four bytes, i.e. subcarriers
0 and 1, are invalid and are masked as NaN.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

CLASSIC_FIELDS = [
    "type", "id", "mac", "rssi", "rate", "sig_mode", "mcs", "bandwidth", "smoothing",
    "not_sounding", "aggregation", "stbc", "fec_coding", "sgi", "noise_floor", "ampdu_cnt",
    "channel", "secondary_channel", "local_timestamp", "ant", "sig_len", "rx_state", "len",
    "first_word", "data",
]
C5C6_FIELDS = [
    "type", "seq", "mac", "rssi", "rate", "noise_floor", "fft_gain", "agc_gain", "channel",
    "local_timestamp", "sig_len", "rx_state", "len", "first_word", "data",
]

CSI_TAG = "CSI_DATA"
FAMILY_CLASSIC = "classic"
FAMILY_C5C6 = "c5c6"

LLTF_SUBCARRIERS = 64
LLTF_VALUES = 2 * LLTF_SUBCARRIERS
#: LLTF subcarrier indices (0..63, order 0..31 then -32..-1) carrying legacy data/pilots.
VALID_LLTF_INDICES = np.array(list(range(1, 27)) + list(range(38, 64)), dtype=np.int64)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")


class ParseError(ValueError):
    """A line that contains ``CSI_DATA`` but cannot be decoded.

    ``reason`` is a short machine readable code (``field_count``,
    ``non_numeric``, ``bad_mac``, ``bad_data``, ``truncated_data``,
    ``len_mismatch``, ``odd_value_count``, ``empty_data``).
    """

    def __init__(self, reason: str, detail: str, line: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.line = line


@dataclass
class CSIPacket:
    """One decoded CSI measurement. Field names follow the official headers."""

    host_ts: Optional[float]
    chip_family: str
    seq: int                      # 'id' (classic) or 'seq' (C5/C6): TX packet counter
    mac: str
    rssi: int
    rate: int
    noise_floor: int
    channel: int
    local_timestamp: int          # receiver clock, microseconds
    sig_len: int
    rx_state: int
    len: int                      # number of int values in 'data'
    first_word_invalid: bool
    raw: list
    csi: np.ndarray               # complex64, one value per subcarrier
    amplitude: np.ndarray         # float32 |csi|
    phase: np.ndarray             # float32 angle(csi), radians
    raw_line: str
    # classic-only header fields (None on C5/C6)
    sig_mode: Optional[int] = None
    mcs: Optional[int] = None
    bandwidth: Optional[int] = None
    smoothing: Optional[int] = None
    not_sounding: Optional[int] = None
    aggregation: Optional[int] = None
    stbc: Optional[int] = None
    fec_coding: Optional[int] = None
    sgi: Optional[int] = None
    ampdu_cnt: Optional[int] = None
    secondary_channel: Optional[int] = None
    ant: Optional[int] = None
    # C5/C6-only header fields (None on classic)
    fft_gain: Optional[int] = None
    agc_gain: Optional[int] = None

    @property
    def n_subcarriers(self) -> int:
        return int(self.csi.shape[0])


@dataclass
class RxStats:
    """``EG_RX_STATS`` line printed once per second by the EchoGuard receiver."""

    uptime_ms: int
    csi_rx_total: int
    csi_rx_rate_hz: float
    queue_drops: int
    seq_gaps: int
    last_rssi: int
    host_ts: Optional[float] = None


@dataclass
class TxStats:
    """``EG_TX_STATS`` line printed once per second by the EchoGuard transmitter."""

    uptime_ms: int
    sent_ok: int
    send_fail: int
    rate_hz: float
    host_ts: Optional[float] = None


@dataclass
class RxGain:
    """``EG_RX_GAIN`` line printed once per second by the EchoGuard receiver.

    ``compensate_gain`` is esp_csi_gain_ctrl's factor relative to the boot-time
    AGC/FFT baseline; ``compensation_applied`` says whether the firmware
    multiplied the printed CSI by it. Baselines are None until known.
    """

    uptime_ms: int
    agc_gain: int
    fft_gain: int
    compensate_gain: float
    baseline_agc: Optional[int]
    baseline_fft: Optional[int]
    compensation_applied: bool
    force_gain: bool
    host_ts: Optional[float] = None


#: Sentinels printed by the firmware while the gain baseline is unknown.
RX_GAIN_UNKNOWN_AGC = -1
RX_GAIN_UNKNOWN_FFT = -128


@dataclass
class ParsedLine:
    """Result of :func:`parse_any`. ``kind`` is csi|malformed|stats|header|other|empty."""

    kind: str
    line: str
    packet: Optional[CSIPacket] = None
    error: Optional[ParseError] = None
    stats: Optional[object] = None
    header_family: Optional[str] = None
    extra: dict = field(default_factory=dict)


def clean_line(line) -> str:
    """Decode bytes, strip ANSI colour codes, CR/LF and surrounding whitespace."""
    if isinstance(line, (bytes, bytearray)):
        line = line.decode("utf-8", errors="replace")
    line = _ANSI_RE.sub("", line)
    return line.strip().strip("\x00")


def header_family(line: str) -> Optional[str]:
    """Return 'classic' / 'c5c6' if ``line`` is an official CSV header line."""
    s = clean_line(line)
    if s.startswith("type,id,mac,") and s.count(",") == len(CLASSIC_FIELDS) - 1:
        return FAMILY_CLASSIC
    if s.startswith("type,seq,mac,") and s.count(",") == len(C5C6_FIELDS) - 1:
        return FAMILY_C5C6
    return None


def _to_int(name: str, value: str, line: str) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        raise ParseError("non_numeric", f"field '{name}' is not an integer: {value!r}", line) from None


def _parse_data(text: str, line: str) -> list:
    s = text.strip()
    if not s.startswith("["):
        raise ParseError("bad_data", "data field does not start with '['", line)
    if not s.endswith("]"):
        raise ParseError("truncated_data", "data array is not terminated with ']'", line)
    body = s[1:-1].strip()
    if not body:
        raise ParseError("empty_data", "data array is empty", line)
    try:
        return [int(v) for v in body.split(",")]
    except ValueError:
        raise ParseError("bad_data", "data array contains non-integer values", line) from None


def decode_complex(raw) -> np.ndarray:
    """Interleaved [imag, real, ...] ints -> complex array (real=raw[2k+1], imag=raw[2k])."""
    arr = np.asarray(raw, dtype=np.float32)
    if arr.size % 2:
        raise ValueError("odd number of CSI values")
    return (arr[1::2] + 1j * arr[0::2]).astype(np.complex64)


def parse_line(line, host_ts: Optional[float] = None) -> Optional[CSIPacket]:
    """Parse one serial line.

    Returns ``None`` for lines that are not CSI data (logs, headers, stats).
    Raises :class:`ParseError` for lines that contain ``CSI_DATA`` but are
    malformed. ESP log prefixes, ANSI codes and CRLF are tolerated.
    """
    s = clean_line(line)
    idx = s.find(CSI_TAG)
    if idx < 0:
        return None
    s = s[idx:]

    try:
        fields = next(csv.reader([s]))
    except csv.Error as e:
        raise ParseError("bad_data", f"csv error: {e}", s) from None
    n = len(fields)
    if n == len(CLASSIC_FIELDS):
        family, names = FAMILY_CLASSIC, CLASSIC_FIELDS
    elif n == len(C5C6_FIELDS):
        family, names = FAMILY_C5C6, C5C6_FIELDS
    else:
        raise ParseError(
            "field_count",
            f"{n} fields (expected {len(CLASSIC_FIELDS)} classic or {len(C5C6_FIELDS)} C5/C6)",
            s,
        )
    if fields[0] != CSI_TAG:
        raise ParseError("bad_data", f"unexpected type field {fields[0]!r}", s)

    rec = dict(zip(names, fields))
    mac = rec["mac"].strip().lower()
    if not _MAC_RE.match(mac):
        raise ParseError("bad_mac", f"invalid MAC {rec['mac']!r}", s)

    ints = {k: _to_int(k, rec[k], s) for k in names if k not in ("type", "mac", "data")}
    raw = _parse_data(rec["data"], s)
    if ints["len"] != len(raw):
        raise ParseError("len_mismatch", f"len field {ints['len']} != {len(raw)} values", s)
    if len(raw) % 2:
        raise ParseError("odd_value_count", f"{len(raw)} values is not a whole number of subcarriers", s)

    csi = decode_complex(raw)
    seq = ints["id"] if family == FAMILY_CLASSIC else ints["seq"]
    pkt = CSIPacket(
        host_ts=host_ts,
        chip_family=family,
        seq=seq,
        mac=mac,
        rssi=ints["rssi"],
        rate=ints["rate"],
        noise_floor=ints["noise_floor"],
        channel=ints["channel"],
        local_timestamp=ints["local_timestamp"],
        sig_len=ints["sig_len"],
        rx_state=ints["rx_state"],
        len=ints["len"],
        first_word_invalid=bool(ints["first_word"]),
        raw=raw,
        csi=csi,
        amplitude=np.abs(csi).astype(np.float32),
        phase=np.angle(csi).astype(np.float32),
        raw_line=s,
    )
    if family == FAMILY_CLASSIC:
        for k in ("sig_mode", "mcs", "bandwidth", "smoothing", "not_sounding", "aggregation", "stbc",
                  "fec_coding", "sgi", "ampdu_cnt", "secondary_channel", "ant"):
            setattr(pkt, k, ints[k])
    else:
        pkt.fft_gain = ints["fft_gain"]
        pkt.agc_gain = ints["agc_gain"]
    return pkt


def _masked_amplitudes(pkt: CSIPacket) -> np.ndarray:
    amp = pkt.amplitude.astype(np.float64)
    if pkt.first_word_invalid and amp.size:
        amp[: min(2, amp.size)] = np.nan
    return amp


def lltf_amplitudes(pkt: CSIPacket) -> np.ndarray:
    """64 LLTF subcarrier amplitudes (order 0..31, -32..-1) for classic chips.

    Subcarriers 0 and 1 are NaN when ``first_word_invalid``. Missing values (a
    packet shorter than 128 values) are NaN. Raises ``ValueError`` for C5/C6
    packets, whose layout does not start with an LLTF block.
    """
    if pkt.chip_family != FAMILY_CLASSIC:
        raise ValueError("LLTF layout is only defined for classic ESP32/S3/C3 packets")
    out = np.full(LLTF_SUBCARRIERS, np.nan)
    amp = _masked_amplitudes(pkt)[:LLTF_SUBCARRIERS]
    out[: amp.size] = amp
    return out


def valid_subcarrier_amplitudes(pkt: CSIPacket) -> np.ndarray:
    """Amplitudes of the subcarriers that carry signal.

    Classic: the 52 legacy LLTF subcarriers (``VALID_LLTF_INDICES``); index 1 is
    NaN when ``first_word_invalid``. C5/C6: all subcarriers (with the first two
    NaN when ``first_word_invalid``); downstream code should drop columns that
    are constantly zero (guard/null subcarriers), since the C5/C6 layout depends
    on the received PPDU format and is not fixed here.
    """
    if pkt.chip_family == FAMILY_CLASSIC:
        return lltf_amplitudes(pkt)[VALID_LLTF_INDICES]
    return _masked_amplitudes(pkt)


def subcarrier_labels(pkt: CSIPacket) -> list:
    """Column labels matching :func:`valid_subcarrier_amplitudes`."""
    if pkt.chip_family == FAMILY_CLASSIC:
        return [f"lltf_{i}" for i in VALID_LLTF_INDICES]
    return [f"sc_{i}" for i in range(pkt.n_subcarriers)]


def parse_stats_line(line, host_ts: Optional[float] = None):
    """Parse ``EG_RX_STATS`` / ``EG_RX_GAIN`` / ``EG_TX_STATS`` lines.

    Returns None for other lines and for malformed stats lines.
    """
    s = clean_line(line)
    for tag, n_fields in (("EG_RX_STATS", 7), ("EG_RX_GAIN", 9), ("EG_TX_STATS", 5)):
        idx = s.find(tag)
        if idx < 0:
            continue
        parts = s[idx:].split(",")
        if len(parts) != n_fields:
            return None
        try:
            if tag == "EG_RX_STATS":
                return RxStats(int(parts[1]), int(parts[2]), float(parts[3]), int(parts[4]),
                               int(parts[5]), int(parts[6]), host_ts)
            if tag == "EG_RX_GAIN":
                v = [int(p) for p in parts[1:]]
                return RxGain(
                    uptime_ms=v[0], agc_gain=v[1], fft_gain=v[2], compensate_gain=v[3] / 1000.0,
                    baseline_agc=None if v[4] == RX_GAIN_UNKNOWN_AGC else v[4],
                    baseline_fft=None if v[5] == RX_GAIN_UNKNOWN_FFT else v[5],
                    compensation_applied=bool(v[6]), force_gain=bool(v[7]), host_ts=host_ts,
                )
            return TxStats(int(parts[1]), int(parts[2]), int(parts[3]), float(parts[4]), host_ts)
        except ValueError:
            return None
    return None


def parse_any(line, host_ts: Optional[float] = None) -> ParsedLine:
    """Classify and parse any serial line without raising."""
    s = clean_line(line)
    if not s:
        return ParsedLine("empty", s)
    if CSI_TAG in s:
        try:
            return ParsedLine("csi", s, packet=parse_line(s, host_ts))
        except ParseError as e:
            return ParsedLine("malformed", s, error=e)
    fam = header_family(s)
    if fam:
        return ParsedLine("header", s, header_family=fam)
    stats = parse_stats_line(s, host_ts)
    if stats is not None:
        return ParsedLine("stats", s, stats=stats)
    return ParsedLine("other", s)
