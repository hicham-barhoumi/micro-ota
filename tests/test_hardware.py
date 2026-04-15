"""
Hardware-in-the-loop tests for micro-ota.

Requirements:
  - ESP32 bootstrapped and running the OTA server
  - Linux/macOS: sg dialout -c "python3 tests/test_hardware.py"
  - Windows:     python tests/test_hardware.py  (run from a terminal with driver installed)

Skip transports selectively:
  SKIP_WIFI=1   python3 tests/test_hardware.py   (serial tests only)
  SKIP_SERIAL=1 python3 tests/test_hardware.py   (WiFi tests only)
"""

import json
import os
import socket
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'packages', 'cli'))

from uota.manifest import build as build_manifest
from uota.transports.wifi_tcp import WiFiTCPTransport
from uota.transports.serial import SerialOTATransport
from uota.cli import send_ota

# ── config ────────────────────────────────────────────────────────────────────

def _cfg():
    path = os.path.join(os.path.dirname(__file__), '..', 'ota.json')
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}  # no ota.json — hardware tests will be skipped at runtime

CFG         = _cfg()
DEVICE_IP   = CFG.get('hostname', '192.168.137.215')
DEVICE_PORT = CFG.get('port', 2018)
_default_port = 'COM3' if sys.platform == 'win32' else '/dev/ttyUSB0'
SERIAL_PORT = CFG.get('serialPort', _default_port)
SERIAL_BAUD = CFG.get('serialBaud', 115200)

SKIP_WIFI   = os.environ.get('SKIP_WIFI')
SKIP_SERIAL = os.environ.get('SKIP_SERIAL')

# ── transport factories ───────────────────────────────────────────────────────

def wifi():
    return WiFiTCPTransport(DEVICE_IP, DEVICE_PORT, timeout=10)

def serial():
    return SerialOTATransport(SERIAL_PORT, SERIAL_BAUD, timeout=15)

# ── helpers ───────────────────────────────────────────────────────────────────

def wait_for_wifi(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((DEVICE_IP, DEVICE_PORT), timeout=2)
            s.close()
            return True
        except OSError:
            pass
    return False


def send_cmd(transport, cmd):
    """Send a single-line command and return the one-line response."""
    with transport:
        transport.write_line(cmd)
        return transport.read_line()


def send_ls(transport, path='/'):
    """Send ls and collect all response lines (multi-line response)."""
    with transport:
        transport.write_line('ls ' + path)
        lines = []
        while True:
            line = transport.read_line()
            if line == '':
                break
            lines.append(line)
            # After a short idle with no more data the server is done
            # (for WiFi the connection closes; for serial we rely on timeout)
            if not _has_data(transport):
                break
    return lines


def _has_data(transport):
    """Return True if the transport has data waiting (best-effort)."""
    try:
        ser = getattr(transport, '_ser', None)
        if ser and hasattr(ser, 'in_waiting'):
            return ser.in_waiting > 0
        sock = getattr(transport, '_sock', None)
        if sock:
            import select
            r, _, _ = select.select([sock], [], [], 0.2)
            return bool(r)
    except Exception:
        pass
    return False


def _write_file(name, content):
    os.makedirs(os.path.dirname(name) if os.path.dirname(name) else '.', exist_ok=True)
    with open(name, 'wb') as f:
        f.write(content)

# ── skip decorators ───────────────────────────────────────────────────────────

class Skip(Exception):
    pass


def skip_if_no_wifi(fn):
    def w():
        if SKIP_WIFI:
            raise Skip('SKIP_WIFI set')
        if not wait_for_wifi(timeout=5):
            raise Skip('WiFi OTA server unreachable (' + DEVICE_IP + ')')
        fn()
    w.__name__ = fn.__name__
    return w


def skip_if_no_serial(fn):
    def w():
        if SKIP_SERIAL:
            raise Skip('SKIP_SERIAL set')
        if not os.path.exists(SERIAL_PORT):
            raise Skip('Serial port not found: ' + SERIAL_PORT)
        fn()
    w.__name__ = fn.__name__
    return w

# ── serial tests ──────────────────────────────────────────────────────────────

@skip_if_no_serial
def test_serial_ping():
    assert send_cmd(serial(), 'ping') == 'pong'


@skip_if_no_serial
def test_serial_version():
    resp = send_cmd(serial(), 'version')
    assert 'version' in json.loads(resp)


@skip_if_no_serial
def test_serial_ls_root():
    lines = send_ls(serial(), '/')
    joined = '\n'.join(lines)
    assert 'boot.py' in joined, 'boot.py missing: ' + repr(lines)
    # OTA library lives in /lib/ (new layout) or at root (legacy layout)
    ota_present = 'ota.py' in joined or 'lib' in joined
    assert ota_present, 'OTA library (ota.py or lib/) missing: ' + repr(lines)


@skip_if_no_serial
def test_serial_get_file():
    t = serial()
    with t:
        t.write_line('get /ota.json')
        size = int(t.read_line().strip())
        assert size > 0
        data = t.read_exact(size)
        cfg = json.loads(data)
        assert 'port' in cfg


@skip_if_no_serial
def test_serial_ota_single_file():
    """Push one file over serial and verify it appears on the device."""
    content = b'# hw serial test\nSERIAL_OK = True\n'
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _write_file('hw_serial.py', content)
        manifest = build_manifest(['hw_serial.py'], version='hw-serial')
        files = {p: p for p in manifest['files']}
        send_ota(serial(), files, manifest)

    time.sleep(3)   # device reboots after OTA

    lines = send_ls(serial(), '/')
    assert 'hw_serial.py' in '\n'.join(lines), 'hw_serial.py missing after OTA'

    # Read it back and verify content
    t = serial()
    with t:
        t.write_line('get /hw_serial.py')
        size = int(t.read_line().strip())
        got = t.read_exact(size)
    assert got == content, repr(got)

    # Clean up
    send_cmd(serial(), 'rm /hw_serial.py')


@skip_if_no_serial
def test_serial_ota_version_persisted():
    """Version from manifest is saved and readable after serial OTA."""
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _write_file('vtest.py', b'pass\n')
        manifest = build_manifest(['vtest.py'], version='9.9.9-serial')
        files = {p: p for p in manifest['files']}
        send_ota(serial(), files, manifest)

    time.sleep(3)

    resp = send_cmd(serial(), 'version')
    assert json.loads(resp)['version'] == '9.9.9-serial', resp

    send_cmd(serial(), 'rm /vtest.py')


@skip_if_no_serial
def test_serial_wipe_keeps_ota_lib():
    """wipe removes user files but keeps the OTA library."""
    send_cmd(serial(), 'wipe')
    time.sleep(2)

    root_lines = send_ls(serial(), '/')
    root_joined = '\n'.join(root_lines)

    # New /lib/ layout (post-bootstrap): OTA files live in /lib/
    if 'lib' in root_joined:
        lib_lines  = send_ls(serial(), '/lib')
        lib_joined = '\n'.join(lib_lines)
        assert 'ota.py' in lib_joined or 'ota.mpy' in lib_joined, \
            'ota.py missing from /lib after wipe: ' + repr(lib_lines)
        assert 'boot_guard' in lib_joined, \
            'boot_guard missing from /lib after wipe: ' + repr(lib_lines)
    else:
        # Legacy root-level layout (pre-bootstrap)
        assert 'ota.py'        in root_joined, 'ota.py missing after wipe: '        + repr(root_lines)
        assert 'boot_guard.py' in root_joined, 'boot_guard.py missing after wipe: ' + repr(root_lines)
        assert 'transports'    in root_joined, 'transports missing after wipe: '    + repr(root_lines)

# ── wifi tests ────────────────────────────────────────────────────────────────

@skip_if_no_wifi
def test_wifi_ping():
    assert send_cmd(wifi(), 'ping') == 'pong'


@skip_if_no_wifi
def test_wifi_version():
    resp = send_cmd(wifi(), 'version')
    assert 'version' in json.loads(resp)


@skip_if_no_wifi
def test_wifi_ls_root():
    lines = send_ls(wifi(), '/')
    joined = '\n'.join(lines)
    assert 'ota.py'        in joined, repr(lines)
    assert 'boot_guard.py' in joined, repr(lines)


@skip_if_no_wifi
def test_wifi_get_file():
    t = wifi()
    with t:
        t.write_line('get /ota.json')
        size = int(t.read_line().strip())
        data = t.read_exact(size)
        cfg = json.loads(data)
        assert 'port' in cfg


@skip_if_no_wifi
def test_wifi_ota_single_file():
    """Push one file over WiFi, verify content after reboot."""
    content = b'# hw wifi test\nWIFI_OK = True\n'
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _write_file('hw_wifi.py', content)
        manifest = build_manifest(['hw_wifi.py'], version='hw-wifi')
        files = {p: p for p in manifest['files']}
        send_ota(wifi(), files, manifest)

    assert wait_for_wifi(timeout=20), 'Device did not come back after OTA'

    lines = send_ls(wifi(), '/')
    assert 'hw_wifi.py' in '\n'.join(lines), repr(lines)

    t = wifi()
    with t:
        t.write_line('get /hw_wifi.py')
        size = int(t.read_line().strip())
        got = t.read_exact(size)
    assert got == content, repr(got)

    assert json.loads(send_cmd(wifi(), 'version'))['version'] == 'hw-wifi'
    send_cmd(wifi(), 'rm /hw_wifi.py')


@skip_if_no_wifi
def test_wifi_reset_and_reconnect():
    """Device accepts reset and comes back up on WiFi."""
    send_cmd(wifi(), 'reset')
    assert wait_for_wifi(timeout=20), 'Did not reconnect after reset'

# ── runner ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    tests = [v for k, v in list(globals().items()) if k.startswith('test_')]
    passed = failed = skipped = 0
    for t in tests:
        try:
            t()
            print(f'  PASS  {t.__name__}')
            passed += 1
        except Skip as e:
            print(f'  SKIP  {t.__name__}: {e}')
            skipped += 1
        except Exception as e:
            import traceback
            print(f'  FAIL  {t.__name__}: {e}')
            traceback.print_exc()
            failed += 1
    print(f'\n{passed} passed, {skipped} skipped, {failed} failed')
    sys.exit(failed)
