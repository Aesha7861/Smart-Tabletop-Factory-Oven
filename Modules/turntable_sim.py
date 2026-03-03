import json
import time
from enum import Enum
import paho.mqtt.client as mqtt

#  CONFIG 
MQTT_HOST = "localhost"
MQTT_PORT = 1883

#  TOPICS (Standard) 
TOPIC_STATUS = "stf/turntable/status"
TOPIC_CMD_START = "stf/turntable/cmd/start"
TOPIC_CMD_STOP = "stf/turntable/cmd/stop"
TOPIC_CMD_RESET = "stf/turntable/cmd/reset"

#  TOPICS (Integration with Teammates) 
# Listen for Vacuum Robot Drop Event
TOPIC_VACUUM_DROPPED = "stf/vacuum/evt/dropped" 
# Send Trigger to Conveyor Belt
TOPIC_CONVEYOR_START = "stf/conveyor/cmd/start"

class State(str, Enum):
    IDLE = "IDLE"           # Waiting at Vacuum Position (I5)
    ROTATING = "ROTATING"   # Motor M1 Moving
    SAWING = "SAWING"       # At Saw Position (I14), Saw Motor M3 ON
    EJECTING = "EJECTING"   # At Belt Position (I6), Valve Q12 ON
    FAULT = "FAULT"

class TurntableSim:
    def __init__(self):
        self.state = State.IDLE
        self.position = 0.0   # 0 to 100
        self.target_pos = 0.0
        self.speed = 20.0     # Speed of rotation (units per sec)

        # Sensor Simulation
        self.sens_vacuum = True  # I5
        self.sens_saw = False    # I14
        self.sens_belt = False   # I6

        self._cmd_start = False
        self._cmd_reset = False
        self.timer = 0.0

        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def on_connect(self, client, userdata, flags, rc):
        print(f" MQTT Connected to {MQTT_HOST}")
        client.subscribe("stf/turntable/cmd/#")
        # Also listen to the Vacuum robot
        client.subscribe(TOPIC_VACUUM_DROPPED)

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode()
        
        # Start if Manual Button OR Vacuum Robot signals drop
        if topic == TOPIC_CMD_START or topic == TOPIC_VACUUM_DROPPED:
            self._cmd_start = True
            print(f" START SIGNAL RECEIVED via {topic}")
            
        elif topic == TOPIC_CMD_RESET:
            self._cmd_reset = True
            print(" RESET SIGNAL RECEIVED")

    def update_sensors(self):
        # Sensors trigger when position is close to target (Tolerance +/- 2)
        self.sens_vacuum = abs(self.position - 0) < 2.0
        self.sens_saw = abs(self.position - 50) < 2.0
        self.sens_belt = abs(self.position - 100) < 2.0

    def tick(self, dt):
        # 1. RESET Logic
        if self._cmd_reset:
            self._cmd_reset = False
            self.state = State.IDLE
            self.position = 0
            return

        # 2. STATE MACHINE
        if self.state == State.IDLE:
            if self._cmd_start:
                self._cmd_start = False
                print(" PART RECEIVED. Rotating to Saw...")
                self.target_pos = 50
                self.state = State.ROTATING

        elif self.state == State.ROTATING:
            # Move position
            if self.position < self.target_pos:
                self.position += self.speed * dt
                if self.position >= self.target_pos:
                    self.position = self.target_pos
            
            # Check Arrival
            if self.position == self.target_pos:
                self.update_sensors()
                if self.sens_saw and self.state != State.SAWING:
                    print(" AT SAW (I14). Sawing for 3s...")
                    self.state = State.SAWING
                    self.timer = 3.0
                elif self.sens_belt and self.state != State.EJECTING:
                    print(" AT BELT (I6). Ejecting for 2s...")
                    self.state = State.EJECTING
                    self.timer = 2.0
                elif self.sens_vacuum and self.state != State.IDLE:
                    self.state = State.IDLE
                    print(" RETURNED TO START (I5). Waiting...")

        elif self.state == State.SAWING:
            self.timer -= dt
            if self.timer <= 0:
                print(" SAW DONE. Rotating to Belt...")
                self.target_pos = 100
                self.state = State.ROTATING

        elif self.state == State.EJECTING:
            self.timer -= dt
            if self.timer <= 0:
                print(" EJECTION DONE. Signalling Conveyor & Returning...")
                
                #  INTEGRATION STEP 
                self.client.publish(TOPIC_CONVEYOR_START, "1")
                print(f" SENT: {TOPIC_CONVEYOR_START}")

                self.target_pos = 0 
                self.position = 0   
                self.state = State.IDLE

        self.update_sensors()

    def publish(self):
        payload = {
            "state": self.state.value,
            "position_raw": round(self.position, 1),
            "sensors": {
                "vacuum_pos": self.sens_vacuum,
                "saw_pos": self.sens_saw,
                "belt_pos": self.sens_belt
            }
        }
        self.client.publish(TOPIC_STATUS, json.dumps(payload))

    def run(self):
        try:
            self.client.connect(MQTT_HOST, MQTT_PORT)
            self.client.loop_start()
            print(" Turntable Simulator RUNNING...")
            
            while True:
                self.tick(0.1)     # Physics tick
                self.publish()     # MQTT Update
                time.sleep(0.1)    # 10Hz Refresh
                
        except KeyboardInterrupt:
            print("\n Stopping...")
            self.client.loop_stop()

if __name__ == "__main__":
    TurntableSim().run()