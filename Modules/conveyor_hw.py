import time

import revpimodio2

import signal

import sys



# SAFETY HANDLER

# This ensures the motor stops if you cancel with Ctrl+C

def cleanup(signal, frame):

    print("\nINTERRUPTED! Force stopping Conveyor Belt.")

    rpi.io.O_3.value = False

    sys.exit(0)



signal.signal(signal.SIGINT, cleanup)



# SETUP

rpi = revpimodio2.RevPiModIO(autorefresh=True)



# CONFIGURATION

# Adjust this time to match the physical length of your belt

RUN_TIME = 4.0  

# [cite_start]O_3 is "Motor Förderband vorwärts" (Conveyor Belt Forward) [cite: 1]



print( "BLIND CONVEYOR SIMULATION")

print(f"WARNING: Light Barrier (I_3) is IGNORED. Running for {RUN_TIME} seconds fixed.")



try:


    # STEP 1: Turn Motor ON


    print(f"[ACTION] Conveyor Motor (O_3) ON. Moving workpiece...")

    rpi.io.O_3.value = True 


    # STEP 2: Wait (Simulate Movement)


    # This loop prints a dot every second so you know it's working

    elapsed = 0

    while elapsed < RUN_TIME:

        time.sleep(1)

        elapsed += 1

        print(f"  -> Moving... ({elapsed}s)")




    # STEP 3: Turn Motor OFF


    print(f"[ACTION] Time limit reached ({RUN_TIME}s). Stopping Conveyor.")

    rpi.io.O_3.value = False

    print("[STATUS] Conveyor Motor (O_3) is OFF.")



except Exception as e:

    print(f"CRITICAL ERROR: {e}")

    rpi.io.O_3.value = False



finally:

    # Double check safety off

    rpi.io.O_3.value = False

    print(" SEQUENCE COMPLETE. SCRIPT STOPPED")