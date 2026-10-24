"""Potential-fall alerts with a countdown and a bounded, persisted event log.

Alert states::

    idle --trigger--> active --countdown expires--> notified --cancel--> cancelled
                        |                                          ^
                        +------------------ cancel ----------------+

The caregiver notification is **simulated only**: it adds an event log entry
and a message for the dashboard. Nothing is sent anywhere and no network call
is made. The countdown uses wall-clock time (it is a human-facing timer).

:class:`EventLog` keeps the newest entries in memory and appends every entry
as one JSON line to ``data/events.jsonl`` (rotated to ``events.jsonl.1`` when
it grows beyond ``max_file_bytes``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import DATA_DIR, AlertConfig
from .engine import json_safe

log = logging.getLogger(__name__)

DEFAULT_EVENTS_PATH = DATA_DIR / "events.jsonl"

ALERT_IDLE = "idle"
ALERT_ACTIVE = "active"
ALERT_NOTIFIED = "notified"
ALERT_CANCELLED = "cancelled"

#: Event kinds written by EchoGuard.
EVENT_KINDS = ("activity", "alert", "notification", "cancel", "calibration", "recording", "connection",
               "detector", "export", "system")

SIMULATED_NOTICE = "simulated only - no message was sent to anyone"


class AlertError(ValueError):
    """Invalid alert request (HTTP 400)."""


def _local(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


class EventLog:
    """Bounded in-memory event list plus append-only JSONL persistence.

    ``path=None`` keeps events in memory only (tests, CLI)."""

    def __init__(self, path=None, maxlen: int = 500, clock=time.time, max_file_bytes: int = 5_000_000,
                 load_existing: bool = True) -> None:
        self.path = Path(path) if path is not None else None
        self.maxlen = max(10, int(maxlen))
        self.clock = clock
        self.max_file_bytes = int(max_file_bytes)
        self._lock = threading.Lock()
        self._events: deque = deque(maxlen=self.maxlen)
        self._next_id = 1
        self.write_errors = 0
        if self.path is not None and load_existing:
            self._load_tail()

    def _load_tail(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict):
                        self._events.append(entry)
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("cannot read %s: %s", self.path, e)
            return
        ids = [e.get("id") for e in self._events if isinstance(e.get("id"), int)]
        self._next_id = (max(ids) + 1) if ids else 1

    def add(self, kind: str, message: str, level: str = "info", /, **data) -> dict:
        ts = float(self.clock())
        with self._lock:
            entry = {
                "id": self._next_id,
                "ts": ts,
                "time": datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds"),
                "kind": str(kind),
                "level": str(level),
                "message": str(message),
                "data": json_safe(data) if data else {},
            }
            self._next_id += 1
            self._events.append(entry)
            if self.path is not None:
                self._append(entry)
        return entry

    def _append(self, entry: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.max_file_bytes > 0 and self.path.exists() and self.path.stat().st_size > self.max_file_bytes:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, allow_nan=False) + "\n")
        except (OSError, ValueError) as e:
            self.write_errors += 1
            log.warning("cannot append to %s: %s", self.path, e)

    def recent(self, n: int = 100, kinds=None) -> list:
        """Newest first."""
        with self._lock:
            items = list(self._events)
        if kinds:
            kinds = set(kinds)
            items = [e for e in items if e.get("kind") in kinds]
        return list(reversed(items))[: max(0, int(n))]

    def __len__(self) -> int:
        return len(self._events)


def simulated_caregiver_notification(alert: dict) -> str:
    """Text of the *simulated* notification. Deliberately does no I/O."""
    when = _local(alert["triggered_at"])
    tag = " [SYNTHETIC DATA]" if alert.get("synthetic") else ""
    return (f"SIMULATED caregiver notification{tag}: potential fall detected at {when}, not cancelled within "
            f"{alert['countdown_s']:g} s ({SIMULATED_NOTICE}).")


class AlertManager:
    """One alert at a time. Thread-safe; :meth:`tick` advances the countdown."""

    def __init__(self, config: Optional[AlertConfig] = None, event_log: Optional[EventLog] = None,
                 clock=time.time, history: int = 20) -> None:
        self.config = config or AlertConfig()
        self.events = event_log if event_log is not None else EventLog(None)
        self.clock = clock
        self._lock = threading.RLock()
        self._alert: Optional[dict] = None
        self._history: deque = deque(maxlen=max(1, history))
        self._seq = 0

    @property
    def state(self) -> str:
        a = self._alert
        return a["state"] if a is not None else ALERT_IDLE

    @property
    def active(self) -> bool:
        """True while an alert is unresolved (counting down or notified)."""
        return self.state in (ALERT_ACTIVE, ALERT_NOTIFIED)

    def trigger(self, event: dict) -> Optional[dict]:
        """Start an alert for a fall event dict (``FallEvent.to_dict()``).
        Returns the alert, or None if one is already unresolved."""
        with self._lock:
            self.tick()
            now = float(self.clock())
            if self.active:
                self.events.add("detector", "Additional potential-fall event while an alert is unresolved",
                                level="warning", event_id=event.get("event_id"))
                return None
            self._seq += 1
            countdown = max(0.0, float(self.config.countdown_s))
            alert = {
                "alert_id": f"A{self._seq:04d}",
                "state": ALERT_ACTIVE,
                "triggered_at": now,
                "countdown_s": countdown,
                "deadline": now + countdown,
                "notified_at": None,
                "notification": None,
                "cancelled_at": None,
                "cancelled_by": None,
                "cancelled_after_notification": False,
                "synthetic": bool(event.get("synthetic")),
                "source_kind": event.get("source_kind"),
                "event": event,
            }
            self._alert = alert
            self._history.appendleft(alert)
            failed = [c["name"] for c in event.get("criteria", []) if not c.get("passed")]
            self.events.add("alert", "POTENTIAL FALL alert: " + (event.get("summary") or "criteria met")
                            + (" [SYNTHETIC DATA]" if alert["synthetic"] else ""), level="alert",
                            alert_id=alert["alert_id"], event_id=event.get("event_id"),
                            criteria=[c.get("name") for c in event.get("criteria", []) if c.get("passed")],
                            failed=failed, synthetic=alert["synthetic"], source_kind=alert["source_kind"])
            if countdown == 0:
                self._notify(now)
            return dict(alert)

    def tick(self) -> None:
        """Send the simulated notification once the countdown has expired."""
        with self._lock:
            a = self._alert
            if a is not None and a["state"] == ALERT_ACTIVE and self.clock() >= a["deadline"]:
                self._notify(float(self.clock()))

    def _notify(self, now: float) -> None:
        a = self._alert
        a["state"] = ALERT_NOTIFIED
        a["notified_at"] = now
        a["notification"] = simulated_caregiver_notification(a)
        self.events.add("notification", a["notification"], level="alert", alert_id=a["alert_id"],
                        simulated=True, synthetic=a["synthetic"])

    def cancel(self, by: str = "user") -> dict:
        with self._lock:
            self.tick()
            a = self._alert
            if a is None or not self.active:
                raise AlertError("no active alert to cancel")
            now = float(self.clock())
            a["cancelled_after_notification"] = a["state"] == ALERT_NOTIFIED
            a["state"] = ALERT_CANCELLED
            a["cancelled_at"] = now
            a["cancelled_by"] = str(by)
            msg = ("Alert dismissed after the simulated notification" if a["cancelled_after_notification"]
                   else f"Alert cancelled with {max(0.0, a['deadline'] - now):.0f} s left (no notification)")
            self.events.add("cancel", f"{msg} ({by})", alert_id=a["alert_id"], by=by)
            return dict(a)

    def status(self) -> dict:
        with self._lock:
            self.tick()
            a = self._alert
            now = float(self.clock())
            cur = None
            if a is not None:
                cur = dict(a)
                if a["state"] == ALERT_ACTIVE:
                    remaining = max(0.0, a["deadline"] - now)
                    cur["remaining_s"] = round(remaining, 1)
                    cur["progress"] = round(1.0 - remaining / a["countdown_s"], 3) if a["countdown_s"] > 0 else 1.0
                else:
                    cur["remaining_s"] = None
                    cur["progress"] = 1.0 if a["state"] == ALERT_NOTIFIED else None
            return json_safe({
                "state": self.state,
                "active": self.active,
                "alert": cur,
                "countdown_s": self.config.countdown_s,
                "history": [{k: h[k] for k in ("alert_id", "state", "triggered_at", "notified_at", "cancelled_at",
                                               "synthetic")} for h in self._history],
                "notice": "Caregiver notification is simulated: nothing is sent.",
            })
