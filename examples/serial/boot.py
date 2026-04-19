# micro-ota boot.py — serial transport
import sys
if '/lib/uota' not in sys.path:
    sys.path.insert(0, '/lib/uota')

# Pre-import OTAUpdater in the main thread so the background thread
# does not pay the module-load cost on its limited stack.
import boot_guard
from ota import OTAUpdater
boot_guard.boot()

import _thread

def _ota():
    try:
        upd = OTAUpdater()
        import boot_guard as _bg; _bg.mark_clean()
        upd.run()
    except Exception as _e:
        print('[OTA] Failed to start:', _e)

_thread.start_new_thread(_ota, ())

# Optional: uncomment if your device has LWIP_MAX_SOCKETS >= 2
# def _remoteio():
#     try:
#         import remoteio
#         remoteio.run()
#     except Exception as _e:
#         print('[RemoteIO] Failed to start:', _e)
# _thread.start_new_thread(_remoteio, ())

import time
time.sleep(3)
