# EchoGuard

A camera-free sitting / standing time tracker. Two ESP32-S3 boards exchange
Wi-Fi packets across the room, and the Channel State Information (CSI) of
those packets changes when you sit, stand, walk or leave. A local web app shows
how long you have been sitting and standing today, reminds you to stand up,
keeps a daily history, shows general health information about prolonged
sitting, and exports your data so you can share it with a health provider.
No camera and no microphone are used.

> **Disclaimer.** EchoGuard is a hackathon research prototype. It is **not a
> medical device** and is **not medically validated**. Sitting and standing are
> estimated from Wi-Fi signals and may be wrong. No accuracy claims are made:
> the only number shown is a within-calibration self-check, which is
> optimistic and is not validated accuracy.

## Quick start

```bash
make setup                                          # or, without internet: make setup-offline
make test
make dashboard ARGS="--serial /dev/cu.usbmodem2101" # live: receiver plugged into the Mac
make dashboard-sim SCENARIO=desk_day                # SYNTHETIC demo, no hardware
```

The app opens at http://127.0.0.1:8050/ (use `make app` as a shorter alias
for `make dashboard`). Then open **Calibrate** and run the calibration once
for your room (1 min 35 s, hands-free, see below). Until the room is
calibrated the app shows *Unknown - Not calibrated*.

Hardware: the current firmware uses **ESP-NOW** directly between the two
boards (no router, no internet). The transmitter runs on wall power across the
room; the receiver is plugged into the Mac and streams `CSI_DATA` lines over
USB at 921600 baud. **No reflash is needed for this version**: the
sitting/standing tracker is entirely on the Python / web side.

## The web app

Pages (top navigation):

* **Today**: the current state in large text (*Sitting*, *Standing*,
  *Moving*, *Away* or *Unknown* with the reason), confidence, the current
  streak (*Sitting for 23 min*), tiles for sitting / standing / moving today,
  upright total, stand hours versus goal, sit-to-stand transitions and the
  longest sitting bout, plus an hourly bar chart. A stand reminder banner
  appears after 30 min of continuous sitting (*Snooze 10 min*, optional
  desktop notification after you allow it, and a short beep). Reminder
  settings are at the bottom of the page.
* **History**: last 7 or 30 days as a chart and a table (sitting, standing,
  moving, upright, average and longest sitting bout, transitions, stand hours).
* **Health**: general, non-prescriptive information about prolonged sitting
  (WHO 2020 guidelines; Dunstan et al., Diabetes Care 2012) next to your own
  7-day averages. It is not medical advice.
* **Share**: what is collected, a date range, CSV / JSON downloads and a
  print-friendly summary for a health provider.
* **Calibrate**: the timed calibration with spoken instructions (below).
* **Sensor**: data source (serial port or `auto`, replay file, simulation,
  Stop), connection status (packets/s, RSSI, data quality, sensor
  configuration) and a live motion score chart.

"Upright" means standing + moving, like Apple Watch stand hours. A
**SYNTHETIC** banner is shown whenever the source is the simulator, and a
*Replay, not saved* note during replays; neither is ever written to your
history.

The UI is plain HTML/CSS/JavaScript served by Flask, with no CDN, no web
fonts and a strict Content-Security-Policy. **Security note:** the dashboard
has **no authentication**. It binds to `127.0.0.1` by default; `--host
0.0.0.0` prints a warning. Use it only on a trusted network.

## Calibration

The posture model is specific to your room and board placement, so calibrate
once after setting up (and again after moving the boards or furniture).
Start the live source, open **Calibrate**, enter a room name, press **Test
sound** (this also unlocks audio in the browser) and then **Start
calibration**. The run is hands-free: a beep and a spoken instruction mark
every phase, so you know when to come back in even from outside the room.

Default timeline (each recorded step includes 1 s that is discarded while
the analysis window clears):

| At | What to do | Length |
| --- | --- | --- |
| 0:00 | Press Start, leave the room | 5 s |
| 0:05 | Empty room recorded (stay out) | 21 s |
| 0:26 | Come back in | 5 s |
| 0:31 | Walk back and forth between the boards (recorded) | 16 s |
| 0:47 | Stop walking and stand still | 3 s |
| 0:50 | Standing still recorded (where you normally stand) | 21 s |
| 1:11 | Walk to the chair and sit down | 3 s |
| 1:14 | Sitting recorded (your usual chair) | 21 s |
| 1:35 | Done - total 1 min 35 s | |

The time to leave (0-60 s), to come back (0-60 s), to change position (0-30 s)
and every step duration can be changed before starting; the schedule and the
total update live. During the run the page shows a big countdown, the current
instruction, what comes next and a progress bar. Afterwards it shows the data
quality of each step (with **Redo** for a failed step), warnings (for example
*sitting and standing look similar at this placement*), the sitting-vs-standing
self-check labelled *within-calibration check, optimistic*, and **Save**.
Saved calibrations live in `data/calibration/` and load automatically when
the same sensor configuration is seen again. You can load another one or
clear the active one.

The equivalent API: `POST /api/calibration/start` (`{"placement": "office",
"auto": true, "lead_in_s": 5, "grace_s": 5, "transition_s": 3}`), then `POST
/api/calibration/step/start`; poll `GET /api/calibration/state` (the
`session.timeline` holds the schedule, `session.phase` the countdown and cue);
`POST /api/calibration/save`.

## How detection works

1. **Motion gating.** Each 1 s window gets a motion score (variance and diff
   energy of the normalised, filtered subcarrier amplitudes relative to the
   empty-room noise). High scores mean *Moving*.
2. **Posture model.** When the person is still, the amplitude profile across
   the 52 subcarriers is compared with the reference profiles recorded during
   calibration (empty room, standing still, sitting) to decide *Away*,
   *Standing* or *Sitting*, with a probability.
3. **Hysteresis.** A new state must be confident (default 60%) and persist for
   a few seconds before it is accepted, so brief fidgeting does not flip the
   state. Short gaps in the data are bridged; long ones become *Unknown*.

Daily totals, bouts and stand hours are stored in `data/posture.sqlite3`
(live data only).

## Limitations

* Location dependent: the model only works for the room and board placement
  it was calibrated in. Recalibrate when the boards, the chair or large
  furniture move.
* A person sitting or standing still away from the line between the two
  boards may change the signal very little and can read as *Away*.
* Sitting and standing can look similar at some placements; the calibration
  warns when they do. Put the TX-RX line near the chair at seated torso height.
* One person at a time. Pets, fans and moving curtains add motion.
* The within-calibration self-check is optimistic (same session, same person)
  and is not validated accuracy.

## Data, privacy and export

Only time spent sitting, standing, moving and away, and reminder responses,
are stored, locally, in `data/posture.sqlite3`. No camera, no audio, and
nothing is sent anywhere automatically. **Share** (or
`GET /api/posture/export?format=csv|json&days=30`) downloads a file you can
give to a health provider; the print summary includes the method and the
disclaimer.

## Architecture

```
 ESP32-S3 #1 (transmitter)         ESP32-S3 #2 (receiver)           MacBook
 firmware/transmitter     ESP-NOW  firmware/receiver      USB       backend/
 broadcast 100 Hz  ~~~~~~~~~~~~~~> CSI callback -> queue  ======>   parser -> buffer -> processing
 fixed channel                     printer task: CSI_DATA  serial   -> motion score + posture model
                                   lines at 921600 baud             -> sitting tracker (sqlite history)
                                                                    -> dashboard (Flask, 127.0.0.1)
 simulation/ (SYNTHETIC CSI_DATA lines, MAC 02:51:4d:00:00:00) ---> same parser and pipeline
```

* Only two ESP32s are needed. No router, no mesh and no internet at runtime.
* The receiver prints the official esp-csi `CSI_DATA` CSV format.
* Live serial data, replays and the simulator go through the same pipeline.
* The earlier fall-detection features (heuristic, alerts, labeled recording,
  RF training) remain in the backend and its API, but are no longer shown in
  the web app. They are documented further below.

## Repository layout

```
firmware/transmitter/     ESP-IDF project (from esp-csi csi_send)
firmware/receiver/        ESP-IDF project (from esp-csi csi_recv)
firmware/flash_helper.sh  guarded build+flash helper (explicit port + confirmation)
backend/                  csi_parser, sources (serial/replay), ports, buffer, recorder, cli,
                          config, processing, activity, engine, calibration, recording,
                          detector (fall heuristic), alerts (alerts + event log), monitor (wiring),
                          offline (offline replay), export (CSV), ml/train_rf (optional)
dashboard/                Flask server (server.py, __main__.py) + static/ offline UI (HTML/CSS/JS)
simulation/               synthetic CSI generator (SimulationSource)
tests/                    pytest suite + format fixtures
data/                     config.json, events.jsonl, recordings/, calibration/, exports/, models/ (gitignored)
```

## macOS setup (host software)

```bash
brew install python@3.11        # if /opt/homebrew/bin/python3.11 is missing
make setup                      # creates .venv and installs pinned requirements.txt
make test                       # runs the unit tests
```

`make setup` prefers Python 3.11 and falls back to 3.12. Python 3.14 is avoided
because binary wheels for some pinned packages may not exist yet. You need internet
only for this install step.

If you have no internet but a local interpreter already has the packages from
`requirements.txt` (numpy, scipy, pandas, pyserial, flask, pytest), build the
venv on top of it without any download:

```bash
make setup-offline                                   # default: ~/miniconda3/bin/python3.12
make setup-offline PYTHON_BOOTSTRAP=/path/to/python3 # any other interpreter
```

Optional ML extras (scikit-learn, joblib) for Random Forest training:
`make setup-ml` (pip install -r requirements-ml.txt).

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
component, which needs internet once.

## Two-board flashing procedure

Nothing in this repository flashes automatically. Flash exactly two boards,
one at a time:

1. **Unplug the third ESP32** (the one with firmware you want to keep). Never
   connect it while flashing.
2. Plug in the board that will be the **transmitter** only, then identify it
   (read-only):

   ```bash
   make ports                                       # lists ports, flags likely ESP32s
   esptool.py --port /dev/cu.usbserial-YYYY chip_id # chip type + MAC, writes nothing
   ```

   `chip_id` resets the board but writes no flash. Note the chip type
   (`esp32`, `esp32s3`, `esp32c3`, `esp32c6`).
3. Build and flash the transmitter:

   ```bash
   . ~/esp/esp-idf/export.sh
   cd firmware/transmitter
   idf.py set-target esp32                      # the chip from step 2
   idf.py -p /dev/cu.usbserial-YYYY flash monitor
   ```

   It prints a banner (target, channel, rate, MAC `1a:00:00:00:00:00`) and an
   `EG_TX_STATS` line every second. Unplug it and power it from any USB supply.
4. Plug in the **receiver** board only, identify it the same way, then:

   ```bash
   cd firmware/receiver
   idf.py set-target esp32s3                    # the chip from chip_id
   idf.py -p /dev/cu.usbmodemXXXX flash monitor
   ```

   You should see `CSI_DATA` lines and `EG_RX_STATS` once per second. Close
   the monitor with `Ctrl-]` before starting EchoGuard.

**Seeed Studio XIAO ESP32-S3** (and other boards that have only native USB): add the
USB-Serial-JTAG console overlay. Without it, no serial output appears:

```bash
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.usbjtag" set-target esp32s3
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

Guarded alternative (read-only chip check, target match, typed confirmation):

```bash
firmware/flash_helper.sh --port /dev/cu.usbmodemXXXX --role rx --target esp32s3 --usbjtag
firmware/flash_helper.sh --port /dev/cu.usbserial-YYYY --role tx --target esp32
```

Both boards must use the same channel (default 11, `idf.py menuconfig` ->
EchoGuard options). More details and all Kconfig options:
[firmware/README.md](firmware/README.md).

## CLI monitor (Milestone 1)

```bash
make monitor                      # PORT=auto; or: make monitor PORT=/dev/cu.usbmodemXXXX
.venv/bin/python -m backend.cli monitor --port auto --record data/recordings/test.csv
.venv/bin/python -m backend.cli monitor --replay tests/fixtures/classic_esp32s3_sample.txt --duration 3
make simulate SCENARIO=potential_fall DURATION=12      # SYNTHETIC data
```

Once per second the monitor prints the connection state, packet counts and
rate, malformed lines, sequence gaps, RSSI, noise floor, CSI length, mean
amplitude, a sparkline of the 52 valid LLTF subcarriers and the device's
`EG_RX_STATS`. It also prints the motion score, the activity, the data
quality and the fall-detector state. A potential-fall event is printed with every
criterion. Before any packet arrives it prints `waiting for CSI packets`
(no values are invented). `--port auto` picks the single likely ESP32 port. If
several are connected, it listens read-only on each for `CSI_DATA`. The serial
source reconnects automatically after unplugging.

## Dashboard command line

```bash
make dashboard                                       # http://127.0.0.1:8050/ opens in the browser
make dashboard ARGS="--serial /dev/cu.usbmodem2101"  # start reading the receiver right away
make dashboard ARGS="--serial auto"                  # or let it find the receiver
.venv/bin/python -m dashboard --replay data/recordings/<session_id> --speed 1
make dashboard-sim SCENARIO=desk_day                 # SYNTHETIC sitting / standing day
```

The page polls the local JSON API (`/api/posture`, `/api/posture/today`,
`/api/posture/history`, `/api/calibration/state`, `/api/status`, ...); see the
docstring of `dashboard/server.py` for every route.

### Signal processing

Per packet (`backend/processing.py`), the 52 valid LLTF subcarrier amplitudes
(C5/C6: all non-zero subcarriers) go through three steps:

1. They are divided by their mean, which removes AGC gain jumps.
2. A Hampel filter replaces impulsive outliers.
3. An EMA smooths them.

Over a 1 s window the processor then computes:

* the **variance** (mean per-subcarrier std),
* the **diff energy** (RMS of the first difference),
* the **baseline deviation** (z-score against the calibration baseline, or
  against a slow adaptive reference),
* the **motion score** `0.5 * (variance / noise_var + diff_energy / noise_diff)`.
  A calibrated empty room scores about 1.

Data quality is *insufficient* when any of these is true:

* the packet rate is below 20 Hz,
* the window is less than 70% covered,
* the CSI layout changed,
* no packet arrived for 1 s.

Settings live in `data/config.json`, which is created with defaults on first
start. The uncalibrated thresholds are low 1.5, movement 2.5 and rapid 6.0.

## Legacy fall-detection features (backend and API only)

EchoGuard started as a potential-fall detector. These parts still work from
the CLI and the JSON API, but the web app no longer shows them. Calibration
files from the timed calibration above also set the motion thresholds they
use.

### Recording and replay

In **Record**, create a session (placement and notes) or select one. Then click
one of the labels: *Empty room*, *Standing still*, *Walking*, *Sitting*,
*Lying down*, *Rapid sitting*, *Simulated fall*. You can set a fixed duration
and a pre-countdown. The engine thread writes every raw line:

```
data/recordings/<YYYYMMDD-HHMMSS-placement>/session.json
data/recordings/<session_id>/<take_index>_<label>_<start_ts>.csv
```

Take CSV columns are `host_ts_iso, host_ts_unix, session_id, take_id, label,
raw_line, rssi, id, channel, len, local_timestamp`. `session.json` holds the
sensor configuration, source kind, `synthetic` flag, notes and a takes index.
A session never mixes real and synthetic data. Recording is disabled while a
live serial source is disconnected.

Replay a whole session (takes in order) or one take from the **Replay** tab
or with `--replay`. The take label appears as ground truth. Use 1x, or "as fast
as possible" (which keeps the original timestamps), to compare with live behaviour.

### Fall heuristic (Milestone 4)

`backend/detector.py` evaluates every motion frame (10 Hz, data time) against
three explainable, configurable criteria:

1. **Sudden motion spike.** The motion score reaches the spike threshold
   (default: the active `rapid` threshold) and rose by at least
   `min_rise_fraction` x spike (default 0.5) within the previous
   `rise_window_s` (1.5 s). Sustained vigorous activity is not "sudden".
2. **Post-event stillness.** After the last spike frame, the score must drop
   to the stillness threshold (default: the `low` threshold) within
   `max_gap_s` (2 s). It must then stay at or below that threshold for
   `stillness_duration_s` (3 s).
3. **Data quality.** Every frame from the start of the rise window to the
   decision must be quality-ok (no *Unknown* frames) with at least
   `min_packet_rate_hz` (20 Hz). Any bad frame aborts the candidate.

Stillness is evaluated **only after a spike**. Low movement alone never
raises an alert, because an empty room or a person sitting still is not a fall. After a
detection a `refractory_s` (30 s) period suppresses repeats. Each event lists
all criteria with value, rule and pass/fail (`motion_spike`, `sudden_rise`,
`stillness_onset`, `stillness_level`, `stillness_duration`, `data_quality`,
`packet_rate`). The dashboard shows them in the alert card. The detector
card shows the current candidate and the last rejected candidate with its
reason.

**Alerts** (`backend/alerts.py`): an event starts an alert with a countdown
(default 30 s). The engine shows *Potential fall* until the alert is cancelled
or dismissed. If nobody cancels in time, a **simulated caregiver
notification** appears. It is only an event-log entry and a message in the UI:
nothing is sent and no network call is made. The event log keeps activity
changes (debounced 1 s), alerts, notifications, cancellations, calibrations,
recordings, exports and connection events. The newest 500 stay in memory and
every event is appended to `data/events.jsonl` (rotated to `.1` at 5 MB).

**Tuning.** Edit the *Heuristic settings* in the alert card. Changes are
saved to the `fall` / `alerts` sections of `data/config.json`. A spike or
stillness value of 0 means "derive from the motion thresholds", which are
calibrated after a calibration. Known limitations:

* A rapid sit followed by sitting still can trigger an alert.
* A real fall followed by continued movement may not trigger one.
* Noise above the stillness threshold prevents detection, so calibrate first.

Check the heuristic on your own labeled recordings:

```bash
.venv/bin/python -m backend.cli evaluate --session data/recordings/<session_id> [--calibration data/calibration/<key>.json] [--json]
make evaluate SESSION=data/recordings/<session_id>
```

It replays each take through the same processing and a fresh detector and
reports, per take, the label, packets, good-quality fraction, maximum motion
score and whether an alert fired (with criteria). It also prints a per-label
summary. This checks the heuristic on recorded examples. It is not an accuracy
estimate.

### Simulation mode (Milestone 5)

**Simulated data is synthetic. It must never be used as evidence of
real-world fall-detection accuracy.**

The *Simulation* tab (or `--simulate`, `make simulate`, `make dashboard-sim`)
offers these scenarios:

* `empty_room`
* `walking`
* `sitting` (sit down, sit, stand up)
* `rapid_movement`
* `potential_fall` (walking, a rapid burst, then lying still)
* `demo` (a scripted sequence of all of the above)

Sliders control the noise (0-0.5), motion strength (0-3), event duration
(0.2-10 s; it sets the fall-like burst, 3x for the rapid bout and 1.5x for the
sit-down transition) and packet rate (10-200 Hz; below 20 Hz the data quality is
insufficient by design).

The model has these parts:

* a static multipath profile,
* a few person-related scatterers whose path length follows the scenario,
* complex Gaussian noise,
* AGC gain jitter,
* int8 quantisation,
* 0.5% packet loss,
* an RSSI around -45 dBm.

It emits official classic `CSI_DATA` lines (len 128), so the same parser and
pipeline run. Simulated packets use the locally administered TX MAC
`02:51:4d:00:00:00`. The source kind is `simulation` with `synthetic: true` in
the dashboard snapshot, in recordings (`session.json`, takes), in
calibrations (`__synthetic` keys), in alerts and events, and in exports
(`synthetic` column, `_synthetic` file names). The current scenario phase is
shown as `... [simulated]`.

### Export

The **Export** card, `POST /api/export`, writes `data/exports/*.csv`:

* **current buffer**: up to 3000 packets with the frame features in effect,
* **recording**: a session or take, re-processed offline with the same pipeline.

Each row contains timestamps, source kind, the `synthetic` flag, session/take
and label, packet metadata, motion score, variance, diff energy, baseline
deviation, quality, activity, and `amp_lltf_<i>` amplitudes of the valid
subcarriers. Downloads are served only from `data/exports` and file names are validated.

### Optional Random Forest training

```bash
make setup-ml                                     # scikit-learn + joblib (needs internet once)
.venv/bin/python -m backend.ml.train_rf --recordings data/recordings
make train-rf ARGS="--folds 5 --trees 200"
```

`backend/ml/train_rf.py` replays every take through the live processing
pipeline. It extracts 2 s windows of quality-ok frames (mean/std/min/max of
the motion features, largest score rise, amplitude and RSSI variability)
labeled with the take label. It then evaluates a `RandomForestClassifier` with
**grouped folds by `session_id`**: whole sessions are held out
(leave-one-session-out with few sessions). Packets or windows are never split
randomly. Per-class precision/recall/F1 and the confusion matrix are computed
on held-out sessions only. A final model plus a JSON metadata file are saved
to `data/models/`.

Training is **refused** in these cases:

* fewer than 2 real sessions,
* any session is synthetic (`session.json` flag or simulator MAC), unless you
  pass `--allow-synthetic` (pipeline test only; every output and file name is
  then marked SYNTHETIC) or `--exclude-synthetic`.

Even with real data, the numbers describe only the recorded sessions, people
and placement. They are not a validated fall-detection accuracy. The model is
experimental and is not used by the dashboard. The module imports neither
flask nor pandas, so it also runs in a minimal numpy + scikit-learn Python.

## Offline operation

After installation nothing needs the internet. The firmware uses ESP-NOW
between the two boards, and the dashboard serves only local files (no CDN, no
external URLs; a strict Content-Security-Policy). The simulated notification
makes no network calls. Internet is needed only for `make setup` / `make
setup-ml` and for the first ESP-IDF build (component download).

## Optional Raspberry Pi host

The same Python app runs on a Raspberry Pi (64-bit Raspberry Pi OS, Python 3.11):

```bash
sudo apt install python3-venv python3-dev
sudo usermod -aG dialout $USER     # serial access; log out and back in
make setup && make test
make dashboard ARGS="--serial auto --no-browser"
```

The receiver appears as `/dev/ttyACM*` (native USB, e.g. XIAO ESP32-S3) or
`/dev/ttyUSB*` (CP210x/CH34x bridges). `--serial auto` finds it, or pass
`--serial /dev/ttyACM0`. To view the dashboard from another machine, use an SSH
tunnel (`ssh -L 8050:127.0.0.1:8050 pi@<host>`). Avoid `--host 0.0.0.0`,
because the dashboard has no authentication.

## Serial format

Classic ESP32 / ESP32-S3 / ESP32-C3 (25 fields):

```
type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,len,first_word,data
CSI_DATA,0,94:d9:b3:80:8c:81,-30,11,1,6,1,0,1,0,1,0,0,-93,0,13,2,2751923,0,67,0,128,1,"[67,48,4,0,...]"
```

ESP32-C5 / ESP32-C6 (15 fields):

```
type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_state,len,first_word,data
```

* `id` / `seq`: transmitter packet counter (loss / gap counting).
* `local_timestamp`: receiver clock in microseconds.
* `len`: number of integers in `data` (128 with `ECHOGUARD_RX_LLTF_ONLY`, the
  default on classic chips).
* `first_word`: 1 means subcarriers 0 and 1 are invalid.
* `data`: interleaved `[imag, real]`, decoded as `complex(raw[2k+1], raw[2k])`
  like the official parser. Classic LLTF order is 0..31, -32..-1. The 52 valid
  subcarriers are indices 1..26 and 38..63.

EchoGuard adds one line per second:

```
EG_RX_STATS,<uptime_ms>,<csi_rx_total>,<csi_rx_rate_hz>,<queue_drops>,<seq_gaps>,<last_rssi>
EG_TX_STATS,<uptime_ms>,<sent_ok>,<send_fail>,<rate_hz>
```

## Serial troubleshooting

* **Port names on macOS.** Native USB (XIAO ESP32-S3, ESP32-C3/C6/S3 USB
  port) shows up as `/dev/cu.usbmodem*`. USB-UART bridges show up as
  `/dev/cu.usbserial-*`, `/dev/cu.SLAB_USBtoUART` (CP210x) or
  `/dev/cu.wchusbserial*` (CH34x). Use `/dev/cu.*`, not `/dev/tty.*`.
  `Bluetooth-Incoming-Port` and `debug-console` are macOS system ports, never
  an ESP32.
* **No port appears.** Try another cable (many are charge-only) or another USB
  port/hub. Recent macOS includes CP210x and CH34x drivers. On older systems,
  install the vendor driver.
* **Cannot connect / download mode.** Hold BOOT, tap RESET (or plug in while
  holding BOOT), then flash. Native-USB boards may re-enumerate under a new
  name. Re-run `make ports`.
* **Port busy.** Only one program can open a port. Close `idf.py monitor`
  (`Ctrl-]`), `screen`, the Arduino serial monitor or another EchoGuard
  instance.
* **Garbage characters.** The receiver runs at **921600** baud (the default).
  The transmitter uses 115200.
* **No output on XIAO ESP32-S3.** Rebuild with `sdkconfig.defaults.usbjtag`.
* **`waiting for CSI packets` and RX stats show 0 Hz.** Make sure the
  transmitter is powered and both boards use the same channel.
* **High `queue_drops` or malformed lines.** Keep `ECHOGUARD_RX_LLTF_ONLY=y`,
  or lower `ECHOGUARD_TX_RATE_HZ`.
* **Activity stays *Unknown*.** Check the quality reasons in the dashboard.
  The packet rate must be at least 20 Hz.

## Tests

```bash
make test
```

The suite uses format fixtures and generated test signals. None of it is real
measurement data. It covers:

* the parser (official example lines, both variants, decoding order, masking, malformed lines),
* the buffer, record/replay and serial reconnect,
* port classification,
* signal processing, activity mapping and the engine,
* calibration and recording,
* the fall detector:
  * a spike followed by stillness triggers, with all criteria reported,
  * a spike without stillness, stillness without a spike, or a late stillness does not trigger,
  * quality gating,
  * the refractory period,
  * configured and calibrated thresholds are respected,
* alerts (countdown, cancel, simulated notification) and event log persistence and rotation,
* the simulator:
  * its lines parse with the official parser,
  * it is marked synthetic,
  * end to end through the engine, `potential_fall` raises an alert and the other scenarios do not,
* RF tooling:
  * session-grouped splits never mix sessions,
  * training is refused for synthetic or single-session data,
  * a tiny training run (skipped without scikit-learn),
* the `evaluate` CLI,
* all dashboard API routes, including bad input and path traversal.

## Verification status

Verified in the development environment (macOS, offline, Python 3.12 venv):

* `make test` passes (scikit-learn tests are skipped in that venv because it
  has no scikit-learn).
* The dashboard server ran on 127.0.0.1. A `potential_fall` simulation
  raised an active alert with all criteria passed and a countdown, and
  `POST /api/alerts/cancel` set it to cancelled. Buffer export and download
  worked.
* `python -m backend.cli monitor --simulate potential_fall --duration 10`
  printed frames and a potential-fall event with its criteria.
* Random Forest training ran with `--allow-synthetic` on generated sessions,
  in a separate local Python that has scikit-learn 1.7.1. The synthetic-only
  refusal was confirmed there too.
* The JavaScript passes a syntax check (`node --check`), and `dashboard/static`
  contains no external URLs.

**Not verified:**

* firmware compilation (ESP-IDF not installed),
* flashing, and any real ESP32 or CSI capture (no board connected),
* live serial streaming,
* behaviour with people moving between real boards,
* threshold and heuristic values on real CSI,
* a real-room calibration,
* any classifier on real recordings,
* visual rendering in a real browser (only the API and JS syntax were checked).

The simulator's behaviour says nothing about real-world accuracy.
