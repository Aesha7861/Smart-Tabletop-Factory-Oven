import json
import time
import threading
from dataclasses import dataclass
from enum import Enum
import paho.mqtt.client as mqtt
from datetime import datetime

#  CONFIG 
MQTT_HOST = "localhost"
MQTT_PORT = 1883

# 1. NEW OVEN SIMULATOR (From stf_oven_sim.py)
TOPIC_OVEN_STATUS = "stf/oven/status"
TOPIC_OVEN_START  = "stf/oven/cmd/start"

class OvenState(str, Enum):
    IDLE = "IDLE"
    LOADING = "LOADING"
    BAKING = "BAKING"
    UNLOADING = "UNLOADING"
    FAULT = "FAULT"
    STOPPED = "STOPPED"

@dataclass
class OvenModel:
    ambient: float = 25.0
    temp_current: float = 25.0
    temp_setpoint: float = 180.0
    heat_rate: float = 0.08
    cool_rate: float = 0.03

class OvenSim:
    def __init__(self, client):
        self.client = client
        self.state = OvenState.IDLE
        self.model = OvenModel()
        self.bake_time_s = 5  # Short for demo
        self.bake_remaining_s = 0
        self.part_present = False
        self.cycle_count = 0
        self._cmd_start = False
        
        # Integration Flag
        self.cycle_completed_trigger = False

    def handle_msg(self, topic, payload):
        if topic == TOPIC_OVEN_START:
            self._cmd_start = True
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [OVEN]  START CMD")

    def update_temperature(self, dt):
        T = self.model.temp_current
        target = self.model.temp_setpoint if self.state == OvenState.BAKING else self.model.ambient
        rate = self.model.heat_rate if self.state == OvenState.BAKING else self.model.cool_rate
        self.model.temp_current += (target - T) * rate

    def tick(self, dt):
        self.update_temperature(dt)

        if self.state == OvenState.IDLE:
            if self._cmd_start:
                self._cmd_start = False
                self.state = OvenState.LOADING
                self.bake_remaining_s = 2.0 # Loading time
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [OVEN]  Heating up...")

        elif self.state == OvenState.LOADING:
            self.bake_remaining_s -= dt
            if self.bake_remaining_s <= 0:
                self.state = OvenState.BAKING
                self.bake_remaining_s = self.bake_time_s
                self.part_present = True

        elif self.state == OvenState.BAKING:
            self.bake_remaining_s -= dt
            if self.bake_remaining_s <= 0:
                self.state = OvenState.UNLOADING
                self.bake_remaining_s = 2.0 # Unloading time
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [OVEN]  Baking Done.")

        elif self.state == OvenState.UNLOADING:
            self.bake_remaining_s -= dt
            if self.bake_remaining_s <= 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [OVEN]  Unload Done. Triggering Vacuum...")
                self.state = OvenState.IDLE
                self.part_present = False
                self.cycle_completed_trigger = True # TRIGGER

        # Publish
        payload = {
            "state": self.state.value,
            "temp_current": round(self.model.temp_current, 1),
            "temp_setpoint": self.model.temp_setpoint,
            "bake_remaining_s": int(self.bake_remaining_s) if self.state in [OvenState.BAKING, OvenState.LOADING, OvenState.UNLOADING] else 0,
            "part_present": self.part_present
        }
        self.client.publish(TOPIC_OVEN_STATUS, json.dumps(payload))


# 2. NEW VACUUM SIMULATOR (Adapted from stf_vgr_hw.py)
# I have removed the 'revpimodio' dependency so this runs on your PC
class VGRState(str, Enum):
    IDLE = "IDLE"
    MOVING_TO_OVEN = "MOVING_TO_OVEN"
    AT_OVEN = "AT_OVEN"
    LOWER_PICK = "LOWER_PICK"
    VAC_ON = "VAC_ON"
    RAISE_PICK = "RAISE_PICK"
    MOVING_TO_TABLE = "MOVING_TO_TABLE"
    AT_TABLE = "AT_TABLE"
    LOWER_PLACE = "LOWER_PLACE"
    VAC_OFF = "VAC_OFF"
    RAISE_PLACE = "RAISE_PLACE"

class VacuumSim:
    def __init__(self, client):
        self.client = client
        self.state = VGRState.IDLE
        self.pos = 0.0  # 0=Oven, 100=Table
        self.timer = 0.0
        self.vacuum_on = False
        self.part_gripped = False
        self.auto_start = False

    def trigger_sequence(self):
        if self.state == VGRState.IDLE:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [VACUUM]  Auto-Sequence Started.")
            self.state = VGRState.MOVING_TO_OVEN

    def tick(self, dt):
        # 1. Move to Oven
        if self.state == VGRState.MOVING_TO_OVEN:
            self.pos -= 30 * dt
            if self.pos <= 0:
                self.pos = 0
                self.state = VGRState.AT_OVEN
                self.timer = 0.5
        
        elif self.state == VGRState.AT_OVEN:
            self.timer -= dt
            if self.timer <= 0:
                self.state = VGRState.LOWER_PICK
                self.timer = 1.0

        # 2. Pick Sequence
        elif self.state == VGRState.LOWER_PICK:
            self.timer -= dt
            if self.timer <= 0:
                self.vacuum_on = True
                self.state = VGRState.VAC_ON
                self.timer = 1.0

        elif self.state == VGRState.VAC_ON:
            self.timer -= dt
            if self.timer <= 0:
                self.part_gripped = True
                self.state = VGRState.RAISE_PICK
                self.timer = 1.0

        elif self.state == VGRState.RAISE_PICK:
            self.timer -= dt
            if self.timer <= 0:
                self.state = VGRState.MOVING_TO_TABLE

        # 3. Move to Table
        elif self.state == VGRState.MOVING_TO_TABLE:
            self.pos += 30 * dt
            if self.pos >= 100:
                self.pos = 100
                self.state = VGRState.AT_TABLE
                self.timer = 0.5

        elif self.state == VGRState.AT_TABLE:
            self.timer -= dt
            if self.timer <= 0:
                self.state = VGRState.LOWER_PLACE
                self.timer = 1.0

        # 4. Place Sequence
        elif self.state == VGRState.LOWER_PLACE:
            self.timer -= dt
            if self.timer <= 0:
                self.vacuum_on = False
                self.state = VGRState.VAC_OFF
                self.timer = 1.0

        elif self.state == VGRState.VAC_OFF:
            self.timer -= dt
            if self.timer <= 0:
                self.part_gripped = False
                # HANDSHAKE TO TURNTABLE
                self.client.publish("stf/vacuum/evt/dropped", "1")
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [VACUUM]  Dropped. Signal sent to Turntable.")
                self.state = VGRState.RAISE_PLACE
                self.timer = 1.0

        elif self.state == VGRState.RAISE_PLACE:
            self.timer -= dt
            if self.timer <= 0:
                self.state = VGRState.IDLE
                self.pos = 50 # Return to idle position

        # Publish
        payload = {
            "state": self.state.value,
            "position": "OVEN" if self.pos < 10 else "TABLE" if self.pos > 90 else "MOVING",
            "vacuum_on": self.vacuum_on,
            "part_gripped": self.part_gripped,
            "fault": "OK"
        }
        self.client.publish("stf/vgr/status", json.dumps(payload))


# 3. TURNTABLE SIMULATOR (From stf_turntable_sim.py)
class TTState(str, Enum):
    IDLE = "IDLE"
    ROTATING = "ROTATING"
    SAWING = "SAWING"
    EJECTING = "EJECTING"

class TurntableSim:
    def __init__(self, client):
        self.client = client
        self.state = TTState.IDLE
        self.position = 0.0
        self.target_pos = 0.0
        self.timer = 0.0
        self._cmd_start = False

    def handle_msg(self, topic, payload):
        # Auto-start from Vacuum Drop
        if topic == "stf/vacuum/evt/dropped" or topic == "stf/turntable/cmd/start":
            self._cmd_start = True
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [TURNTABLE]  Start Signal Received")

    def tick(self, dt):
        if self.state == TTState.IDLE:
            if self._cmd_start:
                self._cmd_start = False
                self.state = TTState.ROTATING
                self.target_pos = 50
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [TURNTABLE]  Rotating to Saw...")

        elif self.state == TTState.ROTATING:
            if self.position < self.target_pos:
                self.position += 20 * dt
                if self.position >= self.target_pos: self.position = self.target_pos
            
            if self.position == self.target_pos:
                if self.position == 50:
                    self.state = TTState.SAWING
                    self.timer = 3.0
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [TURNTABLE]  Sawing...")
                elif self.position == 100:
                    self.state = TTState.EJECTING
                    self.timer = 2.0
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [TURNTABLE]  Ejecting...")
                elif self.position == 0:
                    self.state = TTState.IDLE

        elif self.state == TTState.SAWING:
            self.timer -= dt
            if self.timer <= 0:
                self.state = TTState.ROTATING
                self.target_pos = 100

        elif self.state == TTState.EJECTING:
            self.timer -= dt
            if self.timer <= 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [TURNTABLE]  Ejection Done. Triggering Conveyor.")
                # HANDSHAKE TO CONVEYOR
                self.client.publish("stf/conveyor/cmd/start", "1")
                self.target_pos = 0
                self.position = 0
                self.state = TTState.IDLE

        # Publish
        payload = {
            "state": self.state.value,
            "position_raw": round(self.position, 1),
            "sensors": {"vacuum_pos": abs(self.position-0)<2, "saw_pos": abs(self.position-50)<2, "belt_pos": abs(self.position-100)<2}
        }
        self.client.publish("stf/turntable/status", json.dumps(payload))


# 4. CONVEYOR SIMULATOR (From conveyor_sim.py)
class ConveyorSim:
    def __init__(self, client):
        self.client = client
        self.state = "IDLE"
        self.motor = 0
        self.piece_present = False
        self.timer = 0.0

    def handle_msg(self, topic, payload):
        if topic == "stf/conveyor/cmd/start":
            if self.state == "IDLE":
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [CONVEYOR]  Start Received. Motor ON.")
                self.state = "RUNNING"
                self.motor = 1
                self.piece_present = True
                self.timer = 3.0
        elif topic == "stf/conveyor/cmd/reset":
            self.state = "IDLE"
            self.motor = 0
            self.piece_present = False

    def tick(self, dt):
        if self.state == "RUNNING":
            self.timer -= dt
            if self.timer <= 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [CONVEYOR] End Sensor Reached. Stopping.")
                self.state = "IDLE"
                self.motor = 0

        # Publish
        payload = {"state": self.state, "motor": self.motor, "piece_present": self.piece_present}
        self.client.publish("stf/conveyor/status", json.dumps(payload))


# MAIN SYSTEM ORCHESTRATOR
def run_full_system():
    client = mqtt.Client()
    
    # Initialize Stations
    oven = OvenSim(client)
    vac = VacuumSim(client)
    turn = TurntableSim(client)
    conv = ConveyorSim(client)

    def on_connect(c, userdata, flags, rc):
        print(" MQTT Connected.")
        c.subscribe("stf/oven/cmd/#")
        c.subscribe("stf/turntable/cmd/#")
        c.subscribe("stf/conveyor/cmd/#")
        c.subscribe("stf/vacuum/evt/dropped")

    def on_message(c, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode()
        
        # Route messages to appropriate stations
        if "oven" in topic: oven.handle_msg(topic, payload)
        if "turntable" in topic or "vacuum" in topic: turn.handle_msg(topic, payload)
        if "conveyor" in topic: conv.handle_msg(topic, payload)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()

    print(" FULL INTEGRATED FACTORY RUNNING")
    print(" Go to OVEN Dashboard and click START to begin the sequence.")

    try:
        while True:
            dt = 0.1
            
            # 1. Update all physics/logic
            oven.tick(dt)
            vac.tick(dt)
            turn.tick(dt)
            conv.tick(dt)

            # 2. MASTER INTEGRATION LOGIC (The "Glue")
            # If Oven finishes, trigger Vacuum
            if oven.cycle_completed_trigger:
                oven.cycle_completed_trigger = False
                vac.trigger_sequence()

            time.sleep(dt)

    except KeyboardInterrupt:
        print("\ Stopping...")
        client.loop_stop()

if __name__ == "__main__":
    run_full_system()