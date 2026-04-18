# micro-ota boot.py — serial transport
# Pre-import OTAUpdater in the main thread where the stack is large.
# Importing it inside a _thread.start_new_thread callback can overflow
# the small default 4 KB ESP32 thread stack.
from uota import boot_guard
from uota.ota import OTAUpdater
boot_guard.boot()

import _thread
try:
    _thread.stack_size(8192)   # 8 KB — prevents stack overflow in OTA/RemoteIO threads
except Exception:
    pass

def _ota():
    try:
        upd = OTAUpdater()
        from uota import boot_guard as _bg
        _bg.mark_clean()
        upd.run()
    except Exception as _e:
        print('[OTA] Failed to start:', _e)

_thread.start_new_thread(_ota, ())

import time
time.sleep(3)
