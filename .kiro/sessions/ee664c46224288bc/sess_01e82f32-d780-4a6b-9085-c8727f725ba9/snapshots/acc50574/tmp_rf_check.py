import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.ml import train_rf  # noqa: E402
from backend.recording import SessionStore, TakeWriter  # noqa: E402
from simulation import SimParams, SimulationSource  # noqa: E402

import importlib  # noqa: E402
for mod in ("pandas", "flask", "serial", "pytest"):
    print(mod, "importable:", importlib.util.find_spec(mod) is not None)


def make(root, placement, seed, now):
    s = SessionStore(root).create(placement, synthetic=True, source_kind="simulation", now=now)
    for i, (label, scen) in enumerate((("empty_room", "empty_room"), ("walking", "walking"),
                                       ("simulated_fall", "potential_fall")), start=1):
        fname = f"{i:03d}_{label}_x.csv"
        w = TakeWriter(s.dir / fname, s.session_id, f"t{i:03d}", label)
        for ts, line in SimulationSource(SimParams(scenario=scen, seed=seed + i), realtime=False, duration_s=10,
                                         t0=1000.0 * i).lines():
            w.write(ts, line)
        w.close()
        s.meta["takes"].append({"take_id": f"t{i:03d}", "index": i, "label": label, "file": fname})
        s.save()


with tempfile.TemporaryDirectory() as d:
    rec = Path(d) / "rec"
    make(rec, "sim-a", 0, 1_700_000_000)
    make(rec, "sim-b", 10, 1_700_000_100)
    make(rec, "sim-c", 20, 1_700_000_200)
    print("--- without flag (expect refusal, rc 2)")
    print("rc =", train_rf.main(["--recordings", str(rec), "--out-dir", str(Path(d) / "models")]))
    print("--- with --allow-synthetic")
    rc = train_rf.main(["--recordings", str(rec), "--out-dir", str(Path(d) / "models"), "--trees", "50",
                        "--allow-synthetic"])
    print("rc =", rc, "models:", sorted(p.name for p in (Path(d) / "models").iterdir()))
    loaded = __import__("joblib").load(next((Path(d) / "models").glob("*.joblib")))
    print("loaded model:", type(loaded["model"]).__name__, "synthetic:", loaded["meta"]["synthetic"])
