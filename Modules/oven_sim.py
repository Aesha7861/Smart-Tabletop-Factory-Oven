import json
import time
import threading
from enum import Enum

import paho.mqtt.client as mqtt
import revpimodio2


# MQTT CONFIG
MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_STATUS = "stf/oven/status"

CMD_STARTUP     = "stf/oven/cmd/startup"
CMD_SHUTDOWN    = "stf/oven/cmd/shutdown"
CMD_RESET       = "stf/oven/cmd/reset"
CMD_SET_TEMP    = "stf/oven/cmd/set_temperature"
CMD_SET_BAKE    = "stf/oven/cmd/set_bake_time"
CMD_PICKUP_DONE = "stf/oven/cmd/pickup_done"   # used for BOTH: load done + unload done

PUBLISH_PERIOD_S = 0.5
TICK_PERIOD_S    = 0.05


# I/O MAPPING (RevPi)
# Inputs (sensors)
IN_COOKIE_PRESENT = "I_9"   # light barrier cookie present (optional; may not be reliable for load)
IN_FEEDER_INSIDE  = "I_6"   # feeder inside
IN_FEEDER_OUTSIDE = "I_7"   # feeder outside

# Outputs (actuators)
OUT_FEEDER_IN     = "O_5"   # retract feeder
OUT_FEEDER_OUT    = "O_6"   # extend feeder
OUT_COMPRESSOR    = "O_10"  # compressor / pneumatics enable
OUT_LIGHT         = "O_9"   # oven inside light
OUT_DOOR          = "O_13"  # door valve (ON=open in this logic)
OUT_FEEDER_VALVE  = "O_14"  # feeder valve/enable


# DEFAULT STEP TIMES (seconds)
T_WAIT_COOKIE   = 6.0      # optional start condition
T_DOOR_OPEN     = 0.8
T_FEED_OUT      = 5.0
T_WAIT_VGR_LOAD = 20.0     # how long we wait for VGR to place raw cookie
T_FEED_IN       = 5.0
T_DOOR_CLOSE    = 1.5
T_WAIT_VGR_UNLOAD = 20.0   # how long we wait for VGR to take baked cookie

T_BAKE_DEFAULT  = 5


# STATE MACHINE
class State(Enum):
    IDLE = "IDLE"
    WAIT_COOKIE = "WAIT_COOKIE"

    # LOAD sequence (VGR puts raw cookie)
    DOOR_OPEN_LOAD = "DOOR_OPEN_LOAD"
    FEED_OUT_LOAD = "FEED_OUT_LOAD"
    WAIT_VGR_LOAD = "WAIT_VGR_LOAD"
    FEED_IN_LOAD = "FEED_IN_LOAD"
    DOOR_CLOSE_BEFORE_BAKE = "DOOR_CLOSE_BEFORE_BAKE"

    BAKING = "BAKING"

    # UNLOAD sequence (VGR takes baked cookie)
    DOOR_OPEN_UNLOAD = "DOOR_OPEN_UNLOAD"
    FEED_OUT_UNLOAD = "FEED_OUT_UNLOAD"
    WAIT_VGR_UNLOAD = "WAIT_VGR_UNLOAD"
    FEED_IN_HOME = "FEED_IN_HOME"
    DOOR_CLOSE_END = "DOOR_CLOSE_END"

    STOPPED = "STOPPED"


class OvenOnlyController:
    def __init__(self):
        # RevPi
        self.io = revpimodio2.RevPiModIO(autorefresh=True)

        # Inputs
        self.i_cookie = getattr(self.io.io, IN_COOKIE_PRESENT)
        self.i_in     = getattr(self.io.io, IN_FEEDER_INSIDE)
        self.i_out    = getattr(self.io.io, IN_FEEDER_OUTSIDE)

        # Outputs
        self.o_in     = getattr(self.io.io, OUT_FEEDER_IN)
        self.o_out    = getattr(self.io.io, OUT_FEEDER_OUT)
        self.o_comp   = getattr(self.io.io, OUT_COMPRESSOR)
        self.o_light  = getattr(self.io.io, OUT_LIGHT)
        self.o_door   = getattr(self.io.io, OUT_DOOR)
        self.o_valve  = getattr(self.io.io, OUT_FEEDER_VALVE)

        # Process vars
        self.state = State.IDLE
        self._t = 0.0

        self.temp_setpoint = 180.0
        self.temp_actual = 25.0
        self.bake_time_s = int(T_BAKE_DEFAULT)
        self.bake_remaining_s = 0

        # Handoff reporting
        self.ready_for_pickup = False
        self.handoff_mode = None   # None / "LOAD" / "UNLOAD"

        # Reporting / diagnostics
        self.step_results = {}     # e.g. {"FEED_OUT_LOAD":"TIMEOUT"}
        self.warnings = []
        self.last_warning = None

        # Command latches
        self._cmd_startup = False
        self._cmd_shutdown = False
        self._cmd_reset = False
        self._cmd_pickup_done = False

        # MQTT
        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        self._running = True

        # Safe initial outputs
        self._all_off()
        self._write()

    #  low level helpers
    def _write(self):
        self.io.writeprocimg()

    def _all_off(self):
        self.o_in.value = False
        self.o_out.value = False
        self.o_comp.value = False
        self.o_door.value = False
        self.o_valve.value = False
        self.o_light.value = False

    def _enable_air(self):
        self.o_comp.value = True
        self.o_valve.value = True

    def _warn(self, code: str):
        self.last_warning = code
        self.warnings.append(code)
        if len(self.warnings) > 20:
            self.warnings = self.warnings[-20:]
        print(f" STEP WARNING: {code}")

    def _mark_step(self, name: str, result: str):
        self.step_results[name] = result

    def _set_wait_handoff(self, mode: str):
        # mode: "LOAD" or "UNLOAD"
        self.ready_for_pickup = True
        self.handoff_mode = mode

    def _clear_handoff(self):
        self.ready_for_pickup = False
        self.handoff_mode = None

    #  MQTT callbacks
    def on_connect(self, client, userdata, flags, rc):
        print(" MQTT connected rc=", rc)
        for t in [CMD_STARTUP, CMD_SHUTDOWN, CMD_RESET, CMD_SET_TEMP, CMD_SET_BAKE, CMD_PICKUP_DONE]:
            client.subscribe(t)
        print(" Subscribed to oven command topics.")

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        print(" MQTT:", topic, payload)

        if topic == CMD_STARTUP:
            self._cmd_startup = True
        elif topic == CMD_SHUTDOWN:
            self._cmd_shutdown = True
        elif topic == CMD_RESET:
            self._cmd_reset = True
        elif topic == CMD_SET_TEMP:
            try:
                self.temp_setpoint = float(payload)
            except Exception:
                self._warn("BAD_SET_TEMPERATURE_PAYLOAD")
        elif topic == CMD_SET_BAKE:
            try:
                self.bake_time_s = int(float(payload))
            except Exception:
                self._warn("BAD_SET_BAKE_TIME_PAYLOAD")
        elif topic == CMD_PICKUP_DONE:
            # used for BOTH: load done + unload done
            self._cmd_pickup_done = True

    #  main tick
    def tick(self, dt: float):
        self._t += dt

        cookie = bool(self.i_cookie.value)
        feeder_in = bool(self.i_in.value)
        feeder_out = bool(self.i_out.value)

        # RESET always wins
        if self._cmd_reset:
            self._cmd_reset = False
            self._clear_handoff()
            self.bake_remaining_s = 0
            self.state = State.IDLE
            self._t = 0.0
            self.step_results = {}
            self.warnings = []
            self.last_warning = None
            self._cmd_pickup_done = False
            self._all_off()
            self._write()
            return

        # SHUTDOWN
        if self._cmd_shutdown:
            self._cmd_shutdown = False
            self.state = State.STOPPED
            self._t = 0.0
            self._clear_handoff()
            self._cmd_pickup_done = False
            self._all_off()
            self._write()
            return

        # STARTUP (start cycle)
        if self.state in [State.IDLE, State.STOPPED] and self._cmd_startup:
            self._cmd_startup = False
            self._clear_handoff()
            self.bake_remaining_s = 0
            self._t = 0.0
            self.step_results = {}
            self.warnings = []
            self.last_warning = None
            self._cmd_pickup_done = False

            # optional: you can skip WAIT_COOKIE and go directly to DOOR_OPEN_LOAD
            self.state = State.WAIT_COOKIE

            self.o_light.value = True
            self._enable_air()
            self._write()
            return

        #  State machine
        if self.state == State.IDLE:
            self._all_off()
            self._clear_handoff()
            self._write()

        elif self.state == State.WAIT_COOKIE:
            # optional start gate: wait for cookie sensor OR timeout then continue anyway
            self.o_light.value = True
            self._enable_air()
            self._write()

            if cookie:
                self._mark_step("WAIT_COOKIE", "OK")
                self.state = State.DOOR_OPEN_LOAD
                self._t = 0.0
            elif self._t >= T_WAIT_COOKIE:
                self._mark_step("WAIT_COOKIE", "TIMEOUT")
                self._warn("COOKIE_NOT_DETECTED_TIMEOUT")
                self.state = State.DOOR_OPEN_LOAD
                self._t = 0.0

        #  LOAD (VGR places raw cookie) 
        elif self.state == State.DOOR_OPEN_LOAD:
            self.o_light.value = True
            self._enable_air()
            self.o_door.value = True   # open door
            self._write()

            if self._t >= T_DOOR_OPEN:
                self._mark_step("DOOR_OPEN_LOAD", "OK")
                self.state = State.FEED_OUT_LOAD
                self._t = 0.0

        elif self.state == State.FEED_OUT_LOAD:
            # feeder goes OUT to present tray to VGR for loading cookie
            self.o_light.value = True
            self._enable_air()
            self.o_door.value = True

            self.o_in.value = False
            self.o_out.value = True
            self._write()

            if feeder_out:
                self._mark_step("FEED_OUT_LOAD", "OK")
                self.o_out.value = False
                self._write()
                self.state = State.WAIT_VGR_LOAD
                self._t = 0.0
                self._set_wait_handoff("LOAD")
            elif self._t >= T_FEED_OUT:
                self._mark_step("FEED_OUT_LOAD", "TIMEOUT")
                self._warn("FEED_OUT_LOAD_TIMEOUT")
                self.o_out.value = False
                self._write()
                self.state = State.WAIT_VGR_LOAD
                self._t = 0.0
                self._set_wait_handoff("LOAD")

        elif self.state == State.WAIT_VGR_LOAD:
            # Door OPEN, feeder OUT, wait for VGR to place raw cookie then send pickup_done
            self.o_light.value = True
            self._enable_air()
            self.o_door.value = True
            self.o_in.value = False
            self.o_out.value = False
            self._write()

            if self._cmd_pickup_done:
                self._cmd_pickup_done = False
                self._mark_step("WAIT_VGR_LOAD", "OK")
                self._clear_handoff()
                self.state = State.FEED_IN_LOAD
                self._t = 0.0
            elif self._t >= T_WAIT_VGR_LOAD:
                self._mark_step("WAIT_VGR_LOAD", "TIMEOUT")
                self._warn("VGR_LOAD_TIMEOUT_CONTINUE")
                # skip-safe: continue even if VGR did not confirm
                self._cmd_pickup_done = False
                self._clear_handoff()
                self.state = State.FEED_IN_LOAD
                self._t = 0.0

        elif self.state == State.FEED_IN_LOAD:
            # retract feeder INSIDE to carry raw cookie into oven
            self.o_light.value = True
            self._enable_air()
            self.o_door.value = True

            self.o_out.value = False
            self.o_in.value = True
            self._write()

            if feeder_in:
                self._mark_step("FEED_IN_LOAD", "OK")
                self.o_in.value = False
                self._write()
                self.state = State.DOOR_CLOSE_BEFORE_BAKE
                self._t = 0.0
            elif self._t >= T_FEED_IN:
                self._mark_step("FEED_IN_LOAD", "TIMEOUT")
                self._warn("FEED_IN_LOAD_TIMEOUT")
                self.o_in.value = False
                self._write()
                self.state = State.DOOR_CLOSE_BEFORE_BAKE
                self._t = 0.0

        elif self.state == State.DOOR_CLOSE_BEFORE_BAKE:
            # close door before baking
            self.o_light.value = True
            self._enable_air()
            self.o_door.value = False  # close
            self._write()

            if self._t >= T_DOOR_CLOSE:
                self._mark_step("DOOR_CLOSE_BEFORE_BAKE", "OK")
                self.state = State.BAKING
                self._t = 0.0
                self.bake_remaining_s = int(self.bake_time_s)

        #  BAKING 
        elif self.state == State.BAKING:
            self.o_light.value = True
            self._enable_air()
            self.o_door.value = False  # keep closed during baking
            self._write()

            # temp simulation
            if self.temp_actual < self.temp_setpoint:
                self.temp_actual += 1.0 * dt * 10.0
                if self.temp_actual > self.temp_setpoint:
                    self.temp_actual = self.temp_setpoint
            else:
                self.temp_actual -= 0.2 * dt
                if self.temp_actual < 25.0:
                    self.temp_actual = 25.0

            # bake timer countdown (1-second ticks)
            if self._t >= 1.0:
                self._t -= 1.0
                if self.bake_remaining_s > 0:
                    self.bake_remaining_s -= 1

            if self.bake_remaining_s <= 0:
                self._mark_step("BAKING", "OK")
                self.state = State.DOOR_OPEN_UNLOAD
                self._t = 0.0

        #  UNLOAD (VGR takes baked cookie) 
        elif self.state == State.DOOR_OPEN_UNLOAD:
            self.o_light.value = True
            self._enable_air()
            self.o_door.value = True
            self._write()

            if self._t >= T_DOOR_OPEN:
                self._mark_step("DOOR_OPEN_UNLOAD", "OK")
                self.state = State.FEED_OUT_UNLOAD
                self._t = 0.0

        elif self.state == State.FEED_OUT_UNLOAD:
            # push baked cookie outside for VGR pickup
            self.o_light.value = True
            self._enable_air()
            self.o_door.value = True

            self.o_in.value = False
            self.o_out.value = True
            self._write()

            if feeder_out:
                self._mark_step("FEED_OUT_UNLOAD", "OK")
                self.o_out.value = False
                self._write()
                self.state = State.WAIT_VGR_UNLOAD
                self._t = 0.0
                self._set_wait_handoff("UNLOAD")
            elif self._t >= T_FEED_OUT:
                self._mark_step("FEED_OUT_UNLOAD", "TIMEOUT")
                self._warn("FEED_OUT_UNLOAD_TIMEOUT")
                self.o_out.value = False
                self._write()
                self.state = State.WAIT_VGR_UNLOAD
                self._t = 0.0
                self._set_wait_handoff("UNLOAD")

        elif self.state == State.WAIT_VGR_UNLOAD:
            # Door OPEN, feeder OUT, wait for VGR to take baked cookie then send pickup_done
            self.o_light.value = True
            self._enable_air()
            self.o_door.value = True
            self.o_in.value = False
            self.o_out.value = False
            self._write()

            if self._cmd_pickup_done:
                self._cmd_pickup_done = False
                self._mark_step("WAIT_VGR_UNLOAD", "OK")
                self._clear_handoff()
                self.state = State.FEED_IN_HOME
                self._t = 0.0
            elif self._t >= T_WAIT_VGR_UNLOAD:
                self._mark_step("WAIT_VGR_UNLOAD", "TIMEOUT")
                self._warn("VGR_UNLOAD_TIMEOUT_CONTINUE")
                self._cmd_pickup_done = False
                self._clear_handoff()
                self.state = State.FEED_IN_HOME
                self._t = 0.0

        elif self.state == State.FEED_IN_HOME:
            # retract to home position after unloading
            self.o_light.value = True
            self._enable_air()
            self.o_door.value = True

            self.o_out.value = False
            self.o_in.value = True
            self._write()

            if feeder_in:
                self._mark_step("FEED_IN_HOME", "OK")
                self.o_in.value = False
                self._write()
                self.state = State.DOOR_CLOSE_END
                self._t = 0.0
            elif self._t >= T_FEED_IN:
                self._mark_step("FEED_IN_HOME", "TIMEOUT")
                self._warn("FEED_IN_HOME_TIMEOUT")
                self.o_in.value = False
                self._write()
                self.state = State.DOOR_CLOSE_END
                self._t = 0.0

        elif self.state == State.DOOR_CLOSE_END:
            self.o_light.value = True
            self._enable_air()
            self.o_door.value = False
            self._write()

            if self._t >= T_DOOR_CLOSE:
                self._mark_step("DOOR_CLOSE_END", "OK")
                self.state = State.IDLE
                self._t = 0.0
                self._clear_handoff()

        elif self.state == State.STOPPED:
            self._all_off()
            self._clear_handoff()
            self._write()

    #  status
    def make_status(self):
        return {
            "ts": time.time(),
            "state": self.state.value,

            # handoff / coordination with VGR
            "ready_for_pickup": bool(self.ready_for_pickup),
            "handoff_mode": self.handoff_mode,  # "LOAD" or "UNLOAD" or None

            # sensors
            "cookie_present": bool(self.i_cookie.value),
            "feeder_inside": bool(self.i_in.value),
            "feeder_outside": bool(self.i_out.value),

            # process
            "temp_actual": float(self.temp_actual),
            "temp_setpoint": float(self.temp_setpoint),
            "bake_remaining_s": int(self.bake_remaining_s),

            # diagnostics
            "step_results": dict(self.step_results),
            "warnings": list(self.warnings),
            "last_warning": self.last_warning,
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
            if dt > 0.5:
                dt = 0.05
            self.tick(dt)
            time.sleep(TICK_PERIOD_S)

    def run(self):
        print(" Starting STF OVEN ONLY controller (MQTT + RevPi)")
        self.client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.client.loop_start()

        th_pub = threading.Thread(target=self.publisher_loop, daemon=True)
        th_tick = threading.Thread(target=self.tick_loop, daemon=True)
        th_pub.start()
        th_tick.start()

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print(" Stopping OVEN ONLY controller...")
            self._running = False
            self.client.loop_stop()
            self.client.disconnect()
            self._all_off()
            self._write()
            self.io.exit()


if __name__ == "__main__":
    OvenOnlyController().run()
