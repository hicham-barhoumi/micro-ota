# uota — micro-ota device package marker.
# Intentionally empty: MicroPython resolves submodule imports
# (from uota.ota import OTAUpdater, from uota import boot_guard, ...)
# directly from the filesystem without requiring re-exports here.
# Eager imports would overflow the thread stack on ESP32.
