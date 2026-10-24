# EchoGuard firmware

Two standalone ESP-IDF projects derived from the official
[espressif/esp-csi get-started examples](https://github.com/espressif/esp-csi/tree/master/examples/get-started)
(`csi_send` and `csi_recv`):

| Project | Role | Based on |
| --- | --- | --- |
| `transmitter/` | ESP-NOW broadcast from fixed MAC `1a:00:00:00:00:00`, channel 11, HT40, MCS0 LGI, 100 Hz | `csi_send` |
| `receiver/` | STA + promiscuous, CSI enabled, filters the TX MAC, prints official `CSI_DATA` lines over serial | `csi_recv` |

No router, no mesh, no internet at runtime. The only network access needed is
on the **first receiver build**: the IDF component manager downloads
`esp_csi_gain_ctrl` (>=0.1.4) into `receiver/managed_components/`.

> Not verified here: these projects were written against the official examples
> but were **not compiled** on the development machine (ESP-IDF was not
> installed) and have not been run on hardware yet.

## Configuration (idf.py menuconfig)

| Option | Project | Default | Meaning |
| --- | --- | --- | --- |
| `ECHOGUARD_TX_CHANNEL` | transmitter | 11 | Wi-Fi channel 1..13 |
| `ECHOGUARD_TX_RATE_HZ` | transmitter | 100 | packets per second 10..200 |
| `ECHOGUARD_RX_CHANNEL` | receiver | 11 | must equal the TX channel |
| `ECHOGUARD_RX_LLTF_ONLY` | receiver (classic chips) | y | print only the 128 LLTF values so 100 Hz fits 921600 baud |
| `ECHOGUARD_RX_MAX_CSI_BYTES` | receiver | 384 (612 on C5/C6) | per-packet copy size |
| `ECHOGUARD_RX_QUEUE_LEN` | receiver | 32 | callback-to-printer queue depth |

## Build and flash

Activate ESP-IDF first (`. $HOME/esp/esp-idf/export.sh`). Replace the port
with the one shown by `make ports`. Identify the chip first with the read-only
`esptool.py --port <PORT> chip_id` and pick the matching target.

Standard boards with a USB-UART bridge (CP210x / CH34x), console on UART:

```bash
# Receiver (pick ONE target: esp32 | esp32s3 | esp32c3 | esp32c6)
cd firmware/receiver
idf.py set-target esp32s3
idf.py -p /dev/cu.usbserial-XXXX flash monitor

# Transmitter
cd firmware/transmitter
idf.py set-target esp32
idf.py -p /dev/cu.usbserial-YYYY flash monitor
```

Boards whose only USB port is the chip's native USB-Serial-JTAG (Seeed Studio
XIAO ESP32-S3, many ESP32-C3/C6 mini boards) need the console on
USB-Serial-JTAG, otherwise you see no output:

```bash
cd firmware/receiver
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.usbjtag" set-target esp32s3
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

`set-target` regenerates `sdkconfig` from the defaults files. If you change
target or console type, delete `sdkconfig` (or run `set-target` again).

The guarded helper does the same after a read-only chip check and an explicit
confirmation (never use it on the third board you want to keep):

```bash
firmware/flash_helper.sh --port /dev/cu.usbmodemXXXX --role rx --target esp32s3 --usbjtag
```

Exit `idf.py monitor` with `Ctrl-]` before running `make monitor`; only one
program can hold the serial port.

## Serial output

Receiver: official CSV header once, then one `CSI_DATA` line per packet (see
the main README for the field list), plus once per second:

```
EG_RX_STATS,<uptime_ms>,<csi_rx_total>,<csi_rx_rate_hz>,<queue_drops>,<seq_gaps>,<last_rssi>
```

Transmitter, once per second:

```
EG_TX_STATS,<uptime_ms>,<sent_ok>,<send_fail>,<rate_hz>
```

## Differences from the official examples

Transmitter: Kconfig channel/rate, HT40 secondary channel "above" for channels
1..4 (official always "below"), `vTaskDelayUntil` pacing, counted send errors
with a short back-off on `ESP_ERR_ESPNOW_NO_MEM`, startup banner, stats line.

Receiver: no printing in the CSI callback (copy into a FreeRTOS queue, print
from a separate task); per-packet `compensate_gain` log removed; LLTF-only
option; the classic `rx_state` column prints `rx_ctrl->rx_state` (official code
prints `sig_mode` twice); the classic header says `rx_state` as in the official
README (official firmware prints `rx_format`); C5/C6 keep the official
`cur_bb_format` value in that column; stats line; startup banner; fixed CMake
project names without `git_describe`.
