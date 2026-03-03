import time
import json
import threading
import paho.mqtt.client as mqtt
from datetime import datetime

# MQTT CONFIG
BROKER = "localhost"
STATUS_TOPIC = "stf/conveyor/status"

CMD_INJECT = "stf/conveyor/cmd/inject_piece"
CMD_RESET  = "stf/conveyor/cmd/reset"
CMD_START  = "stf/conveyor/cmd/start"
CMD_STOP   = "stf/conveyor/cmd/stop"

# TIMING (seconds) 

# STATE VARIABLES
state = "IDLE"
motor = 0
I3 = 0
piece = False

lock = threading.Lock()

# UTILITY: print with timestamp
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# MQTT STATUS PUBLISH
def publish_status():
    payload = {
        "state": state,
        "motor": motor,
        "I3_end_sensor": I3,
        "piece_present": piece
    }
    client.publish(STATUS_TOPIC, json.dumps(payload), qos=1)
    log(f"STATUS PUBLISHED: {payload}")

# CONVEYOR SEQUENCE
def conveyor_cycle():
    global state, motor, I3, piece

    log("Conveyor cycle started, moving to I3 sensor...")
    time.sleep(TIME_TO_I3)

    with lock:
        if state != "RUNNING":
            log(f"Conveyor interrupted before reaching I3. Current state: {state}")
            return

        I3 = 1
        state = "DELAY_STOP"
        log("Piece reached end sensor I3, entering DELAY_STOP...")
        publish_status()

    time.sleep(DELAY_STOP)

    with lock:
        motor = 0
        I3 = 0
        piece = False
        state = "IDLE"
        log("Conveyor stopped, state reset to IDLE.")
        publish_status()

# MQTT CALLBACK
def on_message(client, userdata, msg):
    global state, motor, I3, piece

    topic = msg.topic
    payload = msg.payload.decode()
    log(f"MQTT MESSAGE RECEIVED: Topic={topic}, Payload={payload}")

    with lock:
        if topic == CMD_RESET:
            state = "IDLE"
            motor = 0
            I3 = 0
            piece = False
            log("RESET command received: Conveyor reset to IDLE.")
            publish_status()
            return

        if topic == CMD_STOP:
            motor = 0
            state = "IDLE"
            log("STOP command received: Conveyor stopped immediately.")
            publish_status()
            return

        if topic in (CMD_INJECT, CMD_START):
            if state == "IDLE":
                piece = True
                motor = 1
                state = "RUNNING"
                log("START/INJECT command received: Conveyor running.")
                publish_status()

                threading.Thread(
                    target=conveyor_cycle,
                    daemon=True
                ).start()
            else:
                log(f"START/INJECT command ignored: Conveyor already {state}")

# MQTT setup
client = mqtt.Client()
client.on_message = on_message
client.connect(BROKER)

client.subscribe([
    (CMD_INJECT, 1),
    (CMD_RESET, 1),
    (CMD_START, 1),
    (CMD_STOP, 1),
])

log("Conveyor simulator started. Publishing initial status...")
publish_status()
client.loop_forever()
