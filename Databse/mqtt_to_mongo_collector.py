#!/usr/bin/env python3
"""mqtt_to_mongo_collector.py

MQTT -> MongoDB Atlas collector for STF factory topics.

What it does:
- Subscribes to MQTT topics (default: stf/#)
- Stores every JSON status message into a time-series collection `telemetry_status`
- Detects state changes per station and stores transition events into `state_transitions`
- (Optional) propagates `cycle_id` if your payload already provides one

Environment variables
---------------------
Required:
  MONGO_URI          MongoDB Atlas connection string

Optional (MQTT):
  MQTT_HOST          default: localhost
  MQTT_PORT          default: 1883
  MQTT_TOPIC         default: stf/#
  MQTT_USERNAME      default: (none)
  MQTT_PASSWORD      default: (none)

Optional (Mongo):
  MONGO_DB           default: MDA
  TS_RETENTION_DAYS  default: 14   (telemetry retention)

Install deps:
  pip install "paho-mqtt>=1.6" "pymongo>=4.6"

Run:
  export MONGO_URI='mongodb+srv://USER:PASS@cluster.../MDA?retryWrites=true&w=majority'
  python3 mqtt_to_mongo_collector.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import paho.mqtt.client as mqtt
from pymongo import MongoClient
from pymongo.errors import CollectionInvalid, OperationFailure

from dotenv import load_dotenv
load_dotenv()

# Config

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "stf/#")
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "MDA")
TS_RETENTION_DAYS = int(os.getenv("TS_RETENTION_DAYS", "14"))

# Collections
TELEMETRY_COL = "telemetry_status"          # time-series
TRANSITIONS_COL = "state_transitions"       # normal
CYCLES_COL = "cycles"                       # optional / future


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts_from_payload(payload: Dict[str, Any]) -> datetime:
    """Return a timezone-aware UTC datetime.

    Accepts:
    - payload['ts'] as unix seconds (float/int)
    - payload['ts'] as ISO string
    - otherwise: now()
    """
    ts = payload.get("ts")
    if ts is None:
        return utc_now()

    # unix epoch seconds
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except Exception:
            return utc_now()

    # ISO string
    if isinstance(ts, str):
        try:
            # handle trailing Z
            s = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return utc_now()

    return utc_now()


def infer_station_from_topic(topic: str) -> str:
    # expected like: stf/<station>/status
    parts = topic.split("/")
    if len(parts) >= 2 and parts[0] == "stf":
        return parts[1]
    return "unknown"


def extract_state(payload: Dict[str, Any]) -> Optional[str]:
    # common key: 'state'
    state = payload.get("state")
    if isinstance(state, str) and state.strip():
        return state.strip()
    return None


def safe_json_loads(b: bytes) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        s = b.decode("utf-8", errors="replace")
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj, None
        return None, "JSON is not an object"
    except Exception as e:
        return None, str(e)


def ensure_collections(db) -> None:
    """Create collections + indexes if they don't exist.

    - telemetry_status: time-series with retention
    - state_transitions: indexes for fast state-wise queries
    """
    # Create telemetry time-series collection if missing
    try:
        existing = set(db.list_collection_names())
    except Exception as e:
        print(f" Mongo error listing collections: {e}")
        raise

    if TELEMETRY_COL not in existing:
        try:
            db.create_collection(
                TELEMETRY_COL,
                timeseries={
                    "timeField": "ts",
                    "metaField": "meta",
                    "granularity": "seconds",
                },
                expireAfterSeconds=int(TS_RETENTION_DAYS * 24 * 3600),
            )
            print(f" Created time-series collection: {TELEMETRY_COL} (retention {TS_RETENTION_DAYS}d)")
        except CollectionInvalid:
            pass
        except OperationFailure as e:
            # Some Atlas tiers/permissions can restrict createCollection options.
            print(f" Could not create time-series collection automatically: {e}")
            print("   -> I will continue assuming the collection already exists or will be created manually.")

    # Ensure indexes for transitions
    transitions = db[TRANSITIONS_COL]
    try:
        transitions.create_index([("station", 1), ("ts", 1)])
        transitions.create_index([("cycle_id", 1), ("ts", 1)])
        transitions.create_index([("to_state", 1), ("ts", 1)])
    except Exception as e:
        print(f" Could not ensure indexes on {TRANSITIONS_COL}: {e}")


class Collector:
    def __init__(self, mongo_client: MongoClient):
        self.client = mongo_client
        self.db = self.client[MONGO_DB]
        ensure_collections(self.db)

        self.telemetry = self.db[TELEMETRY_COL]
        self.transitions = self.db[TRANSITIONS_COL]
        self.cycles = self.db[CYCLES_COL]

        # state memory: station -> (state, ts)
        self.last_state: Dict[str, Tuple[str, datetime]] = {}

    def handle_message(self, topic: str, payload: Dict[str, Any]) -> None:
        station = infer_station_from_topic(topic)
        ts = parse_ts_from_payload(payload)
        state = extract_state(payload)

        # 1) Raw telemetry write
        telemetry_doc = {
            "ts": ts,
            "meta": {
                "station": station,
                "topic": topic,
            },
            "station": station,
            "topic": topic,
            "state": state,
            "payload": payload,
        }

        try:
            self.telemetry.insert_one(telemetry_doc)
        except Exception as e:
            print(f" telemetry insert failed: {e}")

        # 2) Transition write (only on change)
        if state:
            prev = self.last_state.get(station)
            if (prev is None) or (prev[0] != state):
                cycle_id = payload.get("cycle_id")
                if not isinstance(cycle_id, str):
                    cycle_id = None

                transition_doc = {
                    "ts": ts,
                    "station": station,
                    "topic": topic,
                    "from_state": prev[0] if prev else None,
                    "to_state": state,
                    "cycle_id": cycle_id,
                    "payload": {
                        # keep small but useful fields
                        "fault": payload.get("fault") or payload.get("fault_code"),
                        "ready_for_pickup": payload.get("ready_for_pickup"),
                        "part_present": payload.get("part_present"),
                    },
                }
                try:
                    self.transitions.insert_one(transition_doc)
                except Exception as e:
                    print(f" transition insert failed: {e}")

                self.last_state[station] = (state, ts)


def build_mqtt_client(collector: Collector) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    if MQTT_USERNAME is not None:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD or "")

    def on_connect(c: mqtt.Client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            print(f" MQTT connected to {MQTT_HOST}:{MQTT_PORT}")
            print(f" Subscribing to: {MQTT_TOPIC}")
            c.subscribe(MQTT_TOPIC)
        else:
            print(f" MQTT connect failed rc={reason_code}")

    def on_message(c: mqtt.Client, userdata, msg: mqtt.MQTTMessage):
        payload, err = safe_json_loads(msg.payload)
        if payload is None:
            print(f" Skipping non-JSON message on {msg.topic}: {err}")
            return

        try:
            collector.handle_message(msg.topic, payload)
        except Exception as e:
            print(f" Error handling message on {msg.topic}: {e}")

    client.on_connect = on_connect
    client.on_message = on_message
    return client


def main() -> int:
    if not MONGO_URI:
        print(" Missing MONGO_URI env var")
        return 2

    # Mongo
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
        mongo_client.admin.command("ping")
        print(" MongoDB connected")
    except Exception as e:
        print(f" MongoDB connection failed: {e}")
        return 3

    collector = Collector(mongo_client)

    # MQTT
    client = build_mqtt_client(collector)
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        print(f" MQTT connection failed: {e}")
        return 4

    print(" Collector running. Press Ctrl+C to stop.")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n Stopped")
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            mongo_client.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
