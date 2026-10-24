import json
import sys

from backend.engine import Engine
from dashboard.server import create_app
from simulation import SimParams, SimulationSource
import tempfile

d = tempfile.mkdtemp()
eng = Engine()
app = create_app(eng, data_dir=d)
c = app.test_client()
eng.start(SimulationSource(SimParams(scenario="potential_fall", seed=7), realtime=False, duration_s=12))
eng.join(60)
out = {}
for p in ("/api/status", "/api/series", "/api/heatmap", "/api/calibration/state", "/api/recording", "/api/events",
          "/api/detector/config", "/api/exports", "/api/replay_files", "/api/ports", "/api/calibrations",
          "/api/sessions"):
    out[p] = c.get(p).get_json()
json.dump(out, open(sys.argv[1], "w"))
print("alert state", out["/api/status"]["alert"]["state"])
