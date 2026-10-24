"""Serial port discovery and ESP32 candidate classification (read-only).

Nothing here writes to a device. ``probe_port`` only opens a port and reads.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

#: USB vendor IDs commonly found on ESP32 development boards.
KNOWN_VIDS = {
    0x303A: "Espressif native USB (USB-Serial-JTAG / USB-OTG)",
    0x10C4: "Silicon Labs CP210x USB-UART",
    0x1A86: "WCH CH34x USB-UART",
    0x0403: "FTDI USB-UART",
}
#: macOS system pseudo ports that are never an ESP32.
EXCLUDED_NAME_PARTS = ("Bluetooth-Incoming-Port", "debug-console")
#: Device-name fragments of USB serial adapters when VID/PID is unavailable.
LIKELY_NAME_PARTS = ("usbmodem", "usbserial", "SLAB_USBtoUART", "wchusbserial", "ttyUSB", "ttyACM")


class PortSelectionError(RuntimeError):
    """Raised when ``--port auto`` cannot pick exactly one ESP32 port."""


@dataclass
class PortCandidate:
    device: str
    description: str = ""
    manufacturer: str = ""
    serial_number: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None
    likely_esp32: bool = False
    bridge: str = ""
    excluded: bool = False
    reason: str = ""

    @property
    def vid_pid(self) -> str:
        if self.vid is None:
            return "-"
        return f"{self.vid:04X}:{(self.pid or 0):04X}"


def classify_port(info) -> PortCandidate:
    """Classify a ``serial.tools.list_ports_common.ListPortInfo``-like object."""
    device = getattr(info, "device", "") or ""
    cand = PortCandidate(
        device=device,
        description=getattr(info, "description", "") or "",
        manufacturer=getattr(info, "manufacturer", "") or "",
        serial_number=getattr(info, "serial_number", "") or "",
        vid=getattr(info, "vid", None),
        pid=getattr(info, "pid", None),
    )
    if any(part in device for part in EXCLUDED_NAME_PARTS):
        cand.excluded = True
        cand.reason = "macOS system port (not an ESP32)"
        return cand
    if cand.vid in KNOWN_VIDS:
        cand.likely_esp32 = True
        cand.bridge = KNOWN_VIDS[cand.vid]
        cand.reason = f"USB VID {cand.vid:04X} ({cand.bridge})"
        return cand
    if cand.vid is None and any(part in device for part in LIKELY_NAME_PARTS):
        cand.likely_esp32 = True
        cand.bridge = "unknown USB serial"
        cand.reason = "USB serial device name (no VID/PID reported)"
        return cand
    cand.reason = "unknown USB VID" if cand.vid is not None else "not a USB serial device"
    return cand


def _default_list_fn():
    from serial.tools import list_ports

    return list_ports.comports()


def list_candidates(list_fn: Optional[Callable[[], Iterable]] = None) -> list:
    """List all serial ports, classified. On macOS ``/dev/tty.*`` twins of
    ``/dev/cu.*`` devices are dropped (``cu`` is the right one for reading)."""
    infos = list((list_fn or _default_list_fn)())
    cands = [classify_port(i) for i in infos]
    cu_names = {c.device.replace("/dev/cu.", "", 1) for c in cands if c.device.startswith("/dev/cu.")}
    out = [c for c in cands if not (c.device.startswith("/dev/tty.") and c.device.replace("/dev/tty.", "", 1) in cu_names)]
    return sorted(out, key=lambda c: (not c.likely_esp32, c.excluded, c.device))


def likely_esp32_ports(candidates: Iterable[PortCandidate]) -> list:
    return [c for c in candidates if c.likely_esp32 and not c.excluded]


def probe_port(device: str, baud: int = 921600, duration_s: float = 2.5, serial_factory=None) -> dict:
    """Open ``device`` read-only for ``duration_s`` and count CSI / stats lines.

    Used to tell the CSI receiver apart from the transmitter (or any other
    board) when several ESP32s are plugged in. Never writes to the device.
    """
    import serial

    factory = serial_factory or (lambda: serial.serial_for_url(device, baudrate=baud, timeout=0.2))
    counts = {"csi": 0, "rx_stats": 0, "tx_stats": 0, "lines": 0, "error": ""}
    try:
        ser = factory()
    except Exception as e:  # noqa: BLE001 - report any open failure
        counts["error"] = str(e)
        return counts
    try:
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            counts["lines"] += 1
            if b"CSI_DATA" in raw:
                counts["csi"] += 1
            elif b"EG_RX_STATS" in raw:
                counts["rx_stats"] += 1
            elif b"EG_TX_STATS" in raw:
                counts["tx_stats"] += 1
    except Exception as e:  # noqa: BLE001
        counts["error"] = str(e)
    finally:
        try:
            ser.close()
        except Exception:  # noqa: BLE001
            pass
    return counts


def auto_select(candidates: Optional[list] = None, probe: Optional[Callable[[str], dict]] = None) -> str:
    """Pick the CSI receiver port.

    * exactly one likely ESP32 -> that port
    * several and ``probe`` given -> the single one that emits CSI_DATA / EG_RX_STATS
    * otherwise raise :class:`PortSelectionError` with a helpful message
    """
    if candidates is None:
        candidates = list_candidates()
    likely = likely_esp32_ports(candidates)
    if not likely:
        raise PortSelectionError(
            "No likely ESP32 serial port found. Plug in the receiver with a data-capable USB cable, "
            "run 'make ports', or pass --port /dev/cu.<name>."
        )
    if len(likely) == 1:
        return likely[0].device
    names = ", ".join(c.device for c in likely)
    if probe is not None:
        receivers = []
        for c in likely:
            res = probe(c.device)
            if res.get("csi", 0) > 0 or res.get("rx_stats", 0) > 0:
                receivers.append(c.device)
        if len(receivers) == 1:
            return receivers[0]
        if len(receivers) > 1:
            raise PortSelectionError(f"Several ports emit CSI data ({', '.join(receivers)}); pass --port explicitly.")
        raise PortSelectionError(f"Several ESP32-like ports ({names}) but none is emitting CSI_DATA; pass --port explicitly.")
    raise PortSelectionError(f"Several ESP32-like ports found ({names}); pass --port explicitly.")
