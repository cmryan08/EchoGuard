"""Thread-safe bounded CSI packet buffer with live link diagnostics."""

from __future__ import annotations

import math
import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Optional

from .csi_parser import CSIPacket, ParsedLine, RxGain, RxStats, TxStats, parse_any

SEQ_MOD = 2 ** 32
#: A forward jump larger than this is treated as a transmitter restart, not loss.
MAX_PLAUSIBLE_GAP = 10_000


@dataclass
class BufferStats:
    total_lines: int = 0
    csi_packets: int = 0
    malformed: int = 0
    malformed_reasons: dict = field(default_factory=dict)
    non_csi_lines: int = 0
    header_family: Optional[str] = None
    seq_gaps: int = 0
    seq_resets: int = 0
    duplicates: int = 0
    packet_rate_hz: Optional[float] = None
    last_rssi: Optional[int] = None
    rssi_mean: Optional[float] = None
    rssi_std: Optional[float] = None
    noise_floor: Optional[int] = None
    last_len: Optional[int] = None
    chip_family: Optional[str] = None
    last_packet_ts: Optional[float] = None
    buffered: int = 0
    last_rx_stats: Optional[RxStats] = None
    last_tx_stats: Optional[TxStats] = None
    last_rx_gain: Optional[RxGain] = None


class CSIBuffer:
    """Bounded ring buffer of :class:`CSIPacket` plus counters.

    Feed raw lines with :meth:`ingest` (parses them) or already parsed packets
    with :meth:`add_packet`. Safe to call from a reader thread while another
    thread reads :meth:`snapshot` / :meth:`packets`.
    """

    def __init__(self, maxlen: int = 3000, rate_window_s: float = 2.0, rssi_window: int = 200) -> None:
        self.maxlen = maxlen
        self.rate_window_s = rate_window_s
        self._lock = threading.Lock()
        self._packets: deque = deque(maxlen=maxlen)
        self._rate_ts: deque = deque()
        self._rssi: deque = deque(maxlen=rssi_window)
        self._reasons: Counter = Counter()
        self._first_packet_ts: Optional[float] = None
        self._last_seq: Optional[int] = None
        self._s = BufferStats()

    # ------------------------------------------------------------------ input
    def ingest(self, host_ts: float, line: str) -> ParsedLine:
        parsed = parse_any(line, host_ts)
        with self._lock:
            self._s.total_lines += 1
            if parsed.kind == "csi":
                self._add_locked(parsed.packet)
            elif parsed.kind == "malformed":
                self._s.malformed += 1
                self._reasons[parsed.error.reason] += 1
            elif parsed.kind != "empty":
                self._s.non_csi_lines += 1
                if parsed.kind == "header":
                    self._s.header_family = parsed.header_family
                elif parsed.kind == "stats":
                    if isinstance(parsed.stats, RxStats):
                        self._s.last_rx_stats = parsed.stats
                    elif isinstance(parsed.stats, RxGain):
                        self._s.last_rx_gain = parsed.stats
                    else:
                        self._s.last_tx_stats = parsed.stats
        return parsed

    def add_packet(self, pkt: CSIPacket) -> None:
        with self._lock:
            self._s.total_lines += 1
            self._add_locked(pkt)

    def _add_locked(self, pkt: CSIPacket) -> None:
        s = self._s
        s.csi_packets += 1
        self._packets.append(pkt)
        ts = pkt.host_ts
        if ts is not None:
            if self._first_packet_ts is None:
                self._first_packet_ts = ts
            s.last_packet_ts = ts
            self._rate_ts.append(ts)
            self._trim_rate(ts)
        self._update_seq(pkt.seq)
        s.last_rssi = pkt.rssi
        self._rssi.append(pkt.rssi)
        s.noise_floor = pkt.noise_floor
        s.last_len = pkt.len
        s.chip_family = pkt.chip_family

    def _update_seq(self, seq: int) -> None:
        last = self._last_seq
        self._last_seq = seq
        if last is None:
            return
        diff = (seq - last) % SEQ_MOD
        if diff == 1:
            return
        if diff == 0:
            self._s.duplicates += 1
        elif diff <= MAX_PLAUSIBLE_GAP:
            self._s.seq_gaps += diff - 1
        else:
            # backwards or huge jump: transmitter restarted, resynchronise
            self._s.seq_resets += 1

    def _trim_rate(self, now: float) -> None:
        cutoff = now - self.rate_window_s
        while self._rate_ts and self._rate_ts[0] <= cutoff:
            self._rate_ts.popleft()

    # ----------------------------------------------------------------- output
    def packet_rate(self, now: Optional[float] = None) -> Optional[float]:
        with self._lock:
            return self._rate_locked(now)

    def _rate_locked(self, now: Optional[float]) -> Optional[float]:
        if self._first_packet_ts is None:
            return None
        if now is None:
            now = self._s.last_packet_ts
        self._trim_rate(now)
        span = min(self.rate_window_s, now - self._first_packet_ts)
        if span <= 0:
            return None
        n = sum(1 for t in self._rate_ts if t <= now)
        return n / span

    def snapshot(self, now: Optional[float] = None) -> BufferStats:
        """Copy of the current counters. ``now`` (host time) lets the rate decay to
        zero when packets stop; default is the last packet timestamp."""
        with self._lock:
            s = self._s
            out = BufferStats(**{k: getattr(s, k) for k in s.__dataclass_fields__})
            out.malformed_reasons = dict(self._reasons)
            out.packet_rate_hz = self._rate_locked(now)
            out.buffered = len(self._packets)
            if self._rssi:
                vals = list(self._rssi)
                mean = sum(vals) / len(vals)
                out.rssi_mean = mean
                out.rssi_std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
            return out

    def packets(self, last_n: Optional[int] = None) -> list:
        with self._lock:
            if last_n is None or last_n >= len(self._packets):
                return list(self._packets)
            return list(self._packets)[-last_n:]

    def latest(self) -> Optional[CSIPacket]:
        with self._lock:
            return self._packets[-1] if self._packets else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._packets)
