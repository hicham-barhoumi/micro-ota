"""
micro-ota boot guard.

Add to the top of boot.py:

    import boot_guard
    boot_guard.boot()

Call boot_guard.mark_clean() once the OTA server is up and running.

On every boot the crash counter is incremented.  mark_clean() resets it.
If the counter reaches _MAX_CRASHES (3) before mark_clean() is called:
  1. A warning is printed on the serial console.
  2. If esp32.Partition is available (standard MicroPython ESP32 build),
     the device attempts to boot from the previous firmware partition
     (firmware rollback).  This only applies to firmware-level crashes;
     for file-level OTA crashes the warning is printed and the device
     continues booting so the OTA server can self-repair via a new push.
"""

_STATE_FILE = '/ota_boot_state.json'
_MAX_CRASHES = 3


def _load():
    try:
        import json
        with open(_STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {'crashes': 0, 'clean': False}


def _save(state):
    import json
    with open(_STATE_FILE, 'w') as f:
        json.dump(state, f)


def boot():
    """Call once at the very start of boot.py, before anything else."""
    state = _load()
    crashes = state.get('crashes', 0) + 1
    state['crashes'] = crashes
    state['clean'] = False
    _save(state)

    if crashes >= _MAX_CRASHES:
        print('[boot_guard] WARNING: {} consecutive unclean boots.'.format(crashes))
        _try_firmware_rollback()


def mark_clean():
    """
    Call once the OTA server is confirmed running.

    Resets the crash counter and marks the current firmware partition as valid
    (cancels the automatic rollback timer on ESP32 if OTA firmware was used).
    """
    state = _load()
    state['crashes'] = 0
    state['clean'] = True
    _save(state)

    # Mark firmware partition valid — cancels auto-rollback if the bootloader
    # was set to roll back after a failed OTA firmware update.
    try:
        import esp32
        esp32.Partition.mark_app_valid_cancel_rollback()
    except Exception:
        pass   # not available on all builds / not needed for file-only OTA


def get_crash_count():
    """Return the current consecutive unclean-boot count."""
    return _load().get('crashes', 0)


# ── firmware rollback ─────────────────────────────────────────────────────────

def _try_firmware_rollback():
    """
    Attempt to switch to the previous firmware partition and reboot.

    This is effective when a new MicroPython firmware was flashed via
    'uota flash' and the new firmware is crash-looping.  Switching to the
    previous partition restores the old firmware.

    If esp32.Partition is unavailable (e.g. custom build, SAMD, RP2),
    the function is a no-op and the device continues booting normally.
    """
    try:
        import esp32
        current = esp32.Partition(esp32.Partition.RUNNING)
        previous = current.get_next_update()
        if previous is None:
            print('[boot_guard] No previous partition available for rollback.')
            print('[boot_guard] Reflash via: uota bootstrap (USB required)')
            return
        print('[boot_guard] Switching to previous firmware partition…')
        previous.set_boot()
        import machine, time
        time.sleep(0.5)
        machine.reset()
    except Exception as e:
        # esp32.Partition not available — file-based OTA only, no rollback.
        print('[boot_guard] Firmware rollback unavailable ({}).'.format(e))
        print('[boot_guard] To recover: uota bootstrap (USB required)')
