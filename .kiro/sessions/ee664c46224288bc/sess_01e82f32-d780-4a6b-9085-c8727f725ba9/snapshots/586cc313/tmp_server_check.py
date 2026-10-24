import json
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8599"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return r.status, r.read()


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


proc = subprocess.Popen([".venv/bin/python", "-m", "dashboard", "--no-browser", "--port", "8599"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    for _ in range(100):
        try:
            get("/api/status")
            break
        except Exception:
            time.sleep(0.1)
    st, html = get("/")
    print("GET /", st, b"Potential-fall alerts" in html, b"tab-simulation" in html)
    print("POST source", post("/api/source", {"kind": "simulation", "scenario": "potential_fall"})[0])
    t0 = time.time()
    snap = None
    while time.time() - t0 < 30:
        _, body = get("/api/status")
        snap = json.loads(body)
        if snap["alert"]["state"] == "active" and snap["alert"]["alert"]["event"]["criteria"]:
            break
        time.sleep(0.5)
    a = snap["alert"]
    print(f"after {time.time() - t0:.1f}s: alert state={a['state']} remaining={a['alert'] and a['alert']['remaining_s']} "
          f"activity={snap['activity']['key']} synthetic={snap['source']['synthetic']} tx={snap['config']['tx_mac']}")
    for c in a["alert"]["event"]["criteria"]:
        print("   ", c["name"], c["value"], c["comparison"], c["threshold"], "PASS" if c["passed"] else "FAIL")
    print("cancel", post("/api/alerts/cancel", {}))
    _, body = get("/api/status")
    snap = json.loads(body)
    print("after cancel: alert", snap["alert"]["state"], "activity", snap["activity"]["key"])
    _, body = get("/api/events?n=20")
    for e in json.loads(body)["events"][:8]:
        print("  event:", e["kind"], "-", e["message"])
    st, res = post("/api/export", {"what": "buffer"})
    print("export", st, res.get("export", res))
    if st == 200:
        s2, data = get(res["url"])
        print("download", s2, data.splitlines()[0][:120])
    print("detector cfg", get("/api/detector/config")[0], "sim info", get("/api/simulation")[0])
    print("stop", post("/api/stop", {})[0])
finally:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    print("server output:\n" + out[-800:])
    sys.exit(0)
