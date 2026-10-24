"""Wires the fall detector, the alert manager and the event log to an Engine.

:class:`FallMonitor` registers an engine frame hook (engine thread) that

* resets the detector when the source changes,
* feeds every frame to :class:`backend.detector.FallDetector` with the active
  (calibrated or default) motion thresholds,
* starts an alert for each :class:`backend.detector.FallEvent` and keeps the
  engine's displayed activity at ``potential_fall`` while the alert is
  unresolved,
* logs debounced activity changes, calibration changes and connection events.

It also adds ``alert`` and ``detector`` sections to ``Engine.snapshot()``.
The same class is used by the dashboard and the CLI so live, replayed and
simulated data share one pipeline.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .activity import Activity
from .alerts import AlertManager, EventLog
from .detector import FallDetector

log = logging.getLogger(__name__)


class FallMonitor:
    def __init__(self, engine, event_log: Optional[EventLog] = None, alerts: Optional[AlertManager] = None,
                 detector: Optional[FallDetector] = None, clock=time.time) -> None:
        self.engine = engine
        cfg = engine.config
        self.events = event_log if event_log is not None else EventLog(None, maxlen=cfg.alerts.event_log_max)
        self.alerts = alerts if alerts is not None else AlertManager(cfg.alerts, self.events, clock=clock)
        self.detector = detector if detector is not None else FallDetector(cfg.fall)
        self.clock = clock
        self._lock = threading.RLock()
        self._marker = None
        self._conn = None
        self._stable: Optional[str] = None
        self._pending: Optional[str] = None
        self._pending_since: Optional[float] = None
        self._calib_key = None
        self.fall_events: list = []   # newest last, bounded
        engine.add_frame_hook(self._on_frame)
        engine.add_status_provider("alert", self.alert_status)
        engine.add_status_provider("detector", self.detector_status)

    # ----------------------------------------------------- engine thread
    def _on_frame(self, frame, activity) -> None:
        eng = self.engine
        with self._lock:
            self._check_source()
            if eng.started_at != self._marker:
                self._marker = eng.started_at
                self.detector.reset()
                self._stable = self._pending = None
            ev = self.detector.update(frame, eng.config.thresholds)
            if ev is not None:
                ev.source_kind = eng.source_kind
                ev.synthetic = bool(eng.synthetic)
                d = ev.to_dict()
                self.fall_events.append(d)
                del self.fall_events[:-50]
                self.alerts.trigger(d)
            self.alerts.tick()
            self._sync_override()
            if activity is not None:
                self._track_activity(frame.ts, activity)
            self._check_calibration()

    def _track_activity(self, ts: float, activity) -> None:
        key = activity.value if isinstance(activity, Activity) else str(activity)
        if key == self._stable:
            self._pending = None
            return
        if key != self._pending:
            self._pending, self._pending_since = key, ts
            return
        if ts - self._pending_since + 1e-6 >= self.engine.config.alerts.activity_min_s:
            prev, self._stable, self._pending = self._stable, key, None
            label = Activity(key).label if key in Activity._value2member_map_ else key
            self.events.add("activity", f"Activity: {label}", activity=key, previous=prev,
                            synthetic=bool(self.engine.synthetic))

    def _check_calibration(self) -> None:
        info = self.engine.calibration_info
        key = info.get("key") if info else None
        if key != self._calib_key:
            prev, self._calib_key = self._calib_key, key
            if key:
                how = "auto-loaded" if info.get("auto_loaded") else "applied"
                self.events.add("calibration", f"Calibration {how}: {info.get('name') or key}", key=key,
                                synthetic=bool(info.get("synthetic")))
            elif prev:
                self.events.add("calibration", "Calibration cleared: using the adaptive reference", key=prev)

    def _check_source(self) -> None:
        eng = self.engine
        st = eng.source_status
        if st is None:
            return
        cur = (eng.started_at, eng.source_kind, bool(st.connected), bool(st.finished), int(st.reconnect_count))
        prev, self._conn = self._conn, cur
        name = {"live": "serial", "replay": "replay", "simulation": "simulation"}.get(eng.source_kind, eng.source_kind)
        where = st.port or ""
        syn = bool(eng.synthetic)
        if prev is None or prev[0] != cur[0]:
            self.events.add("connection", f"Source started: {name} {where}" + (" [SYNTHETIC]" if syn else ""),
                            source_kind=eng.source_kind, port=where, synthetic=syn)
            prev = (cur[0], cur[1], False, False, cur[4])
        if cur[3] and not prev[3]:
            self.events.add("connection", f"Source ended: {name} {where}", source_kind=eng.source_kind, port=where)
            return
        if cur[4] > prev[4]:
            self.events.add("connection", f"Serial reconnected ({cur[4]} reconnects): {where}", port=where)
        elif cur[2] and not prev[2]:
            self.events.add("connection", f"Connected: {name} {where}", source_kind=eng.source_kind, port=where)
        if prev[2] and not cur[2] and not cur[3]:
            self.events.add("connection", f"Disconnected: {name} {where}"
                            + (f" ({st.last_error})" if st.last_error else ""), level="warning",
                            kind=eng.source_kind, port=where, error=st.last_error or None)

    def _sync_override(self) -> None:
        want = Activity.POTENTIAL_FALL if self.alerts.active else None
        if self.engine.activity_override != want:
            self.engine.set_activity_override(want)

    # ------------------------------------------------------------ requests
    def poll(self) -> None:
        """Advance the alert countdown and log connection changes (called by
        status requests and the CLI loop, since no frames arrive while
        disconnected)."""
        with self._lock:
            self._check_source()
            self.alerts.tick()
            self._sync_override()

    def cancel_alert(self, by: str = "user") -> dict:
        with self._lock:
            out = self.alerts.cancel(by)
            self._sync_override()
            return out

    def alert_status(self) -> dict:
        self.poll()
        return self.alerts.status()

    def detector_status(self) -> dict:
        with self._lock:
            return self.detector.status()
