"""Start the EchoGuard dashboard.

    python -m dashboard                         # 127.0.0.1:8050, opens the browser
    python -m dashboard --serial auto           # start reading the receiver immediately
    python -m dashboard --replay tests/fixtures/classic_esp32s3_sample.txt --no-browser

The dashboard has no authentication. It binds to 127.0.0.1 unless --host is
given; binding elsewhere exposes it to everyone on that network.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser
from pathlib import Path

from backend.config import DEFAULT_CONFIG_PATH, AppConfig
from backend.engine import Engine
from backend.sources import ReplaySource, SerialSource

from .server import create_app

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
DISCLAIMER = "Research prototype. Not a medical device. Predictions are not medically validated."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m dashboard", description="EchoGuard local dashboard")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1; no authentication!)")
    p.add_argument("--port", type=int, default=8050, help="HTTP port (default 8050)")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--serial", metavar="auto|PATH", help="start reading this serial port ('auto' to detect)")
    src.add_argument("--replay", metavar="PATH", help="start replaying a recording CSV or raw serial log")
    p.add_argument("--baud", type=int, default=921600, help="serial baud rate (default 921600)")
    p.add_argument("--speed", type=float, default=1.0, help="replay speed factor (0 = as fast as possible)")
    p.add_argument("--loop", action="store_true", help="loop the replay")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="config JSON (created with defaults if missing)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if not (0 < args.port < 65536):
        print("--port must be 1..65535", file=sys.stderr)
        return 2

    try:
        cfg = AppConfig.load_or_create(args.config)
    except (OSError, ValueError) as e:
        print(f"WARNING: could not load config {args.config} ({e}); using defaults.", file=sys.stderr)
        cfg = AppConfig()

    engine = Engine(cfg)
    app = create_app(engine)

    if args.replay:
        path = Path(args.replay)
        if not path.is_file():
            print(f"Replay file not found: {path}", file=sys.stderr)
            return 2
        engine.start(ReplaySource(path, speed=args.speed, loop=args.loop))
        print(f"Replaying {path} (recorded data, not live hardware).")
    elif args.serial:
        engine.start(SerialSource(args.serial, baud=args.baud))
        print(f"Reading serial port '{args.serial}' at {args.baud} baud (read-only).")

    url_host = args.host if args.host not in ("0.0.0.0", "::") else "127.0.0.1"
    if ":" in url_host:
        url_host = f"[{url_host}]"
    url = f"http://{url_host}:{args.port}/"
    print(f"EchoGuard dashboard: {url}")
    print(DISCLAIMER)
    if args.host not in LOCAL_HOSTS:
        print(f"WARNING: binding to {args.host}. The dashboard has NO authentication; anyone who can reach "
              f"this address can view data and change the data source. Use only on a trusted network.",
              file=sys.stderr)
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
