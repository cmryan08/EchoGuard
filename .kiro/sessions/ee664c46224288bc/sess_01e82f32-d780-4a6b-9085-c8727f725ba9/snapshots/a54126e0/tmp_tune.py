import sys
from collections import defaultdict

import numpy as np

from backend.activity import classify_activity
from backend.buffer import CSIBuffer
from backend.config import AppConfig
from backend.detector import FallDetector
from backend.processing import SignalProcessor
from simulation import SimParams, SimulationSource

scen = sys.argv[1] if len(sys.argv) > 1 else "demo"
dur = float(sys.argv[2]) if len(sys.argv) > 2 else 60
kw = dict(a.split("=") for a in sys.argv[3:])
kw = {k: float(v) for k, v in kw.items()}
cfg = AppConfig()
src = SimulationSource(SimParams(scenario=scen, seed=1, **kw), realtime=False, duration_s=dur, t0=1000.0)
buf = CSIBuffer()
proc = SignalProcessor(cfg.processing)
det = FallDetector(cfg.fall)
per = defaultdict(list)
acts = defaultdict(lambda: defaultdict(int))
for ts, line in src.lines():
    p = buf.ingest(ts, line)
    if p.kind != "csi":
        continue
    f = proc.process(p.packet)
    if f is None:
        continue
    seg = src.status.extra["label"]
    if f.motion_score is not None:
        per[seg].append(f.motion_score)
        per[seg + "_var"].append(f.variance / cfg.processing.uncalibrated_variance_ref)
        per[seg + "_dif"].append(f.diff_energy / cfg.processing.uncalibrated_diff_ref)
    acts[seg][classify_activity(f, cfg.thresholds).value] += 1
    ev = det.update(f, cfg.thresholds)
    if ev:
        print(f"EVENT at {ev.ts - 1000:.1f}s seg={seg} peak={ev.peak_score:.2f} {ev.to_dict()['summary']}")
    if det.last_rejection and det.last_rejection["ts"] == f.ts:
        print(f"  reject {f.ts - 1000:.1f}s seg={seg}: {det.last_rejection['reason']}")
for k, v in per.items():
    v = np.array(v)
    print(f"{k:16s} n={v.size:4d} p05={np.percentile(v, 5):6.2f} med={np.median(v):6.2f} p95={np.percentile(v, 95):6.2f} max={v.max():6.2f} acts={dict(acts[k])}")
print(buf.snapshot().seq_gaps, buf.snapshot().malformed)
