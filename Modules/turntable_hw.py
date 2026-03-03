import time
import revpimodio2
import signal
import sys

#  SAFETY HANDLER 
def cleanup(signal, frame):
    print("\nINTERRUPTED! Force stopping all turntable motors.")
    rpi.io.O_1.value = False
    rpi.io.O_2.value = False
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)

#  SETUP 
rpi = revpimodio2.RevPiModIO(autorefresh=True)
MOVEMENT_TIME = 3.0  # Time to rotate between stations (Adjust this if needed)
PROCESS_TIME = 2.0   # Time to simulate "Taking/Placing" cookie

print(" BLIND TURNTABLE SIMULATION SEQUENCE ")
print("WARNING: Sensors are IGNORED. Moving based on time only.")

try:
    # STEP 1: Rotate towards Vacuum (Counter-Clockwise)
    print(f"\n[ACTION] Rotating towards Vacuum Gripper (CCW) for {MOVEMENT_TIME}s...")
    rpi.io.O_2.value = True  # CCW ON
    
    # Wait for rotation
    time.sleep(MOVEMENT_TIME)
    
    rpi.io.O_2.value = False # CCW OFF
    print("[STATUS] Rotation Stopped. Arrived at Vacuum (Simulated).")

    # STEP 2: Simulate Taking Cookie
    print(f"[ACTION] Waiting for Vacuum to grab cookie ({PROCESS_TIME}s delay)...")
    time.sleep(PROCESS_TIME)
    print("[STATUS] Cookie taken (Simulated).")

    # STEP 3: Rotate towards Conveyor (Clockwise)
    print(f"\n[ACTION] Rotating towards Conveyor Belt (CW) for {MOVEMENT_TIME}s...")
    rpi.io.O_1.value = True  # CW ON
    
    # Wait for rotation
    time.sleep(MOVEMENT_TIME)
    
    rpi.io.O_1.value = False # CW OFF
    print("[STATUS] Rotation Stopped. Arrived at Conveyor (Simulated).")

    # STEP 4: Simulate Placing Cookie
    print(f"[ACTION] Passing cookie to conveyor ({PROCESS_TIME}s delay)...")
    time.sleep(PROCESS_TIME)
    print("[STATUS] Cookie placed on belt (Simulated).")

except Exception as e:
    print(f"CRITICAL ERROR: {e}")

finally:
    # double check everything is off
    rpi.io.O_1.value = False
    rpi.io.O_2.value = False
    print("\n SEQUENCE COMPLETE. SCRIPT STOPPING ")