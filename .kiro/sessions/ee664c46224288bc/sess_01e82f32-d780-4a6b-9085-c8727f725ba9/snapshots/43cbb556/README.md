# EchoGuard

Privacy-preserving, camera-free motion and potential-fall sensing with Wi-Fi
Channel State Information (CSI) from two ESP32 boards.

> **Disclaimer.** EchoGuard is a hackathon research prototype. It is **not a
> medical device**, is **not medically validated**, and must not be relied on
> to detect falls or summon help. No accuracy claims are made.

## Status

| Milestone | State |
| --- | --- |
| 1. CSI connection: firmware, serial parser, recorder, CLI monitor | implemented |
| 2. Live dashboard: signal processing, motion score, local web UI | implemented |
| 3. Calibration and labeled recording | planned |
| 4. Heuristic fall detection | planned |
| 5. Simulation, replay polish, export | planned |

**Not verified yet:** the firmware has not been compiled (ESP-IDF was not
installed on the development machine) and no ESP32 was connected, so the
hardware path (flash, CSI capture, serial streaming) has **not** been tested
end to end. The Python side is tested against the documented Espressif format
using format fixtures (`tests/fixtures/`, not real captures).

## Architecture

```
 ESP32 #1 (transmitter)            ESP32 #2 (receiver)              MacBook
 firmware/transmitter     ESP-NOW  firmware/receiver      USB       backend/
 broadcast 100 Hz  ~~~~~~~~~~~~~~> CSI callback -> queue  ======>   parser -> buffer
 ch 11, HT40, MCS0                 printer task: CSI_DATA  serial   -> recorder / CLI
 MAC 1a:00:00:00:00:00             lines at 921600 baud             -> processing -> dashboard
```

* Two ESP32s only. No router, no mesh, no internet at runtime.
* The receiver prints the official esp-csi `CSI_DATA` CSV format, so the
  official Espressif tools also work with it.
* A Raspberry Pi can replace the Mac as host (same Python code), but is not required.

## Repository layout

```
firmware/transmitter/   ESP-IDF project (from esp-csi csi_send)
firmware/receiver/      ESP-IDF project (from esp-csi csi_recv)
firmware/flash_helper.sh  guarded build+flash helper (explicit port + confirmation)
backend/                csi_parser, sources (serial/replay), ports, buffer, recorder, cli,
                        config, processing, activity, engine
dashboard/              Flask server (server.py, __main__.py) + static/ offline UI (HTML/CSS/JS)
tests/                  pytest suite + format fixtures
data/                   config.json, recordings/, calibration/, exports/, models/ (gitignored)
```

## macOS setup (host software)

```bash
brew install python@3.11        # if /opt/homebrew/bin/python3.11 is missing
make setup                      # creates .venv and installs pinned requirements.txt
make test                       # runs the unit tests
```

`make setup` prefers Python 3.11 (falls back to 3.12). Python 3.14 is avoided
because binary wheels for some pinned packages may not exist yet. Internet is
only needed for this install step.

No internet? If a local interpreter already has the packages from
`requirements.txt` (numpy, scipy, pandas, pyserial, flask, pytest), build the
venv on top of it without any download:

```bash
make setup-offline                                   # default: ~/miniconda3/bin/python3.12
make setup-offline PYTHON_BOOTSTRAP=/path/to/python3 # any other interpreter
```

This recreates `.venv` with `--system-site-packages` and checks the imports.
Optional ML extras (scikit-learn, joblib, used later for Random Forest
training) are in `requirements-ml.txt`: `make setup-ml`.

## ESP-IDF installation (macOS)

```bash
brew install cmake ninja dfu-util ccache
mkdir -p ~/esp && cd ~/esp
git clone -b v5.5.1 --recursive https://github.com/espressif/esp-idf.git
cd esp-idf && ./install.sh esp32,esp32s3,esp32c3,esp32c6
. ./export.sh                    # run in every new terminal (or add an alias)
idf.py --version
```

See the [ESP-IDF get-started guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/get-started/index.html)
for details. The first receiver build downloads the `esp_csi_gain_ctrl`
component (needs internet once).

## Identify your boards (read-only)

```bash
make ports                                   # lists ports, flags likely ESP32s
esptool.py --port /dev/cu.usbmodemXXXX chip_id   # prints chip type + MAC, writes nothing
```

`chip_id` resets the board but does not write flash. **Do not connect the third
ESP32 that holds firmware you want to keep**; if it is plugged in, unplug it
before flashing anything. Nothing in this repository flashes automatically.

## Build and flash

Pick the target that `chip_id` reported (`esp32`, `esp32s3`, `esp32c3`, `esp32c6`).

Receiver (CSI source, connected to the Mac):

```bash
. ~/esp/esp-idf/export.sh
cd firmware/receiver
idf.py set-target esp32s3                    # use your chip
idf.py -p /dev/cu.usbserial-XXXX flash monitor
```

Transmitter (can be powered by any USB supply once flashed):

```bash
cd firmware/transmitter
idf.py set-target esp32                      # use your chip
idf.py -p /dev/cu.usbserial-YYYY flash monitor
```

Seeed Studio XIAO ESP32-S3 (and other boards with only native USB): add the
USB-Serial-JTAG console overlay, otherwise no serial output appears:

```bash
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.usbjtag" set-target esp32s3
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

Guarded alternative (read-only chip check, target match, typed confirmation):

```bash
firmware/flash_helper.sh --port /dev/cu.usbmodemXXXX --role rx --target esp32s3 --usbjtag
firmware/flash_helper.sh --port /dev/cu.usbserial-YYYY --role tx --target esp32
```

More details and all Kconfig options: [firmware/README.md](firmware/README.md).

## Milestone 1 usage

Close `idf.py monitor` first (`Ctrl-]`), then:

```bash
make ports
make monitor                      # PORT=auto; or: make monitor PORT=/dev/cu.usbmodemXXXX
.venv/bin/python -m backend.cli monitor --port auto --record data/recordings/test.csv
.venv/bin/python -m backend.cli monitor --replay data/recordings/test.csv
.venv/bin/python -m backend.cli monitor --replay tests/fixtures/classic_esp32s3_sample.txt --duration 3
```

Once per second the monitor prints connection state, total CSI packets, packet
rate, malformed lines, sequence gaps, RSSI (mean/std), noise floor, CSI length,
chip family, mean amplitude and a sparkline of the 52 valid LLTF subcarrier
amplitudes, plus the device's own `EG_RX_STATS`. Before any packet arrives it
prints `waiting for CSI packets` (no values are invented). Walk between the
boards and the sparkline / mean amplitude should change.

`--port auto` picks the single likely ESP32 port; if several are connected it
listens (read-only) on each for `CSI_DATA` and picks the one that emits it.
The serial source reconnects automatically after unplugging.

Recordings are CSV files with `host_ts_iso,host_ts_unix,raw_line`; replay feeds
the raw lines through the same parser. Raw `idf.py monitor` logs (`.txt`/`.log`)
can be replayed too (at 100 lines/s, since they have no host timestamps).

## Milestone 2: live dashboard

```bash
make dashboard                                   # http://127.0.0.1:8050/ opens in the browser
make dashboard ARGS="--serial auto"              # start reading the receiver right away
.venv/bin/python -m dashboard --serial /dev/cu.usbmodemXXXX
.venv/bin/python -m dashboard --replay data/recordings/test.csv --speed 1 --loop
.venv/bin/python -m dashboard --no-browser --port 8599 --replay tests/fixtures/classic_esp32s3_sample.txt
```

In the page, the **Data source** card selects a serial port (`auto` or a
listed `/dev/cu.*` device, baud 921600) or a replay file (recordings under
`data/` and the format fixtures), with speed and loop options. Close
`idf.py monitor` first; only one program can open the port.

Panels:

* **Connection status**: source state, port, reconnects and last error;
  packet rate and counts; RSSI (last, mean, sd), noise floor and mean CSI
  amplitude; malformed lines (by reason), sequence gaps and duplicates;
  a signal-quality badge with the reasons when data is insufficient; the TX/RX
  configuration seen in the packets (chip family, channel, secondary channel,
  bandwidth, CSI length, subcarrier count, TX MAC) and the device's own
  `EG_RX_STATS` / `EG_TX_STATS`.
* **Motion classification**: one of *Empty room / low activity*, *Movement
  detected*, *Rapid movement*, *Potential fall* (set by the fall detector in
  Milestone 4) or *Unknown / insufficient data*, plus the current motion
  score, baseline deviation and thresholds.
* **Live CSI**: scrolling heatmap of the normalised subcarrier amplitudes
  (time left to right, newest at right, with colour scale), mean amplitude,
  motion activity score with the movement / rapid threshold lines, baseline
  deviation, and RSSI over the last 30 s.

A **REPLAY of recorded data** banner is shown during replays (a
**SIMULATED DATA** banner is reserved for the simulation mode). Before data
arrives every field shows `No data yet` / `-`; nothing is invented.

Signal processing (`backend/processing.py`), per packet: the 52 valid LLTF
subcarrier amplitudes (C5/C6: all non-zero subcarriers) are divided by their
mean (removes AGC gain jumps), a Hampel filter replaces impulsive outliers per
subcarrier, and an EMA smooths them. Over a 1 s window it computes the
temporal **variance** (mean per-subcarrier std), the **diff energy** (RMS of
the first difference), the **baseline deviation** (mean z-score of the window
profile against the calibration baseline, or against a slow 30 s adaptive
reference until a calibration exists) and the **motion score**
`0.5 * (variance / noise_var + diff_energy / noise_diff)`; with a calibration
an empty room scores about 1. Data quality is *insufficient* (activity
*Unknown*) when the packet rate is below 20 Hz, the window is less than 70%
covered, the CSI layout changed, or no packet arrived for 1 s. Parameters and
thresholds live in `data/config.json` (created with defaults on first start).
**Until calibration (Milestone 3) the thresholds are uncalibrated defaults**,
so the classification is only indicative.

Framework choice: the dashboard is a small Flask app with a self-contained
HTML/CSS/vanilla-JavaScript page that draws its charts on `<canvas>`. It needs
no CDN or internet (Streamlit/Plotly could not be installed offline and are
not required). The page polls `/api/status`, `/api/series` and `/api/heatmap`
about 2.5 times per second.

**Security note:** the dashboard has **no authentication**. It binds to
`127.0.0.1` by default. `--host 0.0.0.0` (e.g. to view it from another
machine, or on a Raspberry Pi host) prints a warning: anyone who can reach the
address can see the data and change the data source. Only do that on a
trusted network.

## Serial format

Receiver lines follow the official esp-csi format.

Classic ESP32 / ESP32-S3 / ESP32-C3 (25 fields):

```
type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,len,first_word,data
CSI_DATA,0,94:d9:b3:80:8c:81,-30,11,1,6,1,0,1,0,1,0,0,-93,0,13,2,2751923,0,67,0,128,1,"[67,48,4,0,...]"
```

ESP32-C5 / ESP32-C6 (15 fields):

```
type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_state,len,first_word,data
```

* `id` / `seq`: transmitter packet counter (used for loss / gap counting).
* `local_timestamp`: receiver clock in microseconds.
* `len`: number of integers in `data`. With `ECHOGUARD_RX_LLTF_ONLY` (default on
  classic chips) it is 128.
* `first_word`: 1 means the first 4 values (subcarriers 0 and 1) are invalid.
* `data`: interleaved `[imag, real]` per subcarrier; decoded as
  `complex(raw[2k+1], raw[2k])` like the official parser. On classic chips the
  first 64 subcarriers are the LLTF in order 0..31, -32..-1; the 52 valid ones
  are indices 1..26 and 38..63.

EchoGuard additions (one line per second):

```
EG_RX_STATS,<uptime_ms>,<csi_rx_total>,<csi_rx_rate_hz>,<queue_drops>,<seq_gaps>,<last_rssi>
EG_TX_STATS,<uptime_ms>,<sent_ok>,<send_fail>,<rate_hz>
```

## Serial troubleshooting

* **Port names on macOS.** Native USB (XIAO ESP32-S3, ESP32-C3/C6/S3 USB
  port): `/dev/cu.usbmodem*`. USB-UART bridges: `/dev/cu.usbserial-*`,
  `/dev/cu.SLAB_USBtoUART` (CP210x), `/dev/cu.wchusbserial*` (CH34x). Use
  `/dev/cu.*`, not `/dev/tty.*`. `Bluetooth-Incoming-Port` and `debug-console`
  are macOS system ports, never an ESP32.
* **No port appears.** Try another cable (many are charge-only) and another USB
  port/hub. Recent macOS includes CP210x and CH34x drivers; for older systems
  install the vendor driver (Silicon Labs CP210x VCP, WCH CH34x).
* **Cannot connect / download mode.** Hold BOOT, tap RESET (or plug in while
  holding BOOT), then flash. Native-USB boards may re-enumerate under a new
  `usbmodem` name after flashing; re-run `make ports`.
* **Port busy.** Only one program can open a port. Close `idf.py monitor`
  (`Ctrl-]`), the Arduino serial monitor, `screen`, or another EchoGuard
  monitor.
* **Garbage characters.** The receiver UART runs at **921600** baud (`--baud
  921600`, the default). The transmitter uses the ESP-IDF default 115200.
* **No output on XIAO ESP32-S3.** The default config prints to UART0 pins, not
  the USB-C port. Rebuild with `sdkconfig.defaults.usbjtag` (see above).
* **`waiting for CSI packets` but RX stats show 0 Hz.** Check that the
  transmitter is powered and both use the same channel (`ECHOGUARD_*_CHANNEL`).
* **High `queue_drops` or malformed lines.** The serial link is saturated:
  keep `ECHOGUARD_RX_LLTF_ONLY=y`, or lower `ECHOGUARD_TX_RATE_HZ`.

## Tests

```bash
make test
```

Covers parsing of the documented official example lines (both variants),
complex decoding order, LLTF subcarrier selection and masking, malformed line
handling, stats lines, the bounded buffer (rate, sequence gaps with
wraparound), record/replay round trip, serial reconnect (fake serial and
pyserial `loop://`) and port classification.
