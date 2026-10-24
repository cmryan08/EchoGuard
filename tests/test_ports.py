from types import SimpleNamespace

import pytest

from backend.ports import PortSelectionError, auto_select, classify_port, list_candidates


def _info(device, vid=None, pid=None, description="n/a"):
    return SimpleNamespace(device=device, vid=vid, pid=pid, description=description,
                           manufacturer=None, serial_number=None)


MAC_SYSTEM = [_info("/dev/cu.Bluetooth-Incoming-Port"), _info("/dev/cu.debug-console")]


@pytest.mark.parametrize(
    "info,likely,bridge_part",
    [
        (_info("/dev/cu.usbmodem1101", 0x303A, 0x1001), True, "Espressif"),
        (_info("/dev/cu.SLAB_USBtoUART", 0x10C4, 0xEA60), True, "CP210x"),
        (_info("/dev/cu.wchusbserial110", 0x1A86, 0x7523), True, "CH34x"),
        (_info("/dev/cu.usbserial-A50285BI", 0x0403, 0x6001), True, "FTDI"),
        (_info("/dev/cu.usbserial-1410"), True, "unknown"),
        (_info("/dev/cu.usbmodemXYZ", 0x05AC, 0x1234), False, ""),
    ],
)
def test_classification(info, likely, bridge_part):
    c = classify_port(info)
    assert c.likely_esp32 is likely
    assert bridge_part in c.bridge
    assert not c.excluded


def test_macos_system_ports_excluded():
    for info in MAC_SYSTEM:
        c = classify_port(info)
        assert c.excluded and not c.likely_esp32


def test_current_hardware_state_no_esp32():
    cands = list_candidates(lambda: MAC_SYSTEM)
    with pytest.raises(PortSelectionError, match="No likely ESP32"):
        auto_select(cands)


def test_single_candidate_selected_and_tty_twin_dropped():
    infos = MAC_SYSTEM + [_info("/dev/cu.usbmodem1101", 0x303A, 0x1001), _info("/dev/tty.usbmodem1101", 0x303A, 0x1001)]
    cands = list_candidates(lambda: infos)
    assert [c.device for c in cands if c.likely_esp32] == ["/dev/cu.usbmodem1101"]
    assert auto_select(cands) == "/dev/cu.usbmodem1101"


def test_ambiguous_without_probe_errors():
    infos = [_info("/dev/cu.usbmodem1101", 0x303A, 0x1001), _info("/dev/cu.usbserial-10", 0x10C4, 0xEA60)]
    with pytest.raises(PortSelectionError, match="Several"):
        auto_select(list_candidates(lambda: infos))


def test_ambiguous_resolved_by_probe():
    infos = [_info("/dev/cu.usbmodem1101", 0x303A, 0x1001), _info("/dev/cu.usbserial-10", 0x10C4, 0xEA60)]
    results = {"/dev/cu.usbmodem1101": {"csi": 0, "tx_stats": 3}, "/dev/cu.usbserial-10": {"csi": 120}}
    assert auto_select(list_candidates(lambda: infos), probe=lambda d: results[d]) == "/dev/cu.usbserial-10"

    none = {"/dev/cu.usbmodem1101": {"csi": 0}, "/dev/cu.usbserial-10": {"csi": 0}}
    with pytest.raises(PortSelectionError, match="none is emitting"):
        auto_select(list_candidates(lambda: infos), probe=lambda d: none[d])
