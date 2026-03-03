import time, sys
import revpimodio2

# Tune these until motion reaches end positions safely

T_TO_OVEN   = 2.5
T_TO_TABLE  = 2.5
T_LOWER     = 1.2
T_RAISE     = 1.2
T_VAC_BUILD = 0.6

def off(rev):
    for o in ["O_7","O_8","O_10","O_11","O_12"]:
        rev.io[o].value = False

def main():
    rev = revpimodio2.RevPiModIO(autorefresh=True)
    print(" Connected. Time-based VGR test. Ctrl+C to stop.")
    off(rev)

    try:
        print(" Move to OVEN (O_7)")
        rev.io.O_8.value = False
        rev.io.O_7.value = True
        time.sleep(T_TO_OVEN)
        rev.io.O_7.value = False

        print(" Lower (O_12)")
        rev.io.O_12.value = True
        time.sleep(T_LOWER)
        rev.io.O_12.value = False

        print(" Vacuum ON (O_10 + O_11)")
        rev.io.O_10.value = True
        rev.io.O_11.value = True
        time.sleep(T_VAC_BUILD)

        print(" Raise")
        rev.io.O_12.value = False
        time.sleep(T_RAISE)

        print(" Move to TURNTABLE (O_8)")
        rev.io.O_7.value = False
        rev.io.O_8.value = True
        time.sleep(T_TO_TABLE)
        rev.io.O_8.value = False

        print(" Lower")
        rev.io.O_12.value = True
        time.sleep(T_LOWER)
        rev.io.O_12.value = False

        print("🫳 Release vacuum (O_11 OFF, O_10 OFF)")
        rev.io.O_11.value = False
        time.sleep(0.3)
        rev.io.O_10.value = False

        print(" Raise")
        time.sleep(T_RAISE)

        off(rev)
        print(" Done.")
        return 0

    except KeyboardInterrupt:
        print("\n Stopping (Ctrl+C).")
        off(rev)
        return 130

if __name__ == "__main__":
    sys.exit(main())
