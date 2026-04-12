"""
micro-ota boot guard.
Add to the top of boot.py:

    import boot_guard
    boot_guard.boot()

Call boot_guard.mark_clean() once the OTA server is up and running.
If the device crashes 3 times before mark_clean() is called, boot_guard
prints a warning. In Phase 3 this will trigger a firmware rollback.
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
    """Call once at the very start of boot.py."""
    state = _load()
    crashes = state.get('crashes', 0) + 1
    state['crashes'] = crashes
    state['clean'] = False
    _save(state)

    if crashes >= _MAX_CRASHES:
        print('[boot_guard] WARNING: {} consecutive unclean boots detected.'.format(crashes))
        print('[boot_guard] If the device keeps crashing, reflash via: uota bootstrap')
        # Phase 3: trigger firmware rollback here via esp32.Partition


def mark_clean():
    """Call once the OTA server is confirmed running. Resets crash counter."""
    state = _load()
    state['crashes'] = 0
    state['clean'] = True
    _save(state)
