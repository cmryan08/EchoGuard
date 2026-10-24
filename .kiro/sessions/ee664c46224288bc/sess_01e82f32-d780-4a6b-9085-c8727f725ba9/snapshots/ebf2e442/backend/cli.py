"""EchoGuard command line tools.

    python -m backend.cli ports
    python -m backend.cli monitor --port auto [--record data/recordings/x.csv]
    python -m backend.cli monitor --replay tests/fixtures/classic_esp32s3_sample.txt --duration 3

Nothing here flashes or writes to an ESP32. ``monitor`` only reads.
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
import threading
import time
from typing import Optional

import numpy as np

from .buffer import CSIBuffer
from .csi_parser import valid_subcarrier_amplitudes
from .ports import list_candidates
from .recorder import RawRecorder
from .sources import LineSource, ReplaySource, SerialSource

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values) -> str:
    """Compact per-frame normalised bar chart; NaN (masked) values show as '·'."""
    vals = np.asarray(values, dtype=float)
    finite = vals[np.isfinite(vals)]
    if finite.size == 0:
        return "·" * len(vals)
    lo, hi = float(finite.min()), float(finite.max())
    span = hi - lo
    out = []
    for v in vals:
        if not math.isfinite(v):
            out.append("·")
        elif span <= 0:
            out.append(SPARK_CHARS[0])
        else:
            out.append(SPARK_CHARS[min(len(SPARK_CHARS) - 1, int((v - lo) / span * len(SPARK_CHARS)))])
    return "".join(out)


# --------------------------------------------------------------------- ports
def cmd_ports(args) -> int:
    try:
        cands = list_candidates()
    except Exception as e:  # noqa: BLE001
        print(f"Could not list serial ports: {e}", file=sys.stderr)
        return 1
    esptool = shutil.which("esptool.py") or shutil.which("esptool") or "esptool.py"
    print("Serial ports:")
    if not cands:
        print("  (none)")
    for c in cands:
        tag = "LIKELY ESP32" if c.likely_esp32 else ("excluded" if c.excluded else "other")
        print(f"  {c.device:<40} {tag:<12} vid:pid={c.vid_pid:<9} {c.description or '-'}  [{c.reason}]")
    likely = [c for c in cands if c.likely_esp32 and not c.excluded]
    print()
    if not likely:
        print("No likely ESP32 is connected.")
        print("  - Use a data-capable USB cable (many cables are charge-only).")
        print("  - Native-USB boards (XIAO ESP32-S3) appear as /dev/cu.usbmodem*;")
        print("    USB-UART bridges as /dev/cu.usbserial-*, /dev/cu.SLAB_USBtoUART or /dev/cu.wchusbserial*.")
        print("  - Then re-run: make ports")
        return 0
    print(f"{len(likely)} likely ESP32 port(s). Identify each chip with the READ-ONLY command:")
    for c in likely:
        print(f"  {esptool} --port {c.device} chip_id")
    print()
    print("WARNING: do not connect the third ESP32 that holds firmware you want to keep.")
    print("If it is plugged in, unplug it (or skip its port) before flashing anything.")
    print("chip_id only reads the chip type and MAC (it resets the board but writes nothing).")
    return 0


# ------------------------------------------------------------------- monitor
def _reader(source: LineSource, buf: CSIBuffer, recorder: Optional[RawRecorder], errors: list) -> None:
    try:
        for ts, line in source.lines():
            buf.ingest(ts, line)
            if recorder is not None:
                recorder.write(ts, line)
    except Exception as e:  # noqa: BLE001 - surface reader crashes to the main thread
        errors.append(e)


def format_status(source: LineSource, buf: CSIBuffer, elapsed: float, prev_packets: int, now: float) -> list:
    st = source.status
    snap = buf.snapshot(now=now if source.kind == "serial" else None)
    if source.kind == "serial":
        conn = f"serial {st.port} {'connected' if st.connected else 'DISCONNECTED'}"
        if st.reconnect_count:
            conn += f" (reconnects {st.reconnect_count})"
    else:
        conn = f"replay {st.port} {'finished' if st.finished else 'playing'}"
    lines = [f"[{elapsed:6.1f}s] {conn} | lines {snap.total_lines} | malformed {snap.malformed}"
             + (f" {snap.malformed_reasons}" if snap.malformed_reasons else "")]
    if not st.connected and st.last_error and source.kind == "serial":
        lines.append(f"          last error: {st.last_error}")
    if snap.csi_packets == 0:
        lines.append("          waiting for CSI packets")
    else:
        pkt = buf.latest()
        amps = valid_subcarrier_amplitudes(pkt)
        mean_amp = float(np.nanmean(amps)) if np.isfinite(amps).any() else float("nan")
        rate = f"{snap.packet_rate_hz:.1f} Hz" if snap.packet_rate_hz is not None else "n/a"
        rssi_sd = f" sd {snap.rssi_std:.1f}" if snap.rssi_std is not None else ""
        lines.append(
            f"          CSI pkts {snap.csi_packets} (+{snap.csi_packets - prev_packets}) | rate {rate}"
            f" | seq gaps {snap.seq_gaps} resets {snap.seq_resets} | RSSI {snap.last_rssi} dBm"
            f" (mean {snap.rssi_mean:.1f}{rssi_sd}) | NF {snap.noise_floor} | len {snap.last_len}"
            f" {snap.chip_family} | mean amp {mean_amp:.1f}"
        )
        lines.append(f"          amp [{sparkline(amps)}] ({len(amps)} subcarriers, seq {pkt.seq})")
    if snap.last_rx_stats is not None:
        r = snap.last_rx_stats
        lines.append(f"          device RX: total {r.csi_rx_total}, {r.csi_rx_rate_hz:.1f} Hz, queue drops "
                     f"{r.queue_drops}, seq gaps {r.seq_gaps}, rssi {r.last_rssi}, uptime {r.uptime_ms / 1000:.0f}s")
    if snap.last_tx_stats is not None:
        t = snap.last_tx_stats
        lines.append(f"          device TX: ok {t.sent_ok}, fail {t.send_fail}, {t.rate_hz:.1f} Hz")
    return lines


def cmd_monitor(args) -> int:
    if args.replay:
        source: LineSource = ReplaySource(args.replay, speed=args.speed, loop=args.loop, line_rate_hz=args.line_rate)
        print(f"Replaying {args.replay} ({source.recording_format}); recorded data, not live hardware.")
    else:
        source = SerialSource(args.port, baud=args.baud)
        print(f"Opening serial port '{args.port}' at {args.baud} baud (read-only). Ctrl-C to stop.")
    buf = CSIBuffer(maxlen=args.buffer)
    recorder = RawRecorder(args.record) if args.record else None
    if recorder:
        print(f"Recording raw lines to {recorder.path}")
    errors: list = []
    t = threading.Thread(target=_reader, args=(source, buf, recorder, errors), daemon=True)
    start = time.time()
    t.start()
    prev_packets = 0
    rc = 0
    try:
        while True:
            deadline = time.time() + args.interval
            while time.time() < deadline and (t.is_alive() or args.duration):
                time.sleep(0.05)
            now = time.time()
            elapsed = now - start
            for line in format_status(source, buf, elapsed, prev_packets, now):
                print(line)
            sys.stdout.flush()
            prev_packets = buf.snapshot().csi_packets
            if errors:
                print(f"Reader error: {errors[0]!r}", file=sys.stderr)
                rc = 1
                break
            if args.duration and elapsed >= args.duration:
                break
            if not t.is_alive() and not args.duration:
                break  # replay finished
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        source.stop()
        t.join(timeout=2.0)
        if recorder:
            recorder.close()
            print(f"Saved {recorder.rows_written} lines to {recorder.path}")
    snap = buf.snapshot()
    print(f"Summary: {snap.total_lines} lines, {snap.csi_packets} CSI packets, {snap.malformed} malformed, "
          f"{snap.non_csi_lines} other, {snap.seq_gaps} seq gaps")
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m backend.cli", description="EchoGuard CSI tools (read-only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ports", help="list serial ports and likely ESP32 boards")
    sp.set_defaults(func=cmd_ports)

    mp = sub.add_parser("monitor", help="print live CSI packet counts, rates and amplitudes")
    mp.add_argument("--port", default="auto", help="serial device path or 'auto' (default)")
    mp.add_argument("--baud", type=int, default=921600)
    mp.add_argument("--record", help="write raw lines to this CSV recording")
    mp.add_argument("--replay", help="replay a recording CSV or raw serial log instead of a serial port")
    mp.add_argument("--speed", type=float, default=1.0, help="replay speed factor (0 = as fast as possible)")
    mp.add_argument("--loop", action="store_true", help="loop the replay")
    mp.add_argument("--line-rate", type=float, default=100.0, help="lines/s for raw log replay (default 100)")
    mp.add_argument("--duration", type=float, default=0.0, help="stop after N seconds (0 = until Ctrl-C / end of replay)")
    mp.add_argument("--interval", type=float, default=1.0, help="status print interval in seconds")
    mp.add_argument("--buffer", type=int, default=3000, help="in-memory packet buffer size")
    mp.set_defaults(func=cmd_monitor)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
