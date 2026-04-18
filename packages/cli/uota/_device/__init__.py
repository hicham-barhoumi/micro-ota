# uota — micro-ota device package.
#
# Add the package directory itself to sys.path so that internal imports
# within ota.py can use plain absolute imports:
#   from transports.wifi_tcp import WiFiTCPTransport
# instead of relative imports:
#   from .transports.wifi_tcp import WiFiTCPTransport
#
# Relative imports in MicroPython traverse the package hierarchy through
# the import machinery on every call, which uses significant thread stack
# and overflows the default 4 KB ESP32 thread stack.  Plain absolute imports
# are a direct sys.path lookup and use far less stack.
import sys as _sys
_pkg_dir = __file__.rsplit('/', 1)[0]   # /lib/uota
if _pkg_dir not in _sys.path:
    _sys.path.insert(0, _pkg_dir)
del _sys, _pkg_dir
