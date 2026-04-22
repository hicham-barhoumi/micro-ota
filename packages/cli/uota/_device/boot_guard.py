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
    Print a rollback notice and advise recovery options.

    Automatic machine.reset() / partition switching is intentionally omitted:
    it would interrupt a concurrent bootstrap session and, if the secondary OTA
    partition has no valid firmware, would put the device into an infinite reset
    loop.  The device continues booting so the OTA server can reach a host that
    issues a new push or bootstrap to self-repair.
    """
    try:
        import esp32
        current = esp32.Partition(esp32.Partition.RUNNING)
        previous = current.get_next_update()
        if previous is not None:
            print('[boot_guard] Previous firmware partition available.')
        else:
            print('[boot_guard] No previous partition available.')
    except Exception:
        pass
    print('[boot_guard] To recover: uota bootstrap (USB required)')
