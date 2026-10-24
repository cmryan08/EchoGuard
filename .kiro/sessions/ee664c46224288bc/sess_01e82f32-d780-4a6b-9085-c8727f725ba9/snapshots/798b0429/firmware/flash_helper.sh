#!/usr/bin/env bash
# EchoGuard flash helper.
#
# Builds and flashes ONE board with the EchoGuard transmitter or receiver
# firmware, then opens the serial monitor. Safety rules:
#   * an explicit --port is required (no auto-detection, no "flash everything")
#   * macOS pseudo ports (Bluetooth-Incoming-Port, debug-console) are refused
#   * the chip is identified first with the READ-ONLY `esptool.py chip_id`
#   * the detected chip must match --target
#   * you must type the port name again to confirm before anything is written
#
# NEVER point this at the third ESP32 that holds firmware you want to keep.
# Unplug that board before running this script.
#
# Usage:
#   firmware/flash_helper.sh --port /dev/cu.usbmodem1101 --role rx --target esp32s3 [--usbjtag] [--no-monitor]
#
# Requires an activated ESP-IDF environment (". $HOME/esp/esp-idf/export.sh").
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PORT=""
ROLE=""
TARGET=""
USBJTAG=0
MONITOR=1

usage() {
    cat <<'EOF'
Usage: flash_helper.sh --port <serial-port> --role tx|rx --target <idf-target> [--usbjtag] [--no-monitor]

  --port       Serial port of the board to flash, e.g. /dev/cu.usbmodem1101
  --role       tx (transmitter) or rx (CSI receiver)
  --target     ESP-IDF target: esp32, esp32s3, esp32c3, esp32c6
  --usbjtag    Route the console to native USB-Serial-JTAG
               (required for Seeed XIAO ESP32-S3 and similar boards)
  --no-monitor Flash only, do not start idf.py monitor
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="${2:-}"; shift 2 ;;
        --role) ROLE="${2:-}"; shift 2 ;;
        --target) TARGET="${2:-}"; shift 2 ;;
        --usbjtag) USBJTAG=1; shift ;;
        --no-monitor) MONITOR=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$PORT" || -z "$ROLE" || -z "$TARGET" ]]; then
    echo "ERROR: --port, --role and --target are all required." >&2
    usage >&2
    exit 2
fi

case "$ROLE" in
    tx) PROJECT_DIR="$SCRIPT_DIR/transmitter" ;;
    rx) PROJECT_DIR="$SCRIPT_DIR/receiver" ;;
    *) echo "ERROR: --role must be tx or rx" >&2; exit 2 ;;
esac

case "$TARGET" in
    esp32) CHIP_PATTERN="ESP32" ;;
    esp32s3) CHIP_PATTERN="ESP32-S3" ;;
    esp32c3) CHIP_PATTERN="ESP32-C3" ;;
    esp32c6) CHIP_PATTERN="ESP32-C6" ;;
    *) echo "ERROR: unsupported --target '$TARGET' (use esp32, esp32s3, esp32c3, esp32c6)" >&2; exit 2 ;;
esac

if [[ "$USBJTAG" -eq 1 && "$TARGET" == "esp32" ]]; then
    echo "ERROR: the classic ESP32 has no USB-Serial-JTAG; do not use --usbjtag with --target esp32" >&2
    exit 2
fi

case "$PORT" in
    *Bluetooth-Incoming-Port*|*debug-console*)
        echo "ERROR: $PORT is a macOS system port, not an ESP32. Refusing." >&2
        exit 2 ;;
esac

if [[ ! -e "$PORT" ]]; then
    echo "ERROR: $PORT does not exist. Run 'make ports' to list candidates." >&2
    exit 2
fi

if command -v esptool.py >/dev/null 2>&1; then
    ESPTOOL=(esptool.py)
elif command -v esptool >/dev/null 2>&1; then
    ESPTOOL=(esptool)
else
    ESPTOOL=(python -m esptool)
fi

if ! command -v idf.py >/dev/null 2>&1; then
    echo "ERROR: idf.py not found. Activate ESP-IDF first: . \$HOME/esp/esp-idf/export.sh" >&2
    exit 2
fi

echo "=================================================================="
echo " WARNING: Never flash the third ESP32 that holds existing firmware."
echo " Unplug it now if it is connected. Flashing OVERWRITES the board."
echo "=================================================================="
echo
echo "Identifying the chip on $PORT (read-only: ${ESPTOOL[*]} --port $PORT chip_id) ..."
set +e
CHIP_OUT="$("${ESPTOOL[@]}" --port "$PORT" chip_id 2>&1)"
CHIP_RC=$?
set -e
echo "$CHIP_OUT" | grep -E "Chip is|Chip type|MAC:|Features|Crystal" || true
if [[ $CHIP_RC -ne 0 ]]; then
    echo "$CHIP_OUT" >&2
    echo "ERROR: chip identification failed (rc=$CHIP_RC). Hold BOOT while plugging in, or check the cable." >&2
    exit 1
fi

DETECTED="$(echo "$CHIP_OUT" | sed -n -E 's/.*(Chip is|Chip type:)[[:space:]]*(ESP32[-A-Z0-9]*).*/\2/p' | head -n 1)"
if [[ -z "$DETECTED" ]]; then
    echo "ERROR: could not parse the chip type from esptool output." >&2
    exit 1
fi
if [[ "$DETECTED" != "$CHIP_PATTERN" ]]; then
    echo "ERROR: detected chip '$DETECTED' does not match --target $TARGET (expected '$CHIP_PATTERN')." >&2
    exit 1
fi

echo
echo "About to build and FLASH the EchoGuard ${ROLE^^} firmware:"
echo "  project : $PROJECT_DIR"
echo "  target  : $TARGET (detected $DETECTED)"
echo "  port    : $PORT"
echo "  console : $([[ $USBJTAG -eq 1 ]] && echo 'USB-Serial-JTAG' || echo 'UART (sdkconfig.defaults)')"
echo
read -r -p "Type the port name ($PORT) to confirm, anything else aborts: " CONFIRM
if [[ "$CONFIRM" != "$PORT" ]]; then
    echo "Aborted. Nothing was written."
    exit 1
fi

cd "$PROJECT_DIR"

if [[ "$USBJTAG" -eq 1 ]]; then
    DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.usbjtag"
else
    DEFAULTS="sdkconfig.defaults"
fi

# set-target regenerates sdkconfig from the defaults files; only needed when the
# target or console choice changed since the last build.
STAMP=".echoguard_target"
WANT="$TARGET usbjtag=$USBJTAG"
if [[ ! -f sdkconfig || ! -f "$STAMP" || "$(cat "$STAMP")" != "$WANT" ]]; then
    rm -f sdkconfig
    idf.py -D SDKCONFIG_DEFAULTS="$DEFAULTS" set-target "$TARGET"
    echo "$WANT" > "$STAMP"
fi

if [[ "$MONITOR" -eq 1 ]]; then
    idf.py -p "$PORT" flash monitor
else
    idf.py -p "$PORT" flash
fi
