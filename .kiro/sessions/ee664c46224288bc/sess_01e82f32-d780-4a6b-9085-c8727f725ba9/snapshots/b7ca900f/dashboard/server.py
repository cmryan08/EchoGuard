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
    POST /api/calibration/start          {"placement": "living-room", "durations": {"empty_baseline": 20, ...}}
    POST /api/calibration/step/start     record the current step
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
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, Optional

from flask import Flask, jsonify, request, send_from_directory

from backend.calibration import CalibrationError, CalibrationManager, CalibrationStore
from backend.engine import Engine
from backend.ports import list_candidates
from backend.recording import MAX_TAKE_DURATION_S, RecordingError, RecordingManager, Session, SessionStore
from backend.sources import ReplaySource, SerialSource

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
               data_dir: Optional[Path] = None) -> Flask:
    """Flask app factory. ``root`` / ``replay_dirs`` / ``data_dir`` are overridable
    for tests (``data_dir`` holds ``calibration/`` and ``recordings/``)."""
    root = Path(root or PROJECT_ROOT).resolve()
    data_dir = Path(data_dir).resolve() if data_dir is not None else root / "data"
    allowed = [Path(d).resolve() for d in (replay_dirs or [data_dir, root / "tests" / "fixtures"])]
    recordings_dir = data_dir / "recordings"
    calibration = CalibrationManager(engine, CalibrationStore(data_dir / "calibration"))
    recording = RecordingManager(engine, SessionStore(recordings_dir))

    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["ENGINE"] = engine
    app.config["CALIBRATION"] = calibration
    app.config["RECORDING"] = recording
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
            raise BadRequest("simulation mode is not available yet (planned for Milestone 5)")
        else:
            raise BadRequest("'kind' must be 'serial' or 'replay'")
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
        return jsonify({"ok": True, "state": calibration.start(body.get("placement"), durations)})

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
        return jsonify({"ok": True, **calibration.save()})

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
        return jsonify({"ok": True, **calibration.delete(body.get("key"))})

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

    return app


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
