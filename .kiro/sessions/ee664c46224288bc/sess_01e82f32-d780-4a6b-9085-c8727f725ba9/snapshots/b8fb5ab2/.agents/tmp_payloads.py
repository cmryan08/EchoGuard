"""Temporary: dump real API payloads for the JS smoke test (deleted after use)."""
import json
from pathlib import Path

from backend.engine import Engine
from backend.sources import ReplaySource

e = Engine()
out = {"empty": {"/api/status": e.snapshot(), "/api/series": e.series(30), "/api/heatmap": e.heatmap(300),
                 "/api/ports": {"ports": [], "error": None}, "/api/replay_files": {"files": []}}}
e.start(ReplaySource("tests/fixtures/classic_esp32s3_sample.txt", speed=0))
e.join(5)
out["replay"] = {"/api/status": e.snapshot(), "/api/series": e.series(30), "/api/heatmap": e.heatmap(300),
                 "/api/ports": {"ports": [], "error": None}, "/api/replay_files": {"files": []}}
Path(__file__).with_name("tmp_payloads.json").write_text(json.dumps(out, allow_nan=False))
