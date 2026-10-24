"""Temporary verification wrapper: launch the dashboard, exercise M3 routes, stop it."""
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path("/Users/christopherryan/Projects/Hackathon")
BASE = "http://127.0.0.1:8599"
cfg_existed = (ROOT / "data" / "config.json").exists()


def call(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


proc = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "dashboard", "--no-browser", "--port", "8599",
                         "--replay", "tests/fixtures/classic_esp32s3_sample.txt", "--loop"],
                        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
sid = None
try:
    for _ in range(50):
        try:
            call("/api/status")
            break
        except Exception:
            time.sleep(0.2)
    code, st = call("/api/status")
    print("status", code, "packets", st["counters"]["packets"], "kind", st["source"]["kind"])
    code, r = call("/api/session/create", {"placement": "verify-test", "notes": "server verification"})
    print("session/create", code, r["session"]["session_id"])
    sid = r["session"]["session_id"]
    code, r = call("/api/take/start", {"label": "walking"})
    print("take/start", code, r.get("take"))
    time.sleep(1.5)
    code, r = call("/api/take/stop", {})
    print("take/stop", code, {k: r["take"][k] for k in ("file", "packets", "lines", "duration_s", "stop_reason")})
    code, r = call("/api/take/start", {"label": "dancing"})
    print("take/start invalid label", code, r["error"])
    code, r = call("/api/session/select", {"session_id": "../../etc"})
    print("session/select traversal", code, r["error"])
    sdir = ROOT / "data" / "recordings" / sid
    print("session dir files:", sorted(p.name for p in sdir.iterdir()))
    meta = json.loads((sdir / "session.json").read_text())
    print("session.json takes:", [(t["label"], t["packets"]) for t in meta["takes"]])
    take_csv = sdir / meta["takes"][0]["file"]
    print("take csv header:", take_csv.read_text().splitlines()[0])
    code, r = call("/api/calibration/start", {"placement": "verify-test", "durations": {"empty_baseline": 15}})
    print("calibration/start", code, r["state"]["session"]["state"], r["state"]["session"]["current_step"])
    code, r = call("/api/calibration/step/start", {})
    print("calibration/step/start", code, r["state"]["session"]["state"])
    time.sleep(1.0)
    code, r = call("/api/calibration/state")
    s0 = r["session"]["steps"][0]
    print("calibration/state", code, s0["status"], "frames", s0["frames"], "quality", s0["last_quality"])
    code, r = call("/api/calibration/step/cancel", {})
    print("calibration/step/cancel", code, r["state"]["session"]["state"])
    code, r = call("/api/calibration/save", {})
    print("calibration/save (not computed)", code, r["error"])
    code, r = call("/api/calibrations")
    print("calibrations", code, r)
    code, r = call("/api/calibration/load", {"key": "../../etc/passwd"})
    print("calibration/load traversal", code, r["error"])
    code, r = call("/api/calibration/cancel", {})
    print("calibration/cancel", code)
    code, r = call("/api/source", {"kind": "replay", "path": f"data/recordings/{sid}", "speed": 0})
    print("replay session", code)
    time.sleep(1.0)
    code, st = call("/api/status")
    print("ground_truth", st["ground_truth"], "packets", st["counters"]["packets"], "format", st["source"]["extra"].get("format"))
    with urllib.request.urlopen(BASE + "/", timeout=5) as r:
        html = r.read().decode()
    print("index has Calibration/Record tabs:", 'id="tab-calibration"' in html and 'id="tab-record"' in html)
finally:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    print("--- server output ---")
    print(out.strip())
    if sid:
        shutil.rmtree(ROOT / "data" / "recordings" / sid, ignore_errors=True)
    if not cfg_existed:
        (ROOT / "data" / "config.json").unlink(missing_ok=True)
    sys.exit(0)
