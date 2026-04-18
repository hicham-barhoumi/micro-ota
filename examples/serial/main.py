# micro-ota serial example application
# Edit this file and run ./test.sh push to see OTA in action.
import time

VERSION = "1.0.0"

print("=== micro-ota serial example ===")
print("version:", VERSION)

count = 0
while True:
    print("uptime:", count, "s  (version", VERSION + ")")
    time.sleep(5)
    count += 5
