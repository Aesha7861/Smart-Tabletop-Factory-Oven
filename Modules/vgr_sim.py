import json
import time
import threading
from enum import Enum

import paho.mqtt.client as mqtt
import revpimodio2

MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_STATUS = "stf/vgr/status"
CMD_GO_OVEN   = "stf/vgr/cmd/go_oven"
CMD_GO_TABLE  = "stf/vgr/cmd/go_table"
CMD_PICK      = "stf/vgr/cmd/pick"
CMD_RELEASE   = "stf/vgr/cmd/release"
CMD_RESET     = "stf/vgr/cmd/reset"
CMD_STOP      = "stf/vgr/cmd/stop"

OVEN_STATUS_TOPIC = "stf/oven/status"
OVEN_PICKUP_DONE_TOPIC = "stf/oven/cmd/pickup_done"

PUBLISH_PERIOD_S = 0.5
TICK_PERIOD_S = 0.05

# IO mapping (keep your best guess)
IN_AT_TABLE = "I_5"
IN_AT_OVEN  = "I_8"
OUT_MOVE_TO_OVEN  = "O_7"
OUT_MOVE_TO_TABLE = "O_8"
OUT_COMPRESSOR    = "O_10"
OUT_VACUUM        = "O_11"
OUT_LOWER_VALVE   = "O_12"

MOVE_TIMEOUT_S = 4.0
LOWER_TIME_S = 0.6
RAISE_TIME_S = 0.6
VAC_ATTACH_TIME_S = 0.6


class State(Enum):
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
    DONE = "DONE"
    STOPPED = "STOPPED"
    FAULT = "FAULT"


class VGRSkipController:
    def __init__(self):
        self.io = revpimodio2.RevPiModIO(autorefresh=True)
        self.i_table = getattr(self.io.io, IN_AT_TABLE)
        self.i_oven  = getattr(self.io.io, IN_AT_OVEN)

        self.o_to_oven  = getattr(self.io.io, OUT_MOVE_TO_OVEN)
        self.o_to_table = getattr(self.io.io, OUT_MOVE_TO_TABLE)
        self.o_comp     = getattr(self.io.io, OUT_COMPRESSOR)
        self.o_vac      = getattr(self.io.io, OUT_VACUUM)
        self.o_lower    = getattr(self.io.io, OUT_LOWER_VALVE)

        self.state = State.IDLE
        self._state_t = 0.0
        self._pickup_done_sent = False

        self.part_gripped = False
        self.last_fault = "OK"

        # This is what you asked for:
        self.virtual_mode = True  # if sensors/actuators don't work -> skip via timeouts

        # auto trigger
        self.auto_mode = True
        self.oven_ready_for_pickup = False
        self._auto_cycle_active = False

        # command latches
        self._cmd_go_oven = False
        self._cmd_go_table = False
        self._cmd_pick = False
        self._cmd_release = False
        self._cmd_reset = False
        self._cmd_stop = False

        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        self._running = True
        self._stop_outputs()

    def _write(self):
        self.io.writeprocimg()

    def _stop_outputs(self):
        self.o_to_oven.value = False
        self.o_to_table.value = False
        self.o_comp.value = False
        self.o_vac.value = False
        self.o_lower.value = False
        self._write()

    def _fault_and_continue(self, code: str):
        if self.last_fault == "OK":
            self.last_fault = code
        print(" VGR FAULT (continuing):", code)

    def on_connect(self, client, userdata, flags, rc):
        print(" MQTT connected, rc=", rc)
        for t in [CMD_GO_OVEN, CMD_GO_TABLE, CMD_PICK, CMD_RELEASE, CMD_RESET, CMD_STOP, OVEN_STATUS_TOPIC]:
            client.subscribe(t)
        print(" Subscribed to VGR command topics + oven/status")

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", "ignore").strip()

        if topic in [CMD_GO_OVEN, CMD_GO_TABLE, CMD_PICK, CMD_RELEASE, CMD_RESET, CMD_STOP]:
            print(f" MQTT CMD: {topic} {payload}")

        if topic == CMD_GO_OVEN:
            self._cmd_go_oven = True
        elif topic == CMD_GO_TABLE:
            self._cmd_go_table = True
        elif topic == CMD_PICK:
            self._cmd_pick = True
        elif topic == CMD_RELEASE:
            self._cmd_release = True
        elif topic == CMD_RESET:
            self._cmd_reset = True
        elif topic == CMD_STOP:
            self._cmd_stop = True
        elif topic == OVEN_STATUS_TOPIC:
            try:
                data = json.loads(payload or "{}")
                self.oven_ready_for_pickup = bool(data.get("ready_for_pickup", False))
                if self.auto_mode and self.oven_ready_for_pickup and (not self._auto_cycle_active):
                    self._auto_cycle_active = True
                    self._cmd_go_oven = True
                    self._pickup_done_sent = False

            except:
                pass

    def tick(self, dt: float):
        self._state_t += dt

        at_table = bool(self.i_table.value)
        at_oven  = bool(self.i_oven.value)

        if self._cmd_stop:
            self._cmd_stop = False
            self._stop_outputs()
            self.state = State.STOPPED
            self._state_t = 0.0
            return

        if self._cmd_reset:
            self._cmd_reset = False
            self._stop_outputs()
            self.part_gripped = False
            self.last_fault = "OK"
            self.state = State.IDLE
            self._state_t = 0.0
            self._auto_cycle_active = False
            return

        if self.state == State.IDLE:
            if self._cmd_go_oven:
                self._cmd_go_oven = False
                self.o_to_table.value = False
                self.o_to_oven.value = True
                self._write()
                self.state = State.MOVING_TO_OVEN
                self._state_t = 0.0
            elif self._cmd_go_table:
                self._cmd_go_table = False
                self.o_to_oven.value = False
                self.o_to_table.value = True
                self._write()
                self.state = State.MOVING_TO_TABLE
                self._state_t = 0.0

        elif self.state == State.MOVING_TO_OVEN:
            if at_oven:
                self.o_to_oven.value = False
                self._write()
                self.state = State.AT_OVEN
                self._state_t = 0.0
            elif self._state_t >= MOVE_TIMEOUT_S:
                self.o_to_oven.value = False
                self._write()
                self._fault_and_continue("MOVE_TO_OVEN_TIMEOUT")
                if self.virtual_mode:
                    self.state = State.AT_OVEN
                    self._state_t = 0.0
                else:
                    self.state = State.FAULT

        elif self.state == State.AT_OVEN:
            # auto pick if command or auto cycle
            if self._auto_cycle_active or self._cmd_pick:
                self._cmd_pick = False
                self.o_lower.value = True
                self._write()
                self.state = State.LOWER_PICK
                self._state_t = 0.0

        elif self.state == State.LOWER_PICK:
            if self._state_t >= LOWER_TIME_S:
                self.o_comp.value = True
                self.o_vac.value = True
                self._write()
                self.state = State.VAC_ON
                self._state_t = 0.0

        elif self.state == State.VAC_ON:
            if self._state_t >= VAC_ATTACH_TIME_S:
                self.part_gripped = True
                self.o_lower.value = False
                self._write()
                self.state = State.RAISE_PICK
                self._state_t = 0.0

        elif self.state == State.RAISE_PICK:
            if not self._pickup_done_sent:
                self.client.publish("stf/oven/cmd/pickup_done", "1", qos=0, retain=False)
                self._pickup_done_sent = True
                print(" Sent pickup_done once")


            if self._state_t >= RAISE_TIME_S:
                # tell oven pickup done
                self.client.publish(OVEN_PICKUP_DONE_TOPIC, "1", qos=0, retain=False)
                # go to table
                
                
                self.o_to_oven.value = False
                self.o_to_table.value = True
                self._write()
                self.state = State.MOVING_TO_TABLE
                self._state_t = 0.0

        elif self.state == State.MOVING_TO_TABLE:
            if at_table:
                self.o_to_table.value = False
                self._write()
                self.state = State.AT_TABLE
                self._state_t = 0.0
            elif self._state_t >= MOVE_TIMEOUT_S:
                self.o_to_table.value = False
                self._write()
                self._fault_and_continue("MOVE_TO_TABLE_TIMEOUT")
                if self.virtual_mode:
                    self.state = State.AT_TABLE
                    self._state_t = 0.0
                else:
                    self.state = State.FAULT

        elif self.state == State.AT_TABLE:
            if self._auto_cycle_active or self._cmd_release:
                self._cmd_release = False
                self.o_lower.value = True
                self._write()
                self.state = State.LOWER_PLACE
                self._state_t = 0.0

        elif self.state == State.LOWER_PLACE:
            if self._state_t >= LOWER_TIME_S:
                self.o_vac.value = False
                self.o_comp.value = False
                self._write()
                self.state = State.VAC_OFF
                self._state_t = 0.0

        elif self.state == State.VAC_OFF:
            if self._state_t >= 0.3:
                self.part_gripped = False
                self.o_lower.value = False
                self._write()
                self.state = State.RAISE_PLACE
                self._state_t = 0.0

        elif self.state == State.RAISE_PLACE:
            if self._state_t >= RAISE_TIME_S:
                self.state = State.DONE
                self._state_t = 0.0

        elif self.state == State.DONE:
            if self._state_t >= 0.2:
                self.state = State.IDLE
                self._state_t = 0.0
                self._auto_cycle_active = False

    def make_status(self):
        at_table = bool(self.i_table.value)
        at_oven  = bool(self.i_oven.value)
        pos = "MOVING"
        if at_table: pos = "TABLE"
        if at_oven:  pos = "OVEN"
        return {
            "ts": time.time(),
            "state": self.state.value,
            "position": pos,
            "part_gripped": self.part_gripped,
            "fault": self.last_fault,
            "virtual_mode": self.virtual_mode,
            "auto_mode": self.auto_mode,
            "oven_ready_for_pickup": self.oven_ready_for_pickup,
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
            if dt > 0.5: dt = 0.05
            self.tick(dt)
            time.sleep(TICK_PERIOD_S)

    def run(self):
        print(" Starting STF VGR SKIP controller (works even if HW dead)")
        self.client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.client.loop_start()
        threading.Thread(target=self.publisher_loop, daemon=True).start()
        threading.Thread(target=self.tick_loop, daemon=True).start()

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            self._running = False
            self._stop_outputs()
            self.client.loop_stop()
            self.client.disconnect()
            self.io.exit()


if __name__ == "__main__":
    VGRSkipController().run()
