"""
uota — micro-ota device package.

Installed to /lib/uota/ on the device.  Import the main entry point with:

    from uota.ota import OTAUpdater

or use the convenience re-exports from this package:

    from uota import OTAUpdater
"""

from .ota import OTAUpdater
from . import boot_guard
from . import remoteio
