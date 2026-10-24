"""SYNTHETIC calibration computed from the simulator (demo without hardware).

:func:`synthetic_calibration` plays :data:`simulation.generator.CALIBRATION_SEGMENTS`
(empty room, walking, standing still, sitting down, sitting) through the same
parser, :class:`backend.processing.SignalProcessor` and
:class:`backend.calibration.CalibrationSession` as live data and returns the
computed calibration (thresholds, baseline and the sitting / standing posture
model). The simulated room is fixed (:data:`simulation.generator.ROOM_SEED`),
so the result matches every simulation run.

The result is marked ``synthetic``: it only ever applies to simulation
sources and is never evidence of real-world accuracy.
"""

from __future__ import annotations

from typing import Optional

from backend.calibration import STEP_KEYS, CalibrationError, CalibrationSession
from backend.config import AppConfig
from backend.csi_parser import parse_any
from backend.processing import SignalProcessor

from .generator import CALIBRATION_SEGMENTS, CSISimulator, SimParams, SimulationSource

#: simulator segment key -> calibration step key
SEGMENT_STEPS = {"empty_room": "empty_baseline", "walking": "walking", "standing_still": "standing_still",
                 "sitting": "sitting"}
#: Seconds into a segment before the step starts recording.
LEAD_S = 2.0
DEFAULT_PLACEMENT = "simulated-room"


def synthetic_calibration(params: Optional[SimParams] = None, placement: str = DEFAULT_PLACEMENT,
                          config: Optional[AppConfig] = None) -> dict:
    """Run the calibration steps on the scripted synthetic sequence.

    ``params`` supplies noise / motion strength / packet rate / seed (scenario
    is ignored). Returns the :func:`backend.calibration.compute_calibration`
    result (``synthetic`` True). Raises :class:`CalibrationError` when a step
    fails (e.g. a packet rate below the quality gate).
    """
    cfg = config or AppConfig()
    p = SimParams(**{**(params.__dict__ if params is not None else {}), "scenario": "demo"}).validate()
    if p.seed is None:
        p.seed = 1
    src = SimulationSource(p, realtime=False, t0=1_000_000.0)
    src.sim = CSISimulator(p, segments=CALIBRATION_SEGMENTS)
    src.duration_s = src.sim.cycle_s
    session = CalibrationSession(placement, frame_rate_hz=cfg.processing.frame_rate_hz,
                                 defaults=cfg.thresholds, settle_s=cfg.processing.window_s,
                                 lead_in_s=0, grace_s=0, transition_s=0, auto=False)
    proc = SignalProcessor(cfg.processing)
    seg_start = {}
    t0 = src.t0
    for ts, line in src.lines():
        parsed = parse_any(line, ts)
        if parsed.kind != "csi":
            continue
        pkt = parsed.packet
        i, seg, tl = src.sim.segment_at(ts - t0)
        seg_start.setdefault(i, ts - tl)
        cur = session.current
        if (session.state == "ready" and cur is not None and SEGMENT_STEPS.get(seg.key) == cur.key
                and tl >= LEAD_S):
            session.start_step()
        frame = proc.process(pkt)
        if frame is None:
            continue
        sensor = {"tx_mac": pkt.mac, "channel": pkt.channel, "secondary_channel": pkt.secondary_channel,
                  "chip_family": pkt.chip_family, "csi_len": pkt.len,
                  "n_subcarriers": proc.layout[1] if proc.layout else None}
        profile = proc.last_smoothed
        session.add_frame(frame, None if profile is None else profile.copy(), proc.layout, sensor=sensor,
                          synthetic=True, source_kind="simulation")
        if session.state in ("computed", "failed"):
            break
    src.stop()
    if session.state != "computed" or session.result is None:
        failed = [f"{k}: {session.steps[k].failure}" for k in STEP_KEYS if session.steps[k].failure]
        raise CalibrationError("synthetic calibration failed: "
                               + ("; ".join(failed) or session.error or f"state {session.state}"))
    return session.result
