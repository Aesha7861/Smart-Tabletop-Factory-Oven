# STF-OVEN (STF Oven Station)

This repo contains the control + simulation code for the **STF (Fischertechnik) oven station**, split into four subsystems:

- **Oven** (door + feeder + baking timer)
- **Vacuum gripper** (pick from oven, place on turntable)
- **Turntable** (rotate through stations)
- **Conveyor belt** (transport cookie to next station)

All subsystems communicate via **MQTT** (typically Mosquitto). Node-RED flows (JSON) are included for dashboards and basic control.

---

## Repository contents

### Main entry points

- **`main.py`** — *Unified controller* (Oven → Gripper → Turntable → Conveyor) intended for **RevPi / RevPiModIO** hardware.
  - Uses `revpimodio2` I/O directly.
  - Publishes station status to MQTT.
  - Supports basic MQTT commands (startup/shutdown/reset).

- **`stf_system_combined.py`** — *End-to-end software simulation* (no RevPi required).
  - Runs oven + vacuum + turntable + conveyor simulators in one process.
  - Uses MQTT topics so Node-RED dashboards can still be used.

### Subsystem modules (standalone)

Hardware-oriented (RevPi required):
- `oven_sim.py` (despite the name: uses RevPi I/O)
- `vgr_sim.py` (uses RevPi I/O)
- `turntable_hw.py`
- `conveyor_hw.py`

Software simulators (no RevPi required):
- `turntable_sim.py`
- `conveyor_sim.py`

### Node-RED flows

Import these JSON files into Node-RED:
- `oven.json`
- `vacume_gripper.json`
- `turntable.json`
- `conveyor.json`

- node-red-combined.json — All-in-one import that bundles Oven + Vacuum Gripper + Turntable + Conveyor dashboards/flows into a single Node-RED flow file.

⚠️ **Reality check:** the dashboards are wired to the *component-level topics* (e.g. `stf/oven/status`, `stf/vgr/status` …). They **do not** control `main.py` out-of-the-box because `main.py` uses `stf/oven_station/...` topics.

###  telemetry logging
- `mqtt_to_mongo_collector.py` — subscribes to `stf/#` and stores JSON messages into MongoDB (Atlas) with basic state-transition tracking.

---

## Requirements

### Common
- Python 3
- MQTT broker (Mosquitto recommended)

### Hardware mode (`main.py`, `*_hw.py`, some `*_sim.py`)
- RevPi / RevPiModIO + `revpimodio2`

### Node-RED dashboards 
- Node-RED
- Dashboard nodes (you must install the dashboard package you're using)

### Python dependencies

Minimum for MQTT-only simulation:
```bash
pip install paho-mqtt
```

For RevPi control:
```bash
pip install revpimodio2 paho-mqtt
```

For MongoDB collector:
```bash
pip install "paho-mqtt>=1.6" "pymongo>=4.6" python-dotenv
```

---

## Quick start

### 1) Start MQTT broker

On Linux (Mosquitto):
```bash
sudo systemctl enable --now mosquitto
```

### 2) Choose a run mode

#### A) Hardware station controller (RevPi) — `main.py`

Run on the RevPi / controller:
```bash
python3 main.py
```

`main.py` auto-starts the first cycle at launch. You can also control it via MQTT:
```bash
# Start a cycle
mosquitto_pub -h localhost -t stf/oven_station/cmd/startup -m 1

# Stop (emergency stop style: all outputs off)
mosquitto_pub -h localhost -t stf/oven_station/cmd/shutdown -m 1

# Reset to IDLE
mosquitto_pub -h localhost -t stf/oven_station/cmd/reset -m 1
```

Status topic:
- `stf/oven_station/status` (JSON)

#### B) Full software simulation (no RevPi) — `stf_system_combined.py`

Run anywhere with Python + MQTT:
```bash
python3 stf_system_combined.py
```

Start the simulated oven cycle:
```bash
mosquitto_pub -h localhost -t stf/oven/cmd/start -m 1
```

This triggers the chain:
Oven → Vacuum "drop" event → Turntable → Conveyor.

---

## Node-RED dashboards

1. Start Node-RED.
2. Import one (or all) JSON flow files from this repo.
3. Deploy.

Dashboard URLs depend on what you installed:
- Classic dashboard (`node-red-dashboard`): `http://<host>:1880/ui`
- Dashboard 2.0 (`@flowfuse/node-red-dashboard`): `http://<host>:1880/dashboard`

If you get **`Cannot GET /ui`**, you either:
- didn't install the classic dashboard nodes, or
- you installed Dashboard 2.0 (different URL).

---

## MQTT topic map (what exists in this repo)

### Unified station (`main.py`)
- Status: `stf/oven_station/status`
- Commands:
  - `stf/oven_station/cmd/startup`
  - `stf/oven_station/cmd/shutdown`
  - `stf/oven_station/cmd/reset`

### Oven (used by `stf_system_combined.py` + `oven.json`)
- Status: `stf/oven/status`
- Command:
  - `stf/oven/cmd/start`

### Vacuum / gripper (used by `stf_system_combined.py`)
- Status: `stf/vgr/status`
- Event:
  - `stf/vacuum/evt/dropped` (published when cookie is dropped on turntable)

### Turntable simulator (`turntable_sim.py`)
- Status: `stf/turntable/status`
- Commands:
  - `stf/turntable/cmd/start`
  - `stf/turntable/cmd/reset`
- Integration:
  - listens: `stf/vacuum/evt/dropped`
  - publishes: `stf/conveyor/cmd/start`

### Conveyor simulator (`conveyor_sim.py`)
- Status: `stf/conveyor/status`
- Commands:
  - `stf/conveyor/cmd/start`
  - `stf/conveyor/cmd/stop`
  - `stf/conveyor/cmd/reset`
  - `stf/conveyor/cmd/inject_piece`

---


## Configuration notes (important)

### RevPi I/O mapping
- `main.py` hardcodes RevPi I/O names at the top (e.g. `I_6`, `O_5`, …). If your wiring differs, **edit those constants**.
- `main.py` uses **timers** for some motions (turntable rotation / gripper moves). If you want sensor-based stopping, you'll need to extend it (or use the dedicated `*_hw.py` modules).

### Known inconsistencies
- The repo contains multiple generations of scripts. Some are demo-quality.


---

## Telemetry to MongoDB (optional)

```bash
export MONGO_URI='mongodb+srv://USER:PASS@cluster.../MDA?retryWrites=true&w=majority'
python3 mqtt_to_mongo_collector.py
```

Defaults:
- subscribes to `stf/#`
- database: `MDA`
- retention: 14 days (time-series collection)

---

## Dataset integrity (telemetry in MongoDB)

The **dataset** in this project is the MQTT telemetry you optionally persist via `mqtt_to_mongo_collector.py`. If you plan to do analysis, reports, or ML on it: treat integrity as a first-class engineering problem, not an afterthought.

### What gets stored

The collector subscribes to `stf/#` and writes two collections:

- **`telemetry_status` (time-series)**
  - Stores every MQTT message that is a **JSON object** (a JSON "dictionary").
  - Adds metadata (`station`, `topic`) and stores the original `payload` verbatim.
  - Timestamp logic: uses `payload.ts` if present (unix seconds or ISO-8601); otherwise it uses the collector's current UTC time.

- **`state_transitions` (events)**
  - Derived from **changes in `payload.state` per station** (e.g. `IDLE → BAKING`).
  - Includes `from_state`, `to_state`, and `cycle_id` **only if** your payload already provides a string `cycle_id`.

### Hard limits of the current pipeline

This is the stuff that silently breaks datasets:

- **Message loss is possible**: most publishers in this repo use MQTT **QoS 0** (best-effort). Under load or network hiccups, you can lose messages.
- **No deduplication**: if the same status is published multiple times, it will be stored multiple times.
- **Ordering is not guaranteed**: delivery can be out-of-order; the collector writes messages as received.
- **Non-JSON messages are skipped**: the collector intentionally ignores anything that is not a JSON object. Example: in `stf_system_combined.py`, `stf/vacuum/evt/dropped` publishes the string `"1"` (not JSON), so it **does not enter the dataset** unless you change it to JSON.
- **Restarts reset counters**: `main.py` publishes `cycle_count`, but it resets when the script restarts, so it is **not a globally-unique cycle identifier**.

### Payload contract (recommended)

If you want a dataset that survives restarts and supports clean analysis, publish JSON objects that follow a minimal contract:

- `ts`: timestamp (unix seconds or ISO-8601 with timezone)
- `state`: a stable string state name (for status topics)
- `cycle_id`: a UUID (same ID across oven → gripper → turntable → conveyor for one cookie)
- Optional but useful:
  - `run_id`: new ID on every program start (helps separate runs)
  - `seq`: monotonic counter per station (helps detect drops/duplicates/out-of-order)
  - `source`: `"hw"` or `"sim"` (avoid mixing modes accidentally)

### Practical integrity checks

These are simple checks that catch most problems early:

- **Timestamp sanity**: per station, `ts` should be roughly monotonic. Large backward jumps usually mean clock drift.
- **Gap detection**: for periodic status (e.g. `main.py` publishes every **0.5 s**), flag gaps greater than ~3× the period as likely loss/outage.
- **State validity**: enforce an allowed set of states per station and flag unknown strings (typos become "new states" if you don't police them).
- **Cycle completeness** (if you add `cycle_id`): each cycle should show an expected sequence of transitions across stations; missing stations usually means topic mismatch or message loss.

### Retention, reproducibility, and backups

- The collector *tries* to create `telemetry_status` as a MongoDB **time-series** collection with TTL based on `TS_RETENTION_DAYS` (default **14 days**). On Atlas, permissions/tier can block auto-creation, so verify your collection settings.
- If you need long-term data, increase `TS_RETENTION_DAYS` or export periodically (e.g., `mongoexport`).

---

## Troubleshooting

- **Nothing moves on hardware**
  - Check `revpimodio2` can see the I/O and that your `I_*/O_*` names match the RevPi configuration.
  - Verify outputs are wired to actuators and that pneumatics are enabled.

- **Dashboards show data but buttons don't do anything**
  - You're probably running `main.py` but using dashboards that publish to `stf/oven/...` topics.
  - Fix: either use `mosquitto_pub` on `stf/oven_station/...`, or edit the Node-RED MQTT-out topics.

- **Stuck states / scripts run forever**
  - These are control loops. Stop with `Ctrl+C`.

---

## Team / contributors

- Rinkal zadafiya
- vrushabh vasoya
- Umang Dholakiya
- Aesha Gadhiya

---

## License

Add a `LICENSE` file if you want explicit usage rights. For a university/lab project with restricted distribution, state that here.