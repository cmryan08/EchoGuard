"""Temporary verification script (deleted after use): start the dashboard,
query it with curl, stop it."""
import json
import subprocess
import sys
import time

cmd = [".venv/bin/python", "-m", "dashboard", "--no-browser", "--port", "8599",
       "--replay", "tests/fixtures/classic_esp32s3_sample.txt"]
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    base = "http://127.0.0.1:8599"
    status = None
    for _ in range(50):
        r = subprocess.run(["curl", "-s", base + "/api/status"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout:
            status = json.loads(r.stdout)
            if status["counters"]["packets"] > 0:
                break
        time.sleep(0.2)
    print("counters:", status["counters"])
    print("source:", status["source"]["kind"], "finished:", status["source"]["finished"])
    print("rate:", status["packet_rate_hz"], "quality:", status["quality"], "activity:", status["activity"])
    page = subprocess.run(["curl", "-s", base + "/"], capture_output=True, text=True).stdout
    print("index head:", page.splitlines()[:5])
    js = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", base + "/static/app.js"],
                        capture_output=True, text=True).stdout
    print("app.js status:", js)
    bad = subprocess.run(["curl", "-s", "-w", " %{http_code}", "-X", "POST", "-H", "Content-Type: application/json",
                          "-d", '{"kind":"replay","path":"../../etc/passwd"}', base + "/api/source"],
                         capture_output=True, text=True).stdout
    print("traversal:", bad)
    heat = json.loads(subprocess.run(["curl", "-s", base + "/api/heatmap?n=5"], capture_output=True, text=True).stdout)
    print("heatmap rows:", len(heat["rows"]), "subcarriers:", heat["n_subcarriers"])
    series = json.loads(subprocess.run(["curl", "-s", base + "/api/series?seconds=30"], capture_output=True, text=True).stdout)
    print("series points:", len(series["ts"]))
finally:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    print("--- server output ---")
    print(out)
    print("server exit code:", proc.returncode)
sys.exit(0)
