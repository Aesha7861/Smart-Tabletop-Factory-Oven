"""
STF Oven Station - Unified Controller (UPDATED)

Merges: Oven + Gripper + Turntable + Conveyor

 Updates in this version:
1) Adds MQTT setters:
   - stf/oven_station/cmd/set_temperature
   - stf/oven_station/cmd/set_bake_time
2) Removes auto-start (cycle starts only when Node-RED sends startup)
3) Fixes O_10 double-mapping by treating it as a single shared AIR_SUPPLY output
   (compressor + vacuum pump share the same hardware output)
4) Publishes bake_time_s in status (so Node-RED can compute progress correctly)
5) Safer shutdown/reset behavior: outputs go OFF immediately

Hardware: Raspberry Pi with RevPi (revpimodio2)
"""

import time
import sys
import signal
import json
import threading
from enum import Enum

import revpimodio2
import paho.mqtt.client as mqtt


# CONFIGURATION

#  MQTT 
MQTT_HOST = "localhost"    # broker on stfpi4 itself
MQTT_PORT = 1883

TOPIC_STATUS = "stf/oven_station/status"

CMD_STARTUP   = "stf/oven_station/cmd/startup"
CMD_SHUTDOWN  = "stf/oven_station/cmd/shutdown"
CMD_RESET     = "stf/oven_station/cmd/reset"
CMD_SET_TEMP  = "stf/oven_station/cmd/set_temperature"
CMD_SET_BAKE  = "stf/oven_station/cmd/set_bake_time"

#  OVEN I/O 
IN_COOKIE_PRESENT = "I_9"
IN_FEEDER_INSIDE  = "I_6"
IN_FEEDER_OUTSIDE = "I_7"

OUT_FEEDER_IN     = "O_5"
OUT_FEEDER_OUT    = "O_6"
OUT_LIGHT         = "O_9"
OUT_DOOR          = "O_13"
OUT_FEEDER_VALVE  = "O_14"

# IMPORTANT:
# O_10 was used as OUT_COMPRESSOR and OUT_VACUUM_PUMP in your code.
# Here we treat it as one shared output (AIR_SUPPLY).
OUT_AIR_SUPPLY    = "O_10"

#  GRIPPER I/O 
OUT_GRIPPER_TO_OVEN  = "O_7"
OUT_GRIPPER_TO_TABLE = "O_8"
OUT_VACUUM_VALVE     = "O_11"
OUT_GRIPPER_LOWER    = "O_12"

#  TURNTABLE I/O 
OUT_TURNTABLE_CW  = "O_1"
OUT_TURNTABLE_CCW = "O_2"

#  CONVEYOR I/O 
OUT_CONVEYOR_MOTOR = "O_3"

#  TIMING PARAMETERS 
# Oven
T_DOOR_OPEN   = 0.8
T_FEED_OUT    = 5.0
T_FEED_IN     = 5.0
T_DOOR_CLOSE  = 1.5
T_BAKE_DEFAULT = 5

# Gripper
T_GRIPPER_TO_OVEN  = 2.5
T_GRIPPER_TO_TABLE = 2.5
T_GRIPPER_LOWER_T  = 1.2
T_GRIPPER_RAISE_T  = 1.2
T_VACUUM_BUILD     = 0.6

# Turntable
T_TURNTABLE_ROTATE  = 3.0
T_TURNTABLE_PROCESS = 2.0

# Conveyor
T_CONVEYOR_RUN = 4.0

TICK_PERIOD_S    = 0.05
PUBLISH_PERIOD_S = 0.5


# STATE MACHINE
class State(Enum):
    IDLE = "IDLE"

    # Oven Load (VGR places raw cookie)
    DOOR_OPEN_LOAD = "DOOR_OPEN_LOAD"
    FEED_OUT_LOAD = "FEED_OUT_LOAD"
    WAIT_VGR_LOAD = "WAIT_VGR_LOAD"
    FEED_IN_LOAD = "FEED_IN_LOAD"
    DOOR_CLOSE_BEFORE_BAKE = "DOOR_CLOSE_BEFORE_BAKE"

    # Baking
    BAKING = "BAKING"

    # Oven Unload (feeder presents baked cookie)
    DOOR_OPEN_UNLOAD = "DOOR_OPEN_UNLOAD"
    FEED_OUT_UNLOAD = "FEED_OUT_UNLOAD"

    # Gripper picks from oven
    GRIPPER_TO_OVEN = "GRIPPER_TO_OVEN"
    GRIPPER_LOWER_OVEN = "GRIPPER_LOWER_OVEN"
    GRIPPER_VACUUM_ON = "GRIPPER_VACUUM_ON"
    GRIPPER_RAISE_OVEN = "GRIPPER_RAISE_OVEN"

    # Gripper moves to turntable
    GRIPPER_TO_TURNTABLE = "GRIPPER_TO_TURNTABLE"
    GRIPPER_LOWER_TABLE = "GRIPPER_LOWER_TABLE"
    GRIPPER_RELEASE = "GRIPPER_RELEASE"
    GRIPPER_RAISE_TABLE = "GRIPPER_RAISE_TABLE"

    # Oven feeder returns home
    FEED_IN_HOME = "FEED_IN_HOME"
    DOOR_CLOSE_END = "DOOR_CLOSE_END"

    # Turntable
    TURNTABLE_WAIT = "TURNTABLE_WAIT"

    # Conveyor
    CONVEYOR_RUN = "CONVEYOR_RUN"

    # Complete
    CYCLE_COMPLETE = "CYCLE_COMPLETE"
    STOPPED = "STOPPED"


# MAIN CONTROLLER
class OvenStationController:
    def __init__(self):
        # RevPi I/O
        self.io = revpimodio2.RevPiModIO(autorefresh=True)
        self._map_io()

        # State machine
        self.state = State.IDLE
        self._t = 0.0

        # Oven vars
        self.bake_time_s = int(T_BAKE_DEFAULT)
        self.bake_remaining_s = 0
        self.temp_actual = 25.0
        self.temp_setpoint = 180.0

        # Command latches
        self._cmd_startup = False
        self._cmd_shutdown = False
        self._cmd_reset = False

        # MQTT
        self.client = mqtt.Client()
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

        self._running = True
        self.cycle_count = 0

        # Safe startup
        self._all_off()

    # I/O mapping
    def _map_io(self):
        io = self.io.io

        # Inputs
        self.i_cookie = getattr(io, IN_COOKIE_PRESENT)
        self.i_feeder_in = getattr(io, IN_FEEDER_INSIDE)
        self.i_feeder_out = getattr(io, IN_FEEDER_OUTSIDE)

        # Oven outputs
        self.o_feeder_in = getattr(io, OUT_FEEDER_IN)
        self.o_feeder_out = getattr(io, OUT_FEEDER_OUT)
        self.o_light = getattr(io, OUT_LIGHT)
        self.o_door = getattr(io, OUT_DOOR)
        self.o_feeder_valve = getattr(io, OUT_FEEDER_VALVE)

        # Shared air supply output (compressor + vacuum pump shared)
        self.o_air = getattr(io, OUT_AIR_SUPPLY)

        # Gripper outputs
        self.o_gripper_oven = getattr(io, OUT_GRIPPER_TO_OVEN)
        self.o_gripper_table = getattr(io, OUT_GRIPPER_TO_TABLE)
        self.o_vacuum_valve = getattr(io, OUT_VACUUM_VALVE)
        self.o_gripper_lower = getattr(io, OUT_GRIPPER_LOWER)

        # Turntable outputs
        self.o_turntable_cw = getattr(io, OUT_TURNTABLE_CW)
        self.o_turntable_ccw = getattr(io, OUT_TURNTABLE_CCW)

        # Conveyor output
        self.o_conveyor = getattr(io, OUT_CONVEYOR_MOTOR)

    def _all_off(self):
        outputs = [
            self.o_feeder_in, self.o_feeder_out,
            self.o_air, self.o_light, self.o_door, self.o_feeder_valve,
            self.o_gripper_oven, self.o_gripper_table,
            self.o_vacuum_valve, self.o_gripper_lower,
            self.o_turntable_cw, self.o_turntable_ccw,
            self.o_conveyor
        ]
        for out in outputs:
            out.value = False
        self.io.writeprocimg()

    def _enable_air(self):
        # Enable pneumatics + shared supply
        self.o_air.value = True
        self.o_feeder_valve.value = True
        
    def _update_temperature(self, dt: float):
        """
    Temperature simulation:
    - During BAKING: temp_actual rises towards temp_setpoint
    - Otherwise: temp_actual cools towards ambient (25C)
    """
        ambient = 25.0

        if self.state == State.BAKING:
         target = float(self.temp_setpoint)
         tau = 10.0   # heating speed (smaller = faster)
        else:
          target = ambient
          tau = 25.0   # cooling speed

        alpha = max(0.0, min(1.0, dt / tau))
        self.temp_actual += (target - self.temp_actual) * alpha


    # MQTT callbacks
    def _on_connect(self, client, userdata, flags, rc):
        print(" MQTT connected")
        for topic in [CMD_STARTUP, CMD_SHUTDOWN, CMD_RESET, CMD_SET_TEMP, CMD_SET_BAKE]:
            client.subscribe(topic)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode(errors="ignore").strip()
        print(f" MQTT: {topic} payload='{payload}'")

        if topic == CMD_STARTUP:
            self._cmd_startup = True

        elif topic == CMD_SHUTDOWN:
            self._cmd_shutdown = True

        elif topic == CMD_RESET:
            self._cmd_reset = True

        elif topic == CMD_SET_TEMP:
            try:
                self.temp_setpoint = float(payload)
                print(f" temp_setpoint set to {self.temp_setpoint}")
            except Exception:
                print(f" Bad temperature payload: '{payload}' (expected number)")

        elif topic == CMD_SET_BAKE:
            try:
                self.bake_time_s = int(float(payload))
                if self.bake_time_s < 1:
                    self.bake_time_s = 1
                print(f" bake_time_s set to {self.bake_time_s}")
            except Exception:
                print(f" Bad bake time payload: '{payload}' (expected number)")

    # State machine tick
    def tick(self, dt: float):
        self._t += dt
        self._update_temperature(dt)

        feeder_in = bool(self.i_feeder_in.value)
        feeder_out = bool(self.i_feeder_out.value)

        #  RESET 
        if self._cmd_reset:
            self._cmd_reset = False
            self._all_off()
            self.state = State.IDLE
            self._t = 0.0
            self.bake_remaining_s = 0
            print(" RESET")
            return

        #  SHUTDOWN 
        if self._cmd_shutdown:
            self._cmd_shutdown = False
            self._all_off()
            self.state = State.STOPPED
            self._t = 0.0
            self.bake_remaining_s = 0
            print(" SHUTDOWN")
            return

        #  STARTUP 
        if self.state in [State.IDLE, State.STOPPED, State.CYCLE_COMPLETE] and self._cmd_startup:
            self._cmd_startup = False
            self._t = 0.0
            self.state = State.DOOR_OPEN_LOAD
            self.o_light.value = True
            self._enable_air()
            print(" STARTING CYCLE")
            return

        # IDLE / STOPPED
        if self.state == State.IDLE:
            # Keep everything off in idle
            self._all_off()

        elif self.state == State.STOPPED:
            self._all_off()

        # OVEN LOAD SEQUENCE
        elif self.state == State.DOOR_OPEN_LOAD:
            self.o_light.value = True
            self._enable_air()
            self.o_door.value = True
            self.io.writeprocimg()

            if self._t >= T_DOOR_OPEN:
                print(" Door opened (LOAD)")
                self.state = State.FEED_OUT_LOAD
                self._t = 0.0

        elif self.state == State.FEED_OUT_LOAD:
            self.o_feeder_out.value = True
            self.o_feeder_in.value = False
            self.io.writeprocimg()

            if feeder_out or self._t >= T_FEED_OUT:
                self.o_feeder_out.value = False
                self.io.writeprocimg()
                print(" Feeder OUT (ready for loading)")
                self.state = State.WAIT_VGR_LOAD
                self._t = 0.0

        elif self.state == State.WAIT_VGR_LOAD:
            # In merged version, simulate cookie placed
            if self._t >= 1.0:
                print(" Cookie loaded (simulated)")
                self.state = State.FEED_IN_LOAD
                self._t = 0.0

        elif self.state == State.FEED_IN_LOAD:
            self.o_feeder_in.value = True
            self.o_feeder_out.value = False
            self.io.writeprocimg()

            if feeder_in or self._t >= T_FEED_IN:
                self.o_feeder_in.value = False
                self.io.writeprocimg()
                print(" Feeder IN")
                self.state = State.DOOR_CLOSE_BEFORE_BAKE
                self._t = 0.0

        elif self.state == State.DOOR_CLOSE_BEFORE_BAKE:
            self.o_door.value = False
            self.io.writeprocimg()

            if self._t >= T_DOOR_CLOSE:
                print(" Door closed → BAKING")
                self.state = State.BAKING
                self._t = 0.0
                self.bake_remaining_s = int(self.bake_time_s)

        # BAKING
        elif self.state == State.BAKING:
            self.o_light.value = True
            self.o_door.value = False
            self.io.writeprocimg()

            if self._t >= 1.0:
                self._t -= 1.0
                if self.bake_remaining_s > 0:
                    self.bake_remaining_s -= 1
                    print(f" Baking... {self.bake_remaining_s}s remaining")

            if self.bake_remaining_s <= 0:
                print(" BAKING COMPLETE")
                self.state = State.DOOR_OPEN_UNLOAD
                self._t = 0.0

        # OVEN UNLOAD SEQUENCE
        elif self.state == State.DOOR_OPEN_UNLOAD:
            self.o_door.value = True
            self.io.writeprocimg()

            if self._t >= T_DOOR_OPEN:
                print(" Door opened (UNLOAD)")
                self.state = State.FEED_OUT_UNLOAD
                self._t = 0.0

        elif self.state == State.FEED_OUT_UNLOAD:
            self.o_feeder_out.value = True
            self.o_feeder_in.value = False
            self.io.writeprocimg()

            if feeder_out or self._t >= T_FEED_OUT:
                self.o_feeder_out.value = False
                self.io.writeprocimg()
                print(" Feeder OUT (cookie ready for pickup)")
                self.state = State.GRIPPER_TO_OVEN
                self._t = 0.0

        # GRIPPER PICKS FROM OVEN
        elif self.state == State.GRIPPER_TO_OVEN:
            self.o_gripper_table.value = False
            self.o_gripper_oven.value = True
            self.io.writeprocimg()

            if self._t >= T_GRIPPER_TO_OVEN:
                self.o_gripper_oven.value = False
                self.io.writeprocimg()
                print(" Gripper at OVEN")
                self.state = State.GRIPPER_LOWER_OVEN
                self._t = 0.0

        elif self.state == State.GRIPPER_LOWER_OVEN:
            self.o_gripper_lower.value = True
            self.io.writeprocimg()

            if self._t >= T_GRIPPER_LOWER_T:
                self.o_gripper_lower.value = False
                self.io.writeprocimg()
                print(" Gripper lowered")
                self.state = State.GRIPPER_VACUUM_ON
                self._t = 0.0

        elif self.state == State.GRIPPER_VACUUM_ON:
            # shared air supply must be ON
            self._enable_air()
            self.o_vacuum_valve.value = True
            self.io.writeprocimg()

            if self._t >= T_VACUUM_BUILD:
                print(" Vacuum ON - cookie grabbed")
                self.state = State.GRIPPER_RAISE_OVEN
                self._t = 0.0

        elif self.state == State.GRIPPER_RAISE_OVEN:
            self.o_gripper_lower.value = False
            self.io.writeprocimg()

            if self._t >= T_GRIPPER_RAISE_T:
                print(" Gripper raised")
                self.state = State.GRIPPER_TO_TURNTABLE
                self._t = 0.0

        # GRIPPER MOVES TO TURNTABLE
        elif self.state == State.GRIPPER_TO_TURNTABLE:
            self.o_gripper_oven.value = False
            self.o_gripper_table.value = True
            self.io.writeprocimg()

            if self._t >= T_GRIPPER_TO_TABLE:
                self.o_gripper_table.value = False
                self.io.writeprocimg()
                print(" Gripper at TURNTABLE")
                self.state = State.GRIPPER_LOWER_TABLE
                self._t = 0.0

        elif self.state == State.GRIPPER_LOWER_TABLE:
            self.o_gripper_lower.value = True
            self.io.writeprocimg()

            if self._t >= T_GRIPPER_LOWER_T:
                self.o_gripper_lower.value = False
                self.io.writeprocimg()
                print(" Gripper lowered at turntable")
                self.state = State.GRIPPER_RELEASE
                self._t = 0.0

        elif self.state == State.GRIPPER_RELEASE:
            self.o_vacuum_valve.value = False
            self.io.writeprocimg()

            if self._t >= 0.3:
                print(" Cookie released on turntable")
                self.state = State.GRIPPER_RAISE_TABLE
                self._t = 0.0

        elif self.state == State.GRIPPER_RAISE_TABLE:
            self.o_gripper_lower.value = False
            self.io.writeprocimg()

            if self._t >= T_GRIPPER_RAISE_T:
                print(" Gripper raised")
                self.state = State.FEED_IN_HOME
                self._t = 0.0

        # OVEN FEEDER HOME + TURNTABLE ROTATE
        elif self.state == State.FEED_IN_HOME:
            self.o_feeder_in.value = True
            self.o_feeder_out.value = False

            # Start turntable rotation
            self.o_turntable_cw.value = True
            self.io.writeprocimg()

            if (feeder_in or self._t >= T_FEED_IN) and self._t >= T_TURNTABLE_ROTATE:
                self.o_feeder_in.value = False
                self.o_turntable_cw.value = False
                self.io.writeprocimg()
                print(" Feeder HOME +  Turntable rotated to conveyor")
                self.state = State.DOOR_CLOSE_END
                self._t = 0.0

        elif self.state == State.DOOR_CLOSE_END:
            self.o_door.value = False
            self.io.writeprocimg()

            if self._t >= T_DOOR_CLOSE:
                print(" Door closed")
                self.state = State.TURNTABLE_WAIT
                self._t = 0.0

        # TURNTABLE WAIT
        elif self.state == State.TURNTABLE_WAIT:
            if self._t >= T_TURNTABLE_PROCESS:
                print(" Turntable ready for conveyor")
                self.state = State.CONVEYOR_RUN
                self._t = 0.0

        # CONVEYOR RUN
        elif self.state == State.CONVEYOR_RUN:
            self.o_conveyor.value = True
            self.io.writeprocimg()

            if self._t >= T_CONVEYOR_RUN:
                self.o_conveyor.value = False
                self.io.writeprocimg()
                print(" Conveyor DONE - cookie transported")
                self.state = State.CYCLE_COMPLETE
                self._t = 0.0

        # CYCLE COMPLETE
        elif self.state == State.CYCLE_COMPLETE:
            self._all_off()
            self.cycle_count += 1
            print(f" CYCLE {self.cycle_count} COMPLETE!")
            print(" Waiting for next 'startup' command...")
            self.state = State.IDLE
            self._t = 0.0

    # STATUS
    def make_status(self):
        return {
            "ts": time.time(),
            "state": self.state.value,
            "cycle_count": self.cycle_count,
            "bake_time_s": int(self.bake_time_s),
            "bake_remaining_s": int(self.bake_remaining_s),
            "temp_actual": float(self.temp_actual),
            "temp_setpoint": float(self.temp_setpoint),
        }

    # THREAD LOOPS
    def _publisher_loop(self):
        while self._running:
            try:
                self.client.publish(TOPIC_STATUS, json.dumps(self.make_status()), qos=0)
            except Exception:
                pass
            time.sleep(PUBLISH_PERIOD_S)

    def _tick_loop(self):
        last = time.time()
        while self._running:
            now = time.time()
            dt = min(now - last, 0.5)
            last = now
            self.tick(dt)
            time.sleep(TICK_PERIOD_S)

    # RUN
    def run(self):
        print("=" * 60)
        print("  STF OVEN STATION - UNIFIED CONTROLLER (UPDATED)")
        print("=" * 60)
        print("Modules: Oven + Gripper + Turntable + Conveyor")
        print("Controlled via Node-RED / MQTT")
        print("Press Ctrl+C to stop")
        print()

        try:
            self.client.connect(MQTT_HOST, MQTT_PORT, 60)
            self.client.loop_start()
            print(f" MQTT connected to {MQTT_HOST}:{MQTT_PORT}")
        except Exception as e:
            print(f" MQTT connection failed: {e}")
            print("Running without MQTT...")

        # Start threads
        th_pub = threading.Thread(target=self._publisher_loop, daemon=True)
        th_tick = threading.Thread(target=self._tick_loop, daemon=True)
        th_pub.start()
        th_tick.start()

        try:
            while self._running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n Stopping...")

        self._running = False
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

        self._all_off()
        self.io.exit()
        print("Goodbye!")


# ENTRY POINT
def cleanup_handler(sig, frame):
    print("\nINTERRUPTED - Emergency stop!")
    sys.exit(0)


signal.signal(signal.SIGINT, cleanup_handler)

if __name__ == "__main__":
    controller = OvenStationController()
    controller.run()