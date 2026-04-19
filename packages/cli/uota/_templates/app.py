# Application entry point — called by main.py
import time

VERSION = "1.0.0"

def run():
    print("=== micro-ota app ===")
    print("version:", VERSION)
    count = 0
    while True:
        print("uptime:", count, "s  (version", VERSION + ")")
        time.sleep(5)
        count += 5
