# micro-ota boot.py — serial transport
# OTAUpdater is imported here (main thread) so the background thread does
# not need to run the package import machinery on its small stack.
from uota import boot_guard
from uota.ota import OTAUpdater
boot_guard.boot()

import _thread

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
