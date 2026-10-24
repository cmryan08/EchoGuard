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

