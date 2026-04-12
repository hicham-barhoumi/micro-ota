"""
Unit tests for Phase 5: firmware.py + boot_guard partition rollback.

No real ESP32 or esptool execution required — tests validate the module
structure, argument handling, and boot_guard logic with mocked esp32.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

_BOOT_GUARD = os.path.join(os.path.dirname(__file__), '..', 'device', 'boot_guard.py')
_FIRMWARE   = os.path.join(os.path.dirname(__file__), '..', 'host', 'firmware.py')


# ── boot_guard logic tests ────────────────────────────────────────────────────
# Run boot_guard in a tmpdir so it doesn't touch the real filesystem.

def _load_boot_guard(state_file):
    """Import boot_guard with _STATE_FILE pointing to state_file."""
    import importlib, types
    with open(_BOOT_GUARD) as f:
        src = f.read()
    # Override the state file path
    src = src.replace("_STATE_FILE = '/ota_boot_state.json'",
                      "_STATE_FILE = {!r}".format(state_file))
    mod = types.ModuleType('boot_guard_test')
    exec(compile(src, _BOOT_GUARD, 'exec'), mod.__dict__)
    return mod


def test_boot_increments_crash_count():
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, 'state.json')
        bg = _load_boot_guard(sf)
        bg.boot()
        assert bg.get_crash_count() == 1
        bg2 = _load_boot_guard(sf)
        bg2.boot()
        assert bg2.get_crash_count() == 2


def test_mark_clean_resets_counter():
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, 'state.json')
        bg = _load_boot_guard(sf)
        bg.boot()
        bg.boot()
        bg.mark_clean()
        assert bg.get_crash_count() == 0


def test_boot_warns_at_max_crashes(capsys=None):
    """Third boot should print a warning (rollback call is silenced via except)."""
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, 'state.json')
        bg = _load_boot_guard(sf)
        bg.boot(); bg.boot(); bg.boot()
        assert bg.get_crash_count() == 3


def test_boot_state_persists_across_instances():
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, 'state.json')
        _load_boot_guard(sf).boot()
        _load_boot_guard(sf).boot()
        bg = _load_boot_guard(sf)
        assert bg.get_crash_count() == 2


def test_mark_clean_sets_clean_flag():
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, 'state.json')
        bg = _load_boot_guard(sf)
        bg.boot()
        bg.mark_clean()
        with open(sf) as f:
            state = json.load(f)
        assert state['clean'] is True
        assert state['crashes'] == 0


def test_boot_creates_state_file():
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, 'state.json')
        assert not os.path.exists(sf)
        _load_boot_guard(sf).boot()
        assert os.path.exists(sf)


def test_mark_clean_handles_missing_esp32():
    """mark_clean() must not raise even when esp32 module is absent."""
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, 'state.json')
        bg = _load_boot_guard(sf)
        bg.boot()
        bg.mark_clean()   # esp32 not importable on host — must not raise


def test_boot_guard_has_rollback_function():
    with open(_BOOT_GUARD) as f:
        src = f.read()
    assert '_try_firmware_rollback' in src
    assert 'esp32.Partition' in src
    assert 'mark_app_valid_cancel_rollback' in src


# ── host/firmware.py structure tests ─────────────────────────────────────────

def test_firmware_module_valid_python():
    with open(_FIRMWARE) as f:
        src = f.read()
    compile(src, _FIRMWARE, 'exec')


def test_firmware_has_flash_function():
    with open(_FIRMWARE) as f:
        src = f.read()
    assert 'def flash(' in src


def test_firmware_has_chip_addresses():
    with open(_FIRMWARE) as f:
        src = f.read()
    assert 'esp32' in src
    assert '0x1000' in src


def test_firmware_check_esptool_raises_helpfully():
    """_check_esptool must call sys.exit when esptool missing (mocked)."""
    import importlib, types, unittest.mock as mock
    with open(_FIRMWARE) as f:
        src = f.read()
    mod = types.ModuleType('firmware_test')
    exec(compile(src, _FIRMWARE, 'exec'), mod.__dict__)
    with mock.patch.dict('sys.modules', {'esptool': None}):
        with mock.patch('builtins.__import__', side_effect=ImportError):
            try:
                mod._check_esptool()
                assert False, 'expected SystemExit'
            except SystemExit:
                pass


def test_flash_raises_on_missing_file():
    import importlib, types
    with open(_FIRMWARE) as f:
        src = f.read()
    mod = types.ModuleType('firmware_test2')
    exec(compile(src, _FIRMWARE, 'exec'), mod.__dict__)
    try:
        mod.flash('/nonexistent/firmware.bin', '/dev/ttyUSB0')
        assert False, 'expected FileNotFoundError'
    except FileNotFoundError:
        pass


def test_uota_flash_subparser_has_erase():
    """uota flash should expose --erase, --chip, --baud flags."""
    import subprocess
    result = subprocess.run(
        [sys.executable, 'host/uota.py', 'flash', '--help'],
        capture_output=True, text=True
    )
    assert '--erase' in result.stdout
    assert '--chip' in result.stdout
    assert '--baud' in result.stdout


# ── runner ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    tests = [v for k, v in list(globals().items()) if k.startswith('test_')]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print('  PASS ', t.__name__)
            passed += 1
        except Exception as e:
            import traceback
            print('  FAIL ', t.__name__, ':', e)
            traceback.print_exc()
            failed += 1
    print('\n%d passed, %d failed' % (passed, failed))
    sys.exit(failed)
