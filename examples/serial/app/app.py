# micro-ota serial example — application entry point
# Edit this file and run 'uota fast' to see OTA in action.
import time

VERSION = "1.0.0"

def run():
    print("=== micro-ota serial example ===")
    print("version:", VERSION)
    count = 0
    while True:
        print("uptime:", count, "s  (version", VERSION + ")")
        time.sleep(5)
        count += 5
