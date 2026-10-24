"""Test-only helpers that build CSI_DATA lines in the official format from
numpy amplitude arrays. These are generated test signals, not hardware data."""

from __future__ import annotations

import numpy as np

from backend.csi_parser import LLTF_SUBCARRIERS, VALID_LLTF_INDICES, parse_line
from backend.sources import ReplaySource

TX_MAC = "1a:00:00:00:00:00"


def classic_line(valid_amps, seq: int = 0, rssi: int = -40, first_word: int = 0, ts_us: int = 0) -> str:
    """Classic 25-field line whose 52 valid LLTF subcarriers have ``valid_amps``
    (real part = amplitude, imag = 0; other subcarriers 0)."""
    amps = np.zeros(LLTF_SUBCARRIERS)
    amps[VALID_LLTF_INDICES] = np.asarray(valid_amps, dtype=float)
    raw = []
    for a in amps:
        raw += [0, int(round(a))]  # [imag, real]
    data = ",".join(str(v) for v in raw)
    return (f"CSI_DATA,{seq},{TX_MAC},{rssi},11,1,0,1,0,1,0,0,0,0,-93,0,11,2,{ts_us},0,67,0,"
            f"{len(raw)},{first_word},\"[{data}]\"")


def c5c6_line(amps, seq: int = 0, rssi: int = -40, ts_us: int = 0) -> str:
    raw = []
    for a in np.asarray(amps, dtype=float):
        raw += [0, int(round(a))]
    data = ",".join(str(v) for v in raw)
    return f"CSI_DATA,{seq},{TX_MAC},{rssi},11,-96,32,4,11,{ts_us},67,0,{len(raw)},0,\"[{data}]\""


def classic_packet(valid_amps, ts: float, seq: int = 0, **kw):
    return parse_line(classic_line(valid_amps, seq=seq, **kw), host_ts=ts)


def c5c6_packet(amps, ts: float, seq: int = 0, **kw):
    return parse_line(c5c6_line(amps, seq=seq, **kw), host_ts=ts)


def base_profile(n: int = 52, level: float = 40.0) -> np.ndarray:
    """Static multipath-like profile (deterministic)."""
    k = np.arange(n)
    return level * (1.0 + 0.3 * np.sin(k / 5.0))


class GatedReplay(ReplaySource):
    """Replay that waits for ``gate`` (threading.Event) before emitting, so a
    test can start a calibration step / take deterministically first."""

    def __init__(self, path, gate, **kw):
        super().__init__(path, **kw)
        self.gate = gate

    def lines(self):
        self.status.connected = True
        self.gate.wait(10)
        yield from super().lines()


def stream(n_packets: int, rate_hz: float = 100.0, t0: float = 1000.0, noise: float = 0.0,
           perturb: float = 0.0, perturb_hz: float = 1.5, seed: int = 0, profile=None):
    """Yield (ts, valid_amps) for a generated stream. ``perturb`` adds a
    subcarrier-dependent time-varying multipath term (relative amplitude)."""
    rng = np.random.default_rng(seed)
    prof = base_profile() if profile is None else np.asarray(profile, dtype=float)
    phases = np.linspace(0, 3 * np.pi, prof.size)
    for i in range(n_packets):
        t = t0 + i / rate_hz
        amps = prof.copy()
        if perturb:
            amps = amps * (1.0 + perturb * np.sin(2 * np.pi * perturb_hz * (t - t0) + phases))
        if noise:
            amps = amps + rng.normal(0.0, noise, prof.size)
        yield t, np.clip(amps, 1.0, None)
