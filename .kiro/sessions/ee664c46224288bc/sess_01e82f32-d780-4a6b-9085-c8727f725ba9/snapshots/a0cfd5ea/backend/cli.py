"""EchoGuard command line tools.

    python -m backend.cli ports
    python -m backend.cli monitor --port auto [--record data/recordings/x.csv]
    python -m backend.cli monitor --replay tests/fixtures/classic_esp32s3_sample.txt --duration 3
    python -m backend.cli monitor --simulate potential_fall --duration 12      # SYNTHETIC data
    python -m backend.cli evaluate --session data/recordings/<session_id>

``monitor`` runs the same parser, signal processing and heuristic fall
detector as the dashboard and prints packet stats, the motion score and any
potential-fall event with its criteria. Nothing here flashes or writes to an
ESP32; ``monitor`` only reads.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .activity import Activity, classify_activity
from .buffer import CSIBuffer
from .config import DEFAULT_CONFIG_PATH, AppConfig
from .csi_parser import valid_subcarrier_amplitudes
from .detector import FallDetector
from .ports import list_candidates
from .recorder import RawRecorder
from .sources import LineSource, ReplaySource, SerialSource

SPARK_CHARS = "▁▂▃▄▅▆▇█"
DISCLAIMER = "Heuristic prototype output. Not a medical device; not medically validated."


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
class _Pipeline:
    """Processing + heuristic fall detection for the CLI (reader thread only)."""

    def __init__(self, config: AppConfig) -> None:
        from .processing import SignalProcessor

        self.config = config
        self.processor = SignalProcessor(config.processing)
        self.detector = FallDetector(config.fall)
        self.last_frame = None
        self.last_activity: Optional[Activity] = None
        self.events: list = []
        self.first_ts: Optional[float] = None

    def feed(self, parsed) -> None:
        if parsed.kind != "csi":
            return
        if self.first_ts is None:
            self.first_ts = parsed.packet.host_ts
        frame = self.processor.process(parsed.packet)
        if frame is None:
            return
        self.last_frame = frame
        self.last_activity = classify_activity(frame, self.config.thresholds)
        ev = self.detector.update(frame, self.config.thresholds)
        if ev is not None:
            self.events.append(ev)


def _reader(source: LineSource, buf: CSIBuffer, recorder: Optional[RawRecorder], errors: list,
            pipeline: Optional[_Pipeline] = None) -> None:
    try:
        for ts, line in source.lines():
            parsed = buf.ingest(ts, line)
            if recorder is not None:
                recorder.write(ts, line)
            if pipeline is not None:
                pipeline.feed(parsed)
    except Exception as e:  # noqa: BLE001 - surface reader crashes to the main thread
        errors.append(e)


def format_event(ev, t0: Optional[float] = None) -> list:
    at = f"+{ev.ts - t0:.1f} s" if t0 is not None else time.strftime("%H:%M:%S", time.localtime(ev.ts))
    tag = " [SYNTHETIC DATA]" if ev.synthetic else ""
    out = [f"*** POTENTIAL FALL{tag} at {at}: {ev.to_dict()['summary']}",
           f"    ({DISCLAIMER})"]
    for c in ev.criteria:
        unit = f" {c.unit}" if c.unit else ""
        out.append(f"    [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.value}{unit} {c.comparison} "
                   f"{c.threshold}{unit} - {c.description}")
    return out


def format_status(source: LineSource, buf: CSIBuffer, elapsed: float, prev_packets: int, now: float,
                  pipeline: Optional[_Pipeline] = None) -> list:
    st = source.status
    live_like = source.kind in ("serial", "simulation")
    snap = buf.snapshot(now=now if live_like else None)
    if source.kind == "serial":
        conn = f"serial {st.port} {'connected' if st.connected else 'DISCONNECTED'}"
        if st.reconnect_count:
            conn += f" (reconnects {st.reconnect_count})"
    elif source.kind == "simulation":
        phase = st.extra.get("label_name") or "-"
        conn = f"SIMULATION {st.port} (synthetic) phase: {phase}"
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
    if pipeline is not None:
        f = pipeline.last_frame
        if f is None:
            lines.append("          motion: no frame yet")
        else:
            score = f"{f.motion_score:.2f}" if f.motion_score is not None else "n/a"
            q = "ok" if f.quality_ok else "INSUFFICIENT (" + "; ".join(f.quality_reasons) + ")"
            act = pipeline.last_activity.label if pipeline.last_activity is not None else "-"
            det = pipeline.detector.status()
            lines.append(f"          motion score {score} | activity: {act} | quality {q} | detector: {det['state_text']}")
    if snap.last_rx_stats is not None:
        r = snap.last_rx_stats
        lines.append(f"          device RX: total {r.csi_rx_total}, {r.csi_rx_rate_hz:.1f} Hz, queue drops "
                     f"{r.queue_drops}, seq gaps {r.seq_gaps}, rssi {r.last_rssi}, uptime {r.uptime_ms / 1000:.0f}s")
    if snap.last_tx_stats is not None:
        t = snap.last_tx_stats
        lines.append(f"          device TX: ok {t.sent_ok}, fail {t.send_fail}, {t.rate_hz:.1f} Hz")
    return lines


def _load_config(path) -> AppConfig:
    """data/config.json if it exists (never created by the CLI), else defaults."""
    p = Path(path)
    if p.is_file():
        try:
            return AppConfig.load(p)
        except (OSError, ValueError) as e:
            print(f"WARNING: cannot load {p} ({e}); using defaults.", file=sys.stderr)
    return AppConfig()


def cmd_monitor(args) -> int:
    cfg = _load_config(args.config)
    if args.simulate:
        from simulation.generator import SIM_MAC, SimParams, SimulationSource

        try:
            params = SimParams(scenario=args.simulate, noise=args.noise, motion_strength=args.motion_strength,
                               event_duration_s=args.event_duration, rate_hz=args.rate, seed=args.seed).validate()
        except ValueError as e:
            print(f"Invalid simulation parameters: {e}", file=sys.stderr)
            return 2
        source: LineSource = SimulationSource(params)
        print(f"SIMULATION '{args.simulate}': SYNTHETIC data (TX MAC {SIM_MAC}), "
              "not evidence of real-world accuracy.")
    elif args.replay:
        source = ReplaySource(args.replay, speed=args.speed, loop=args.loop, line_rate_hz=args.line_rate)
        print(f"Replaying {args.replay} ({source.recording_format}); recorded data, not live hardware.")
    else:
        source = SerialSource(args.port, baud=args.baud)
        print(f"Opening serial port '{args.port}' at {args.baud} baud (read-only). Ctrl-C to stop.")
    th = cfg.thresholds
    print(f"Thresholds: movement {th.movement:g}, rapid {th.rapid:g}, stillness {th.low:g}"
          f" ({'calibrated' if th.calibrated else 'uncalibrated defaults'}); fall heuristic "
          f"{'on' if cfg.fall.enabled else 'off'}. {DISCLAIMER}")
    buf = CSIBuffer(maxlen=args.buffer)
    recorder = RawRecorder(args.record) if args.record else None
    if recorder:
        print(f"Recording raw lines to {recorder.path}")
    pipeline = _Pipeline(cfg)
    errors: list = []
    t = threading.Thread(target=_reader, args=(source, buf, recorder, errors, pipeline), daemon=True)
    start = time.time()
    t.start()
    prev_packets = 0
    shown_events = 0
    rc = 0
    synthetic = bool(getattr(source, "synthetic", False))
    try:
        while True:
            deadline = time.time() + args.interval
            while time.time() < deadline and (t.is_alive() or args.duration):
                time.sleep(0.05)
                if len(pipeline.events) > shown_events:
                    break
            now = time.time()
            elapsed = now - start
            while len(pipeline.events) > shown_events:
                ev = pipeline.events[shown_events]
                ev.synthetic = synthetic
                ev.source_kind = source.kind
                for line in format_event(ev, pipeline.first_ts):
                    print(line)
                shown_events += 1
            for line in format_status(source, buf, elapsed, prev_packets, now, pipeline):
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
          f"{snap.non_csi_lines} other, {snap.seq_gaps} seq gaps, {len(pipeline.events)} potential-fall event(s)"
          + (" [SYNTHETIC DATA]" if synthetic else ""))
    return rc


# ------------------------------------------------------------------ evaluate
def evaluate_take(path, cfg: AppConfig, calibration: Optional[dict] = None) -> dict:
    """Replay one take through processing + a fresh FallDetector."""
    from .offline import OfflineReplay

    rep = OfflineReplay(path, cfg, calibration)
    det = FallDetector(rep.config.fall)
    th = rep.config.thresholds
    packets = frames = good = 0
    max_score = None
    t0 = None
    events, rejections = [], 0
    label = None
    last_rej = None
    for step in rep:
        packets += 1
        if t0 is None:
            t0 = step.packet.host_ts
        label = label or step.label
        f = step.frame
        if f is None:
            continue
        frames += 1
        good += 1 if f.quality_ok else 0
        if f.motion_score is not None:
            max_score = f.motion_score if max_score is None else max(max_score, f.motion_score)
        ev = det.update(f, th)
        if det.last_rejection is not None and det.last_rejection is not last_rej:
            last_rej = det.last_rejection
            rejections += 1
        if ev is not None:
            ev.synthetic = rep.synthetic
            ev.source_kind = "replay"
            events.append(ev)
    return {
        "file": Path(path).name, "label": label, "packets": packets, "frames": frames,
        "quality_ok_fraction": (good / frames) if frames else None, "max_motion_score": max_score,
        "fired": bool(events), "events": events, "rejected_candidates": rejections,
        "last_rejection": last_rej, "t0": t0, "synthetic": rep.synthetic,
        "thresholds": {"movement": th.movement, "rapid": th.rapid, "low": th.low, "calibrated": th.calibrated},
    }


def cmd_evaluate(args) -> int:
    from .recording import LABELS, RecordingError, Session

    cfg = _load_config(args.config)
    calibration = None
    if args.calibration:
        try:
            calibration = json.loads(Path(args.calibration).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"Cannot read calibration {args.calibration}: {e}", file=sys.stderr)
            return 2
    target = Path(args.session)
    if target.is_dir():
        try:
            session = Session.load(target)
        except RecordingError as e:
            print(f"{target}: {e}", file=sys.stderr)
            return 2
        takes = session.take_files()
        synthetic = bool(session.meta.get("synthetic", False))
        title = f"session {session.session_id}"
    elif target.is_file():
        takes, title = [target], f"file {target.name}"
        synthetic = False
    else:
        print(f"Not found: {target}", file=sys.stderr)
        return 2
    if not takes:
        print(f"No takes found in {target}.", file=sys.stderr)
        return 2
    results = [evaluate_take(p, cfg, calibration) for p in takes]
    synthetic = synthetic or any(r["synthetic"] for r in results)
    if args.json:
        out = []
        for r in results:
            r = dict(r)
            r["events"] = [e.to_dict() for e in r["events"]]
            out.append(r)
        print(json.dumps({"target": str(target), "synthetic": synthetic, "takes": out}, indent=2, default=str))
        return 0

    print(f"Heuristic fall-detector check on {title}" + ("  [SYNTHETIC DATA - not accuracy evidence]" if synthetic else ""))
    th = results[0]["thresholds"]
    print(f"Thresholds: rapid/spike {th['rapid']:g}, stillness {th['low']:g} "
          f"({'calibrated' if th['calibrated'] else 'uncalibrated defaults'}); "
          f"stillness {cfg.fall.stillness_duration_s:g} s within {cfg.fall.max_gap_s:g} s after the spike.")
    print(f"{'take':<44} {'label':<16} {'pkts':>6} {'good%':>6} {'max score':>9} {'alert':>6}")
    for r in results:
        good = f"{100 * r['quality_ok_fraction']:.0f}" if r["quality_ok_fraction"] is not None else "-"
        mx = f"{r['max_motion_score']:.2f}" if r["max_motion_score"] is not None else "-"
        print(f"{r['file'][:44]:<44} {str(r['label'] or '-'):<16} {r['packets']:>6} {good:>6} {mx:>9} "
              f"{'YES' if r['fired'] else 'no':>6}")
        for ev in r["events"]:
            for line in format_event(ev, r["t0"]):
                print("    " + line)
        if not r["fired"] and r["last_rejection"]:
            print(f"    last rejected candidate: {r['last_rejection']['reason']}")
    print()
    print("Per label (alerts fired / takes):")
    by_label: dict = {}
    for r in results:
        k = r["label"] or "(unlabeled)"
        n, fired = by_label.get(k, (0, 0))
        by_label[k] = (n + 1, fired + (1 if r["fired"] else 0))
    for k, (n, fired) in by_label.items():
        print(f"  {LABELS.get(k, k):<22} {fired}/{n}")
    print()
    print("This replays recorded examples through the heuristic; it is not an accuracy estimate. "
          + ("Synthetic data never demonstrates real-world performance. " if synthetic else "")
          + DISCLAIMER)
    return 0


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
