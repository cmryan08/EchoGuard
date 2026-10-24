"""Flask app serving the EchoGuard dashboard and its JSON API.

The UI is a self-contained single page in ``dashboard/static`` (vanilla JS,
canvas charts, no CDN). The server has **no authentication**: it binds to
127.0.0.1 by default and must not be exposed on a shared network.

Routes::

    GET  /                       dashboard page
    GET  /api/status             Engine.snapshot()
    GET  /api/series?seconds=30  motion feature time series
    GET  /api/heatmap?n=300      normalised subcarrier amplitude rows
    GET  /api/ports              serial port candidates (read-only listing)
    GET  /api/replay_files       recordings and fixtures that can be replayed
    POST /api/source             {"kind": "serial", "port": "auto"|"/dev/...", "baud": 921600}
                                 {"kind": "replay", "path": "...", "speed": 1.0, "loop": false}
    POST /api/stop               stop the current source

Calibration (backend.calibration)::

    GET  /api/calibration/state          wizard state, active calibration
    POST /api/calibration/start          {"placement": "living-room", "durations": {"empty_baseline": 20, ...},
                                          "lead_in_s": 5, "grace_s": 5, "auto": true}
    POST /api/calibration/step/start     start a run (lead-in, then the current step; all remaining with auto)
    POST /api/calibration/step/redo      {"step": "walking"} (default: current step)
    POST /api/calibration/step/cancel    abort the running step
    POST /api/calibration/cancel         abandon the calibration
    POST /api/calibration/save           persist to data/calibration/<sensor_key>.json and apply
    GET  /api/calibrations               stored calibrations
    POST /api/calibration/load           {"key": "..."}
    POST /api/calibration/delete         {"key": "...", "confirm": true}
    POST /api/calibration/clear          back to the adaptive reference / default thresholds

Labeled recording (backend.recording)::

    GET  /api/recording                  current session, running take, can_record
    GET  /api/sessions                   all sessions
    POST /api/session/create             {"placement": "...", "notes": "..."}
    POST /api/session/select             {"session_id": "..."}
    POST /api/take/start                 {"label": "walking", "duration": 10 | null}
    POST /api/take/stop
    POST /api/take/delete                {"take_id": "t001", "confirm": true}

Simulation, fall detection, alerts, events and export::

    GET  /api/simulation                 scenarios and parameter ranges
    POST /api/source                     {"kind": "simulation", "scenario": "potential_fall", "noise": 0.03,
                                          "motion_strength": 1.0, "event_duration_s": 1.0, "rate_hz": 100}
    GET  /api/alerts                     alert state (countdown, criteria) + detector state
    POST /api/alerts/cancel              cancel / dismiss the current alert
    GET  /api/detector/config            fall heuristic + alert settings
    POST /api/detector/config            update them (validated; saved to data/config.json)
    GET  /api/events?n=100               event log (newest first)
    POST /api/export                     {"what": "buffer"} | {"what": "session", "path": "<session dir or take>"}
    GET  /api/exports                    files in data/exports
    GET  /api/exports/<name>             download one export (CSV)
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Optional

from flask import Flask, jsonify, request, send_from_directory

from backend.alerts import AlertError, EventLog
from backend.calibration import CalibrationError, CalibrationManager, CalibrationStore
from backend.detector import effective_thresholds
from backend.engine import Engine
from backend.export import ExportError, export_buffer, export_session, list_exports, validate_export_name
from backend.monitor import FallMonitor
from backend.ports import list_candidates
from backend.recording import MAX_TAKE_DURATION_S, RecordingError, RecordingManager, Session, SessionStore
from backend.sources import ReplaySource, SerialSource
from simulation.generator import PARAM_RANGES, SCENARIOS, SimParams, SimulationSource, scenario_segments

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
REPLAY_EXTENSIONS = (".csv", ".txt", ".log")
FIXTURE_EXTENSIONS = (".csv", ".txt")
#: Serial device paths: /dev/ followed by safe path segments (no '..').
PORT_RE = re.compile(r"^/dev/[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*$")
BAUD_MIN, BAUD_MAX = 9600, 3_000_000
SPEED_MAX = 50.0
LINE_RATE_MIN, LINE_RATE_MAX = 1.0, 2000.0
SERIES_MAX_S = 120.0
HEATMAP_MAX_N = 600
EVENTS_MAX_N = 500

#: Editable fall-heuristic settings: name -> (section, lo, hi). 0 for the two
#: score thresholds means "derive from the motion thresholds".
DETECTOR_FIELDS = {
    "spike_threshold": ("fall", 0.0, 1000.0),
    "rise_window_s": ("fall", 0.2, 10.0),
    "min_rise_fraction": ("fall", 0.0, 1.0),
    "stillness_threshold": ("fall", 0.0, 1000.0),
    "stillness_duration_s": ("fall", 0.5, 120.0),
    "max_gap_s": ("fall", 0.1, 30.0),
    "min_packet_rate_hz": ("fall", 1.0, 1000.0),
    "refractory_s": ("fall", 0.0, 3600.0),
    "countdown_s": ("alerts", 0.0, 600.0),
}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


class BadRequest(ValueError):
    """Input validation failure, reported as HTTP 400 with a JSON error."""


def _err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _num(value, name: str, lo: float, hi: float, default=None, integer: bool = False):
    if value is None:
        if default is None:
            raise BadRequest(f"'{name}' is required")
        return default
    if isinstance(value, bool):
        raise BadRequest(f"'{name}' must be a number")
    try:
        num = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise BadRequest(f"'{name}' must be {'an integer' if integer else 'a number'}") from None
    if integer and isinstance(value, float) and not value.is_integer():
        raise BadRequest(f"'{name}' must be an integer")
    if not (lo <= num <= hi) or num != num:
        raise BadRequest(f"'{name}' must be between {lo:g} and {hi:g}")
    return num


def validate_port(port) -> str:
    if not isinstance(port, str) or not port:
        raise BadRequest("'port' must be 'auto' or a /dev/ device path")
    if port == "auto":
        return port
    if not PORT_RE.match(port) or ".." in port.split("/"):
        raise BadRequest("'port' must be 'auto' or a /dev/ device path such as /dev/cu.usbmodem1101")
    return port


def resolve_replay_path(path, root: Path, allowed_dirs: Iterable[Path]) -> Path:
    """Resolve ``path`` (relative to ``root`` or absolute) and require it to be
    an existing replayable file inside one of ``allowed_dirs``."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise BadRequest("'path' must be a non-empty string")
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    try:
        resolved = p.resolve(strict=False)
    except (OSError, RuntimeError):
        raise BadRequest("invalid path") from None
    if not any(resolved.is_relative_to(d.resolve()) for d in allowed_dirs):
        raise BadRequest("replay files must be inside data/ or tests/fixtures/")
    if resolved.is_dir():
        if not (resolved / "session.json").is_file():
            raise BadRequest("a replay directory must be a recording session (with session.json)")
        return resolved
    if resolved.suffix.lower() not in REPLAY_EXTENSIONS:
        raise BadRequest(f"replay files must be one of {', '.join(REPLAY_EXTENSIONS)}")
    if not resolved.is_file():
        raise BadRequest("replay file not found")
    return resolved


def list_replay_files(root: Path, recordings_dir: Optional[Path] = None) -> list:
    """Replayable inputs: recording sessions (whole session), their takes,
    other recordings under the recordings dir, and the format fixtures."""
    out = []
    rec_dir = Path(recordings_dir) if recordings_dir is not None else root / "data" / "recordings"
    if rec_dir.is_dir():
        for p in sorted(rec_dir.iterdir(), reverse=True):
            if p.is_dir() and (p / "session.json").is_file():
                try:
                    s = Session.load(p)
                except RecordingError:
                    continue
                entry = _file_entry(p, root, "sessions")
                entry.update({"kind": "session", "session_id": s.session_id, "takes": len(s.meta.get("takes", [])),
                              "synthetic": bool(s.meta.get("synthetic", False))})
                out.append(entry)
                for t in sorted(s.meta.get("takes", []), key=lambda t: t.get("index", 0)):
                    f = p / str(t.get("file") or "")
                    if f.is_file() and f.parent == p:
                        e = _file_entry(f, root, "takes")
                        e.update({"kind": "take", "session_id": s.session_id, "take_id": t.get("take_id"),
                                  "label": t.get("label"), "label_name": t.get("label_name"),
                                  "synthetic": bool(s.meta.get("synthetic", False))})
                        out.append(e)
        for p in sorted(rec_dir.rglob("*")):
            if (p.is_file() and p.suffix.lower() in REPLAY_EXTENSIONS
                    and not (p.parent / "session.json").is_file()):
                out.append(_file_entry(p, root, "recordings"))
    fix_dir = root / "tests" / "fixtures"
    if fix_dir.is_dir():
        for p in sorted(fix_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in FIXTURE_EXTENSIONS:
                out.append(_file_entry(p, root, "fixtures"))
    return out


def _file_entry(p: Path, root: Path, group: str) -> dict:
    try:
        size = p.stat().st_size if p.is_file() else None
    except OSError:
        size = None
    try:
        rel = p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = p.as_posix()
    return {"path": rel, "name": p.name, "group": group, "size": size, "kind": "file"}


def create_app(engine: Engine, root: Optional[Path] = None, replay_dirs: Optional[list] = None,
               data_dir: Optional[Path] = None, config_path: Optional[Path] = None) -> Flask:
    """Flask app factory. ``root`` / ``replay_dirs`` / ``data_dir`` are overridable
    for tests (``data_dir`` holds ``calibration/``, ``recordings/``,
    ``exports/`` and ``events.jsonl``). ``config_path`` (optional) is where
    detector settings changed in the UI are saved."""
    root = Path(root or PROJECT_ROOT).resolve()
    data_dir = Path(data_dir).resolve() if data_dir is not None else root / "data"
    allowed = [Path(d).resolve() for d in (replay_dirs or [data_dir, root / "tests" / "fixtures"])]
    recordings_dir = data_dir / "recordings"
    export_dir = data_dir / "exports"
    calibration = CalibrationManager(engine, CalibrationStore(data_dir / "calibration"))
    recording = RecordingManager(engine, SessionStore(recordings_dir))
    events = EventLog(data_dir / "events.jsonl", maxlen=engine.config.alerts.event_log_max)
    monitor = FallMonitor(engine, event_log=events)
    recording.on_event = events.add

    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["ENGINE"] = engine
    app.config["CALIBRATION"] = calibration
    app.config["RECORDING"] = recording
    app.config["MONITOR"] = monitor
    app.config["EVENTS"] = events
    app.json.sort_keys = False

    @app.after_request
    def _headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(BadRequest)
    def _bad_request(e):
        return _err(str(e), 400)

    @app.errorhandler(CalibrationError)
    def _calibration_error(e):
        return _err(str(e), 400)

    @app.errorhandler(RecordingError)
    def _recording_error(e):
        return _err(str(e), 400)

    @app.errorhandler(AlertError)
    def _alert_error(e):
        return _err(str(e), 400)

    @app.errorhandler(ExportError)
    def _export_error(e):
        return _err(str(e), 400)

    @app.errorhandler(404)
    def _not_found(e):
        if request.path.startswith("/api/"):
            return _err("not found", 404)
        return e

    @app.errorhandler(405)
    def _not_allowed(e):
        if request.path.startswith("/api/"):
            return _err("method not allowed", 405)
        return e

    @app.errorhandler(500)
    def _server_error(e):
        log.exception("internal error")
        return _err("internal server error", 500)

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/status")
    def status():
        return jsonify(engine.snapshot())

    @app.get("/api/series")
    def series():
        seconds = _num(request.args.get("seconds"), "seconds", 1, SERIES_MAX_S, default=30.0)
        return jsonify(engine.series(seconds))

    @app.get("/api/heatmap")
    def heatmap():
        n = _num(request.args.get("n"), "n", 1, HEATMAP_MAX_N, default=300, integer=True)
        return jsonify(engine.heatmap(n))

    @app.get("/api/ports")
    def ports():
        try:
            cands = list_candidates()
        except Exception as e:  # noqa: BLE001
            return jsonify({"ports": [], "error": f"could not list serial ports: {e}"})
        return jsonify({"ports": [{
            "device": c.device, "description": c.description, "vid_pid": c.vid_pid,
            "likely_esp32": c.likely_esp32, "excluded": c.excluded, "bridge": c.bridge, "reason": c.reason,
        } for c in cands], "error": None})

    @app.get("/api/replay_files")
    def replay_files():
        return jsonify({"files": list_replay_files(root, recordings_dir)})

    @app.post("/api/source")
    def set_source():
        body = _json_body()
        kind = body.get("kind")
        if kind == "serial":
            port = validate_port(body.get("port", "auto"))
            baud = _num(body.get("baud"), "baud", BAUD_MIN, BAUD_MAX, default=921600, integer=True)
            source = SerialSource(port, baud=baud)
        elif kind == "replay":
            path = resolve_replay_path(body.get("path"), root, allowed)
            speed = _num(body.get("speed"), "speed", 0.0, SPEED_MAX, default=1.0)
            line_rate = _num(body.get("line_rate_hz"), "line_rate_hz", LINE_RATE_MIN, LINE_RATE_MAX, default=100.0)
            loop = body.get("loop", False)
            if not isinstance(loop, bool):
                raise BadRequest("'loop' must be true or false")
            try:
                source = ReplaySource(path, speed=speed, loop=loop, line_rate_hz=line_rate)
            except OSError as e:
                raise BadRequest(f"cannot open replay file: {e.strerror or e}") from None
            except ValueError as e:
                raise BadRequest(f"cannot open replay: {e}") from None
        elif kind == "simulation":
            source = _simulation_source(body)
        else:
            raise BadRequest("'kind' must be 'serial', 'replay' or 'simulation'")
        engine.start(source)
        return jsonify({"ok": True, "status": engine.snapshot()})

    @app.post("/api/stop")
    def stop():
        engine.stop()
        return jsonify({"ok": True, "status": engine.snapshot()})

    # ------------------------------------------------------- calibration
    @app.get("/api/calibration/state")
    def calibration_state():
        return jsonify(calibration.state())

    @app.post("/api/calibration/start")
    def calibration_start():
        body = _json_body()
        durations = body.get("durations") or {}
        if not isinstance(durations, dict):
            raise BadRequest("'durations' must be an object of step -> seconds")
        return jsonify({"ok": True, "state": calibration.start(
            body.get("placement"), durations, lead_in_s=body.get("lead_in_s", 0.0),
            grace_s=body.get("grace_s", 0.0), auto=body.get("auto", False))})

    @app.post("/api/calibration/step/start")
    def calibration_step_start():
        _json_body()
        return jsonify({"ok": True, "state": calibration.step_start()})

    @app.post("/api/calibration/step/redo")
    def calibration_step_redo():
        step = _json_body().get("step")
        if step is not None and not isinstance(step, str):
            raise BadRequest("'step' must be a step key")
        return jsonify({"ok": True, "state": calibration.step_redo(step)})

    @app.post("/api/calibration/step/cancel")
    def calibration_step_cancel():
        _json_body()
        return jsonify({"ok": True, "state": calibration.step_cancel()})

    @app.post("/api/calibration/cancel")
    def calibration_cancel():
        _json_body()
        return jsonify({"ok": True, "state": calibration.cancel()})

    @app.post("/api/calibration/save")
    def calibration_save():
        _json_body()
        out = calibration.save()
        events.add("calibration", f"Calibration saved: data/calibration/{out['path']}", key=out["key"],
                   synthetic=bool(out["calibration"].get("synthetic")))
        return jsonify({"ok": True, **out})

    @app.get("/api/calibrations")
    def calibrations():
        return jsonify({"calibrations": calibration.store.list(), "active": engine.calibration_info})

    @app.post("/api/calibration/load")
    def calibration_load():
        return jsonify({"ok": True, **calibration.load(_json_body().get("key"))})

    @app.post("/api/calibration/delete")
    def calibration_delete():
        body = _json_body()
        _require_confirm(body)
        out = calibration.delete(body.get("key"))
        events.add("calibration", f"Calibration deleted: {out['key']}", key=out["key"])
        return jsonify({"ok": True, **out})

    @app.post("/api/calibration/clear")
    def calibration_clear():
        _json_body()
        return jsonify({"ok": True, "state": calibration.clear()})

    # --------------------------------------------------------- recording
    @app.get("/api/recording")
    def recording_status():
        return jsonify(recording.status())

    @app.get("/api/sessions")
    def sessions():
        return jsonify({"sessions": recording.list_sessions(), "current": recording.status()["session"]})

    @app.post("/api/session/create")
    def session_create():
        body = _json_body()
        notes = body.get("notes", "")
        if notes is None:
            notes = ""
        return jsonify({"ok": True, "session": recording.create_session(body.get("placement"), notes)})

    @app.post("/api/session/select")
    def session_select():
        return jsonify({"ok": True, "session": recording.select_session(_json_body().get("session_id"))})

    @app.post("/api/take/start")
    def take_start():
        body = _json_body()
        duration = body.get("duration")
        if duration is not None:
            duration = _num(duration, "duration", 0, MAX_TAKE_DURATION_S)
            if duration == 0:
                duration = None
        return jsonify({"ok": True, "take": recording.start_take(body.get("label"), duration),
                        "status": recording.status()})

    @app.post("/api/take/stop")
    def take_stop():
        _json_body()
        return jsonify({"ok": True, "take": recording.stop_take(), "status": recording.status()})

    @app.post("/api/take/delete")
    def take_delete():
        body = _json_body()
        _require_confirm(body)
        return jsonify({"ok": True, "session": recording.delete_take(body.get("take_id")),
                        "status": recording.status()})

    # --------------------------------------------------- simulation info
    @app.get("/api/simulation")
    def simulation_info():
        return jsonify({
            "scenarios": [{"key": k, "name": v,
                           "segments": [s.key for s in scenario_segments(k)]} for k, v in SCENARIOS.items()],
            "params": {k: {"min": lo, "max": hi, "default": d} for k, (lo, hi, d) in PARAM_RANGES.items()},
            "notice": "Synthetic data. Never evidence of real-world detection accuracy.",
        })

    # ------------------------------------------------- alerts / detector
    @app.get("/api/alerts")
    def alerts_status():
        return jsonify({"alert": monitor.alert_status(), "detector": monitor.detector_status(),
                        "fall_events": list(reversed(monitor.fall_events[-10:]))})

    @app.post("/api/alerts/cancel")
    def alerts_cancel():
        _json_body()
        monitor.cancel_alert("user (dashboard)")
        return jsonify({"ok": True, "alert": monitor.alert_status()})

    @app.get("/api/detector/config")
    def detector_config():
        return jsonify(_detector_settings(engine))

    @app.post("/api/detector/config")
    def detector_config_set():
        body = _json_body()
        updates = {}
        for name, value in body.items():
            if name == "enabled":
                if not isinstance(value, bool):
                    raise BadRequest("'enabled' must be true or false")
                updates[name] = ("fall", value)
                continue
            if name not in DETECTOR_FIELDS:
                raise BadRequest(f"unknown setting '{name}'")
            section, lo, hi = DETECTOR_FIELDS[name]
            updates[name] = (section, float(_num(value, name, lo, hi)))
        fall = copy.deepcopy(engine.config.fall)
        for name, (section, value) in updates.items():
            if section == "fall":
                setattr(fall, name, value)
        if 0 < fall.spike_threshold <= fall.stillness_threshold:
            raise BadRequest("spike_threshold must be greater than stillness_threshold")
        for name, (section, value) in updates.items():
            setattr(engine.config.fall if section == "fall" else engine.config.alerts, name, value)
        saved = None
        if config_path is not None:
            cfg = copy.deepcopy(engine.config)
            cfg.thresholds = engine.base_thresholds   # never persist calibrated thresholds as defaults
            try:
                saved = str(cfg.save(config_path))
            except OSError as e:
                log.warning("cannot save %s: %s", config_path, e)
        if updates:
            events.add("detector", "Fall heuristic settings changed: "
                       + ", ".join(f"{k}={v[1]}" for k, v in updates.items()), settings={k: v[1] for k, v in updates.items()})
        return jsonify({"ok": True, "saved_to": saved, **_detector_settings(engine)})

    @app.get("/api/events")
    def events_list():
        n = _num(request.args.get("n"), "n", 1, EVENTS_MAX_N, default=100, integer=True)
        return jsonify({"events": events.recent(n), "persisted_to": events.path.name if events.path else None})

    # ------------------------------------------------------------ export
    @app.post("/api/export")
    def export_create():
        body = _json_body()
        what = body.get("what")
        if what == "buffer":
            result = export_buffer(engine, export_dir)
        elif what == "session":
            path = resolve_replay_path(body.get("path"), root, [recordings_dir])
            if not (path.is_dir() or (path.parent / "session.json").is_file()):
                raise BadRequest("export a recording session directory or one of its takes")
            try:
                result = export_session(path, engine.config, export_dir)
            except (OSError, RecordingError) as e:
                raise BadRequest(f"cannot export: {e}") from None
        else:
            raise BadRequest("'what' must be 'buffer' or 'session'")
        events.add("export", f"Exported {result['rows']} packets to data/exports/{result['name']}"
                   + (" [SYNTHETIC]" if result["synthetic"] else ""), name=result["name"], rows=result["rows"])
        result.pop("path", None)
        return jsonify({"ok": True, "export": result, "url": f"/api/exports/{result['name']}"})

    @app.get("/api/exports")
    def exports_list():
        return jsonify({"exports": list_exports(export_dir)})

    @app.get("/api/exports/<name>")
    def exports_get(name):
        try:
            validate_export_name(name)
        except ExportError:
            return _err("invalid export file name", 400)
        if not (export_dir / name).is_file():
            return _err("not found", 404)
        return send_from_directory(export_dir, name, as_attachment=True, mimetype="text/csv")

    return app


def _detector_settings(engine: Engine) -> dict:
    return {"fall": asdict(engine.config.fall), "alerts": asdict(engine.config.alerts),
            "effective": effective_thresholds(engine.config.fall, engine.config.thresholds),
            "ranges": {k: {"min": lo, "max": hi} for k, (_, lo, hi) in DETECTOR_FIELDS.items()}}


def _simulation_source(body: dict) -> SimulationSource:
    scenario = body.get("scenario", "demo")
    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        raise BadRequest(f"'scenario' must be one of: {', '.join(SCENARIOS)}")
    vals = {name: _num(body.get(name), name, lo, hi, default=d) for name, (lo, hi, d) in PARAM_RANGES.items()}
    seed = body.get("seed")
    if seed is not None:
        seed = _num(seed, "seed", 0, 2 ** 31 - 1, integer=True)
    try:
        return SimulationSource(SimParams(scenario=scenario, seed=seed, **vals))
    except ValueError as e:
        raise BadRequest(str(e)) from None


def _json_body() -> dict:
    if not request.is_json:
        raise BadRequest("expected a JSON body (Content-Type: application/json)")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise BadRequest("expected a JSON object")
    return body


def _require_confirm(body: dict) -> None:
    if body.get("confirm") is not True:
        raise BadRequest("deletion requires \"confirm\": true")
