"""Synthetic CSI-like data for demonstrations without an ESP32.

**Everything produced here is synthetic.** It is meant to exercise the same
parser, processing, detector and dashboard as live data, never as evidence of
real-world detection accuracy.

:class:`SimulationSource` is a :class:`backend.sources.LineSource` that emits
text lines in the official esp-csi classic ``CSI_DATA`` format (25 fields,
LLTF only, ``len`` 128), so they go through :func:`backend.csi_parser.parse_any`
exactly like serial data. The transmitter MAC is the locally administered
``02:51:4d:00:00:00`` (never the real EchoGuard TX MAC
``1a:00:00:00:00:00``), the source kind is ``simulation`` and
``synthetic = True``, so the Engine, recordings, calibrations and exports are
all marked synthetic.

Signal model (per LLTF subcarrier ``k`` with frequency offset ``f_k``):

* a static multipath profile ``sum_p a_p exp(-j(2 pi f_k tau_p + phi_p))``
  (room reflections, fixed per seed),
* a few person-related scatterers whose excess path length ``L(t)`` follows
  the scenario (walking back and forth, sitting down, rapid movement, a fall
  burst, breathing while still) with amplitude scaled by ``motion_strength``,
  contributing ``b exp(-j 2 pi (f_c + f_k) L(t) / c)``,
* complex Gaussian noise (``noise`` relative to the static RMS amplitude), a
  small per-packet gain jitter (AGC), int8 quantisation, occasional packet
  loss, and an RSSI around -45 dBm.

Scenarios: ``empty_room``, ``walking``, ``sitting``, ``rapid_movement``,
``potential_fall`` (walking, a rapid burst of ``event_duration_s``, then
lying still) and ``demo`` (a scripted sequence of all of them). Each scenario
repeats until the source is stopped (or ``duration_s`` of simulated time).
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Iterator, List, Optional, Tuple

import numpy as np

from backend.csi_parser import CLASSIC_FIELDS, LLTF_SUBCARRIERS, VALID_LLTF_INDICES
from backend.sources import LineSource

#: Locally administered unicast MAC marking simulated packets.
SIM_MAC = "02:51:4d:00:00:00"
SIM_PORT_PREFIX = "simulation:"
CHANNEL = 11
CARRIER_HZ = 2.462e9          # channel 11
SUBCARRIER_SPACING_HZ = 312.5e3
LIGHT_SPEED = 299_792_458.0
TARGET_RMS = 30.0             # static RMS amplitude in int8 units

SCENARIOS = {
    "empty_room": "Empty room",
    "walking": "Walking",
    "sitting": "Sitting down, sitting, standing up",
    "rapid_movement": "Walking with rapid movement bouts",
    "potential_fall": "Walking, rapid fall-like burst, then lying still",
    "demo": "Scripted demo: empty room, walking, sitting, rapid movement, potential fall",
}

#: Parameter ranges (inclusive) and defaults, also used by the dashboard API.
PARAM_RANGES = {
    "noise": (0.0, 0.5, 0.03),
    "motion_strength": (0.0, 3.0, 1.0),
    "event_duration_s": (0.2, 10.0, 1.0),
    "rate_hz": (10.0, 200.0, 100.0),
}

# person scatterers: (base excess path length m, displacement scale, amplitude weight)
_SCATTERERS = ((9.0, 1.0, 1.0), (13.0, 0.8, 0.6), (17.0, 1.25, 0.4))


@dataclass
class SimParams:
    scenario: str = "demo"
    noise: float = 0.03
    motion_strength: float = 1.0
    event_duration_s: float = 1.0
    rate_hz: float = 100.0
    seed: Optional[int] = None
    loss: float = 0.005

    def validate(self) -> "SimParams":
        if self.scenario not in SCENARIOS:
            raise ValueError(f"scenario must be one of: {', '.join(SCENARIOS)}")
        for name, (lo, hi, _) in PARAM_RANGES.items():
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= float(v) <= hi):
                raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
            setattr(self, name, float(v))
        if not (0.0 <= float(self.loss) < 0.5):
            raise ValueError("loss must be between 0 and 0.5")
        return self


@dataclass
class Segment:
    key: str
    label_name: str
    duration_s: float
    motion: str   # empty | still | walk | sit_down | stand_up | rapid | fall | lying


def scenario_segments(scenario: str, event_duration_s: float = 1.0) -> List[Segment]:
    e = float(event_duration_s)
    walk = Segment("walking", "Walking", 6.0, "walk")
    sit = [Segment("standing_still", "Standing still", 3.0, "still"),
           Segment("sitting_down", "Sitting down", 1.5 * e, "sit_down"),
           Segment("sitting", "Sitting", 8.0, "still"),
           Segment("standing_up", "Standing up", 1.5 * e, "stand_up")]
    fall = [Segment("walking", "Walking", 3.0, "walk"),
            Segment("fall_impact", "Fall-like burst", e, "fall"),
            Segment("lying_still", "Lying still after the burst", 30.0, "lying")]
    if scenario == "empty_room":
        return [Segment("empty_room", "Empty room", 60.0, "empty")]
    if scenario == "walking":
        return [walk]
    if scenario == "sitting":
        return sit
    if scenario == "rapid_movement":
        return [Segment("walking", "Walking", 4.0, "walk"),
                Segment("rapid_movement", "Rapid movement", 3.0 * e, "rapid"),
                Segment("walking", "Walking", 4.0, "walk")]
    if scenario == "potential_fall":
        return fall
    if scenario == "demo":
        return ([Segment("empty_room", "Empty room", 8.0, "empty"), Segment("walking", "Walking", 8.0, "walk")]
                + sit[1:] + [Segment("walking", "Walking", 4.0, "walk"),
                             Segment("rapid_movement", "Rapid movement", 3.0 * e, "rapid"),
                             Segment("walking", "Walking", 4.0, "walk")]
                + fall[1:2] + [Segment("lying_still", "Lying still after the burst", 25.0, "lying")])
    raise ValueError(f"unknown scenario {scenario!r}")


# ------------------------------------------------------------- motion model
def _person_amp(motion: str) -> float:
    return {"empty": 0.0, "still": 0.12, "lying": 0.12, "walk": 0.18, "sit_down": 0.16, "stand_up": 0.16,
            "rapid": 0.5, "fall": 1.0}[motion]


def _displacement(motion: str, t: float, T: float, phase: float) -> float:
    """Excess path-length change (m) at segment-local time ``t`` of a segment of length ``T``."""
    breathing = 0.004 * math.sin(2 * math.pi * 0.25 * t + phase) + 0.01 * math.sin(2 * math.pi * 0.07 * t)
    if motion in ("empty", "still", "lying"):
        return breathing
    if motion == "walk":
        return 0.35 * math.sin(2 * math.pi * 0.25 * t + phase)
    if motion in ("sit_down", "stand_up"):
        x = min(1.0, max(0.0, t / T)) if T > 0 else 1.0
        sign = 1.0 if motion == "sit_down" else -1.0
        return sign * 0.3 * (0.5 - 0.5 * math.cos(math.pi * x))
    if motion == "rapid":
        return 0.25 * math.sin(2 * math.pi * 0.6 * t + phase) + 0.05 * math.sin(2 * math.pi * 1.7 * t)
    if motion == "fall":
        x = min(1.0, max(0.0, t / T)) if T > 0 else 1.0
        return 0.6 * x * x + 0.04 * math.sin(2 * math.pi * 2.5 * t + phase)
    raise ValueError(motion)


class CSISimulator:
    """Deterministic (per seed) generator of complex LLTF CSI vectors."""

    def __init__(self, params: SimParams) -> None:
        self.p = params.validate()
        self.rng = np.random.default_rng(params.seed)
        idx = np.arange(LLTF_SUBCARRIERS)
        k = np.where(idx < 32, idx, idx - 64)               # LLTF order 0..31, -32..-1
        self.f_k = k * SUBCARRIER_SPACING_HZ
        self.valid = np.zeros(LLTF_SUBCARRIERS, dtype=bool)
        self.valid[VALID_LLTF_INDICES] = True
        # static multipath (room)
        delays = np.array([0.0, 12e-9, 30e-9, 55e-9, 85e-9, 120e-9])
        amps = np.array([1.0, 0.65, 0.45, 0.3, 0.2, 0.12]) * self.rng.uniform(0.8, 1.2, delays.size)
        phis = self.rng.uniform(0, 2 * np.pi, delays.size)
        H = (amps[:, None] * np.exp(-1j * (2 * np.pi * self.f_k[None, :] * delays[:, None] + phis[:, None]))).sum(0)
        rms = float(np.sqrt(np.mean(np.abs(H[self.valid]) ** 2)))
        self.scale = TARGET_RMS / rms
        self.H_static = H * self.scale
        self.scat_phase = self.rng.uniform(0, 2 * np.pi, len(_SCATTERERS))
        self.segments = scenario_segments(self.p.scenario, self.p.event_duration_s)
        self.cycle_s = sum(s.duration_s for s in self.segments)
        self._offsets = np.zeros(len(_SCATTERERS))
        self._seg_index: Optional[int] = None
        self._last_disp = np.zeros(len(_SCATTERERS))

    def segment_at(self, t: float) -> Tuple[int, Segment, float]:
        tc = t % self.cycle_s if self.cycle_s > 0 else 0.0
        acc = 0.0
        for i, s in enumerate(self.segments):
            if tc < acc + s.duration_s or i == len(self.segments) - 1:
                return i, s, tc - acc
            acc += s.duration_s
        raise AssertionError("unreachable")

    def csi(self, t: float) -> Tuple[np.ndarray, Segment, float]:
        """Complex CSI (64 LLTF values, nulls zero) at simulated time ``t`` (s),
        the active segment and a gain factor (for RSSI)."""
        i, seg, tl = self.segment_at(t)
        disp = np.array([_displacement(seg.motion, tl, seg.duration_s, ph) * scale
                         for ph, (_, scale, _) in zip(self.scat_phase, _SCATTERERS)])
        if i != self._seg_index:
            # keep the path lengths continuous across segment boundaries
            if self._seg_index is not None:
                self._offsets = self._offsets + self._last_disp - disp
            self._seg_index = i
        self._last_disp = disp
        b_total = _person_amp(seg.motion) * self.p.motion_strength * TARGET_RMS
        H = self.H_static.copy()
        if b_total > 0:
            for j, (L0, _, w) in enumerate(_SCATTERERS):
                L = L0 + self._offsets[j] + disp[j]
                H = H + b_total * w * np.exp(-1j * 2 * np.pi * (CARRIER_HZ + self.f_k) * L / LIGHT_SPEED)
        sigma = self.p.noise * TARGET_RMS / math.sqrt(2)
        if sigma > 0:
            H = H + self.rng.normal(0, sigma, H.size) + 1j * self.rng.normal(0, sigma, H.size)
        gain = float(np.clip(1.0 + self.rng.normal(0, 0.02), 0.9, 1.1))
        H = np.where(self.valid, H * gain, 0)
        return H, seg, gain


def csi_line(H: np.ndarray, seq: int, rssi: int, local_us: int, noise_floor: int = -93) -> str:
    """Official classic 25-field CSI_DATA line (``[imag, real]`` int8 pairs)."""
    re = np.clip(np.round(H.real), -127, 127).astype(int)
    im = np.clip(np.round(H.imag), -127, 127).astype(int)
    raw = np.empty(2 * H.size, dtype=int)
    raw[0::2], raw[1::2] = im, re
    data = ",".join(map(str, raw.tolist()))
    # type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,
    # sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,len,first_word,data
    return (f"CSI_DATA,{seq},{SIM_MAC},{rssi},11,1,0,1,0,1,0,0,0,0,{noise_floor},0,{CHANNEL},2,{local_us},0,"
            f"67,0,{raw.size},0,\"[{data}]\"")


HEADER_LINE = ",".join(CLASSIC_FIELDS)


class SimulationSource(LineSource):
    """Synthetic LineSource (kind ``simulation``, ``synthetic = True``).

    ``realtime=True`` paces packets at ``rate_hz`` of wall time; ``False``
    emits as fast as possible with simulated timestamps. ``duration_s`` stops
    after that much simulated time (None = until :meth:`stop`). The current
    segment is published as ``status.extra['label']`` / ``'label_name'``
    (shown as the simulated scenario phase, never as real ground truth)."""

    kind = "simulation"
    synthetic = True

    def __init__(self, params: Optional[SimParams] = None, realtime: bool = True,
                 duration_s: Optional[float] = None, t0: Optional[float] = None, **kw) -> None:
        super().__init__()
        self.params = (params or SimParams(**kw)).validate()
        self.realtime = realtime
        self.duration_s = duration_s
        self.t0 = t0
        self.sim = CSISimulator(self.params)
        self.status.port = SIM_PORT_PREFIX + self.params.scenario
        self.status.extra.update({"synthetic": True, "scenario": self.params.scenario,
                                  "params": asdict(self.params), "label": None, "label_name": None,
                                  "segment": None, "cycle_s": round(self.sim.cycle_s, 2)})

    def lines(self) -> Iterator[Tuple[float, str]]:
        p = self.params
        st = self.status
        rng = np.random.default_rng(None if p.seed is None else p.seed + 1)
        start_wall = time.time()
        t0 = self.t0 if self.t0 is not None else start_wall
        period = 1.0 / p.rate_hz
        st.connected = True
        st.connected_since = start_wall
        try:
            yield t0, HEADER_LINE
            st.lines_read += 1
            i = 0
            while not self._stop.is_set():
                t = i * period
                if self.duration_s is not None and t > self.duration_s:
                    break
                if self.realtime:
                    delay = start_wall + t - time.time()
                    if delay > 0 and self._stop.wait(delay):
                        break
                H, seg, gain = self.sim.csi(t)
                i += 1
                if seg.key != st.extra.get("label"):
                    st.extra.update({"label": seg.key, "label_name": seg.label_name + " [simulated]",
                                     "segment": seg.motion})
                if p.loss > 0 and rng.random() < p.loss:
                    continue          # lost packet (sequence gap)
                jitter = float(rng.normal(0, 0.0005))
                ts = time.time() if self.realtime else t0 + t + jitter
                motion = _person_amp(seg.motion) * p.motion_strength
                rssi = int(round(-45 + 20 * math.log10(gain) - 2.0 * motion + rng.normal(0, 0.7)))
                line = csi_line(H, seq=i, rssi=rssi, local_us=int((t + jitter) * 1e6) + 1_000_000)
                st.lines_read += 1
                st.bytes_read += len(line) + 1
                yield ts, line
        finally:
            st.connected = False
            st.finished = True
