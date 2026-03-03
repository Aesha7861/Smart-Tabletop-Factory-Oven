import json
import time
import threading
from enum import Enum

import paho.mqtt.client as mqtt
import revpimodio2


# MQTT CONFIG
MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_STATUS          = "stf/oven/status"
TOPIC_CMD_STARTUP     = "stf/oven/cmd/startup"
TOPIC_CMD_SHUTDOWN    = "stf/oven/cmd/shutdown"
TOPIC_CMD_RESET       = "stf/oven/cmd/reset"
TOPIC_CMD_SETPOINT    = "stf/oven/cmd/set_temperature"
TOPIC_CMD_BAKE_TIME   = "stf/oven/cmd/set_bake_time"
TOPIC_CMD_PICKUP_DONE = "stf/oven/cmd/pickup_done"


OUT_OVEN_LIGHT = "O_?"
OUT_DOOR_OPEN  = "O_?"
OUT_DOOR_CLOSE = "O_?"

FORCE_VIRTUAL_IO = True

PUBLISH_PERIOD_S = 0.5
TICK_PERIOD_S    = 0.05


# REVPI IO MAPPING (EDIT IF NEEDED)
# Inputs
IN_COOKIE_PRESENT   = "I_9"   # light barrier: True = cookie present
IN_FEEDER_INSIDE    = "I_6"   # feeder end-stop inside
IN_FEEDER_OUTSIDE   = "I_7"   # feeder end-stop outside

# Outputs (if you have them wired; otherwise keep None)
# If you don't know outputs yet, leave these as None and we will operate "virtual movement" with timeouts.
OUT_FEEDER_IN  = None  # e.g. "O_3"
OUT_FEEDER_OUT = None  # e.g. "O_4"


# TIMEOUTS + DEFAULT TIMES (YOUR REQUIREMENT)
# If hardware feedback doesn't happen within TIMEOUT, we "move ahead" and set a fault.
T_WAIT_COOKIE_S      = 12.0  # wait for cookie present at entry
T_FEEDER_IN_TIMEOUT  = 6.0   # wait until IN_FEEDER_INSIDE becomes True
T_FEEDER_OUT_TIMEOUT = 6.0   # wait until IN_FEEDER_OUTSIDE becomes True

# Baking defaults
DEFAULT_BAKE_TIME_S = 5.0    # you can change from Node-RED via MQTT
TEMP_AMBIENT = 25.0


# state machine
class State(Enum):
    IDLE = "IDLE"
    READY = "READY"
    WAIT_COOKIE = "WAIT_COOKIE"
    FEED_IN = "FEED_IN"
    BAKING = "BAKING"
    UNLOAD = "UNLOAD"
    READY_FOR_PICKUP = "READY_FOR_PICKUP"
    SHUTDOWN = "SHUTDOWN"
    FAULT = "FAULT"


class OvenHardwareController:
    def __init__(self):
        # RevPi
        self.io = revpimodio2.RevPiModIO(autorefresh=True)

        # Inputs
        self.i_cookie  = getattr(self.io.io, IN_COOKIE_PRESENT)
        self.i_in      = getattr(self.io.io, IN_FEEDER_INSIDE)
        self.i_out     = getattr(self.io.io, IN_FEEDER_OUTSIDE)

        # Outputs (optional)
        self.o_in  = getattr(self.io.io, OUT_FEEDER_IN) if OUT_FEEDER_IN else None
        self.o_out = getattr(self.io.io, OUT_FEEDER_OUT) if OUT_FEEDER_OUT else None

        # Process / state
        self.state = State.IDLE
        self._state_t = 0.0

        self.ready_for_pickup = False
        self.bake_time_s = DEFAULT_BAKE_TIME_S
        self.temp_setpoint = 180.0
        self.temp_actual = TEMP_AMBIENT
        self.bake_remaining_s = 0.0
        
        OUT_OVEN_LIGHT = "O_?"
        OUT_DOOR_OPEN  = "O_?"
        OUT_DOOR_CLOSE = "O_?"


        # fault handling
        self.fault = "OK"
        self.fault_latched = False

        # command latches
        self._cmd_start = False
        self._cmd_stop = False
        self._cmd_reset = False

        # MQTT
        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        self._running = True

        # Ensure safe outputs
        self._set_outputs(in_cmd=False, out_cmd=False)

    # RevPi output helpers
    def _write(self):
        self.io.writeprocimg()

    def _set_outputs(self, in_cmd: bool, out_cmd: bool):
        # If outputs not configured, ignore (virtual feeder)
        if self.o_in is not None:
            self.o_in.value = bool(in_cmd)
        if self.o_out is not None:
            self.o_out.value = bool(out_cmd)
        self._write()



    def set_light(self, on: bool):
        self.o_light.value = bool(on)
        self._write()

    def door_open(self):
        self.o_door_close.value = False
        self.o_door_open.value = True
        self._write()

    def door_close(self):
        self.o_door_open.value = False
        self.o_door_close.value = True
        self._write()

    def door_stop(self):
        self.o_door_open.value = False
        self.o_door_close.value = False
        self._write()



    def _stop_outputs(self):
        self._set_outputs(False, False)

    # MQTT
    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        print(" MQTT connected rc=", reason_code)
        for t in [
            TOPIC_CMD_STARTUP, TOPIC_CMD_SHUTDOWN, TOPIC_CMD_RESET,
            TOPIC_CMD_SETPOINT, TOPIC_CMD_BAKE_TIME, TOPIC_CMD_PICKUP_DONE
        ]:
            client.subscribe(t)
        print(" Subscribed to oven command topics.")

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="ignore").strip()

        # Debug
        print(f" MQTT: {topic} {payload}")

        if topic == TOPIC_CMD_STARTUP:
            self._cmd_start = True
        elif topic == TOPIC_CMD_SHUTDOWN:
            self._cmd_stop = True
        elif topic == TOPIC_CMD_RESET:
            self._cmd_reset = True
        elif topic == TOPIC_CMD_SETPOINT:
            try:
                self.temp_setpoint = float(payload)
            except:
                pass
        elif topic == TOPIC_CMD_BAKE_TIME:
            try:
                self.bake_time_s = max(0.5, float(payload))
            except:
                pass
        elif topic == TOPIC_CMD_PICKUP_DONE:
            # Handshake from VGR
            self.ready_for_pickup = False
            if self.state == State.READY_FOR_PICKUP:
                self.state = State.READY
                self._state_t = 0.0

    # Fault strategy (YOUR REQUEST)
    def _fault_and_continue(self, code: str):
        """
        Set a fault but DO NOT stop the whole system.
        We continue using default/virtual completion.
        """
        if not self.fault_latched:
            self.fault = code
            self.fault_latched = True
            print(" FAULT (continuing):", code)

    def _reset_fault(self):
        self.fault = "OK"
        self.fault_latched = False

    # Temperature simulation (dashboard only)
    def _update_temp(self, dt: float):
        # simple first-order behavior toward setpoint when baking, else toward ambient
        target = self.temp_setpoint if self.state == State.BAKING else TEMP_AMBIENT
        alpha = 0.25 if self.state == State.BAKING else 0.08
        self.temp_actual += (target - self.temp_actual) * alpha * dt

    # Main tick / state machine
    def tick(self, dt: float):
        self._state_t += dt

        # Read inputs
        cookie_present = bool(self.i_cookie.value)
        feeder_inside  = bool(self.i_in.value)
        feeder_outside = bool(self.i_out.value)

        # Commands have priority
        if self._cmd_reset:
            self._cmd_reset = False
            self._stop_outputs()
            self.ready_for_pickup = False
            self.bake_remaining_s = 0.0
            self.state = State.IDLE
            self._state_t = 0.0
            self._reset_fault()
            return

        if self._cmd_stop:
            self._cmd_stop = False
            self._stop_outputs()
            self.ready_for_pickup = False
            self.bake_remaining_s = 0.0
            self.state = State.SHUTDOWN
            self._state_t = 0.0
            return

        if self._cmd_start:
            self._cmd_start = False
            if self.state in [State.IDLE, State.SHUTDOWN]:
                self.state = State.READY
                self._state_t = 0.0

        # Update temperature model
        self._update_temp(dt)

        #  STATES 
        if self.state == State.IDLE:
            # do nothing
            self._stop_outputs()

        elif self.state == State.SHUTDOWN:
            # safe outputs off
            self._stop_outputs()
            if self._state_t >= 0.5:
                self.state = State.IDLE
                self._state_t = 0.0

        elif self.state == State.READY:
            self.ready_for_pickup = False
            self._stop_outputs()
            # Go wait for cookie
            self.state = State.WAIT_COOKIE
            self._state_t = 0.0

        elif self.state == State.WAIT_COOKIE:
            # Wait for cookie_present, but if it doesn't come within T_WAIT_COOKIE_S,
            # we "move ahead" and continue (as requested).
            if cookie_present:
                self.state = State.FEED_IN
                self._state_t = 0.0
            elif self._state_t >= T_WAIT_COOKIE_S:
                self._fault_and_continue("COOKIE_NOT_DETECTED_TIMEOUT")
                # move ahead anyway
                self.state = State.FEED_IN
                self._state_t = 0.0

        elif self.state == State.FEED_IN:
            # drive feeder inward if outputs exist, else virtual
            if self.o_in is not None:
                self._set_outputs(in_cmd=True, out_cmd=False)

            # if feeder_inside becomes true -> done
            if feeder_inside:
                self._stop_outputs()
                self.bake_remaining_s = float(self.bake_time_s)
                self.state = State.BAKING
                self._state_t = 0.0
            elif self._state_t >= T_FEEDER_IN_TIMEOUT:
                self._fault_and_continue("FEEDER_IN_TIMEOUT")
                self._stop_outputs()
                # move ahead anyway
                self.bake_remaining_s = float(self.bake_time_s)
                self.state = State.BAKING
                self._state_t = 0.0

        elif self.state == State.BAKING:
            # count down
            self.bake_remaining_s = max(0.0, self.bake_remaining_s - dt)
            if self.bake_remaining_s <= 0.0:
                self.state = State.UNLOAD
                self._state_t = 0.0

        elif self.state == State.UNLOAD:
            # drive feeder outward if outputs exist, else virtual
            if self.o_out is not None:
                self._set_outputs(in_cmd=False, out_cmd=True)

            if feeder_outside:
                self._stop_outputs()
                self.ready_for_pickup = True
                self.state = State.READY_FOR_PICKUP
                self._state_t = 0.0
            elif self._state_t >= T_FEEDER_OUT_TIMEOUT:
                self._fault_and_continue("FEEDER_OUT_TIMEOUT")
                self._stop_outputs()
                self.ready_for_pickup = True
                self.state = State.READY_FOR_PICKUP
                self._state_t = 0.0

        elif self.state == State.READY_FOR_PICKUP:
            # Wait until VGR sends pickup_done; then on_message() moves us to READY
            pass

        elif self.state == State.FAULT:
            # (Not used in "continue" strategy; kept for future)
            self._stop_outputs()

    # Status publish
    def make_status(self) -> dict:
        cookie_present = bool(self.i_cookie.value)
        feeder_inside  = bool(self.i_in.value)
        feeder_outside = bool(self.i_out.value)

        return {
            "ts": time.time(),
            "state": self.state.value,
            "ready_for_pickup": bool(self.ready_for_pickup),
            "cookie_present": cookie_present,
            "feeder_inside": feeder_inside,
            "feeder_outside": feeder_outside,
            "temp_actual": round(float(self.temp_actual), 1),
            "temp_setpoint": round(float(self.temp_setpoint), 1),
            "bake_remaining_s": int(round(self.bake_remaining_s)),
            "fault": self.fault,
            "fault_latched": bool(self.fault_latched),
        }

    def publisher_loop(self):
        while self._running:
            self.client.publish(TOPIC_STATUS, json.dumps(self.make_status()), qos=0, retain=False)
            time.sleep(PUBLISH_PERIOD_S)

    def tick_loop(self):
        last = time.time()
        while self._running:
            now = time.time()
            dt = now - last
            last = now
            if dt > 1.0:
                dt = 0.05
            self.tick(dt)
            time.sleep(TICK_PERIOD_S)

    def run(self):
        print(" Starting STF OVEN HARDWARE controller (MQTT + RevPi)")
        self.client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.client.loop_start()

        threading.Thread(target=self.publisher_loop, daemon=True).start()
        threading.Thread(target=self.tick_loop, daemon=True).start()

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print(" Stopping...")
            self._running = False
            self._stop_outputs()
            self.client.loop_stop()
            self.client.disconnect()
            self.io.exit()


if __name__ == "__main__":
    OvenHardwareController().run()
