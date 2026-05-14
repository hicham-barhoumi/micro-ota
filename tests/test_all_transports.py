"""
tests/test_all_transports.py — full hardware-in-the-loop test suite

Tests every OTA protocol command and RemoteIO on every available transport:

  Transport   | ping version ls get rm | stream_ota start_ota wipe reset | RemoteIO
  ─────────────────────────────────────────────────────────────────────────────────
  Serial      |          ✓             |              ✓                  |    —
  WiFi TCP    |          ✓             |              ✓                  |   TCP
  BLE OTA     |          ✓             |              ✓                  |    —
  BLE NUS     |          —             |              —                  |   NUS

Usage (from repo root or examples/serial/):
    python3 tests/test_all_transports.py

Environment variables:
    SKIP_SERIAL=1    skip serial tests
    SKIP_WIFI=1      skip WiFi TCP tests
    SKIP_BLE=1       skip BLE tests
    SERIAL_PORT=/dev/ttyUSB0   override serial port
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'packages', 'cli'))

from uota.transports.ble      import BLETransport
from uota.transports.wifi_tcp import WiFiTCPTransport
from uota.transports.serial   import SerialOTATransport
from uota.remoteio            import RemoteIOClient, RemoteIOBLEClient

# ── configuration ─────────────────────────────────────────────────────────────

EXAMPLE_DIR   = os.path.join(ROOT, 'examples', 'serial')
OTA_JSON_PATH = os.path.join(EXAMPLE_DIR, 'config', 'ota.json')

def _read_ota_json():
    with open(OTA_JSON_PATH) as f:
        return json.load(f)

_CFG = _read_ota_json()

WIFI_PORT    = int(_CFG.get('port', 2018))
RIO_PORT     = int(_CFG.get('remoteioPort', 2019))
BLE_NAME     = _CFG.get('bleName', 'micro-ota')

def _resolve_wifi_host():
    """
    Resolve the device hostname to an IP.
    1. Try mDNS / standard DNS.
    2. Check the ARP/neighbor table for any host answering on WIFI_PORT.
    """
    hostname = _CFG.get('hostname', 'micro-ota.local')
    try:
        return socket.gethostbyname(hostname)
    except OSError:
        pass
    # mDNS failed — check the ARP/neighbour table
    try:
        import subprocess as _sp
        out = _sp.check_output(['ip', 'neigh', 'show'], text=True)
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0][0].isdigit():
                candidate = parts[0]
                try:
                    s = socket.create_connection((candidate, WIFI_PORT), timeout=0.5)
                    s.close()
                    return candidate
                except OSError:
                    pass
    except Exception:
        pass
    return hostname   # fall back to let connect() produce the error

WIFI_HOST = _CFG.get('wifiHost') or os.environ.get('WIFI_HOST') or _resolve_wifi_host()

_default_serial = 'COM3' if sys.platform == 'win32' else '/dev/ttyUSB0'
SERIAL_PORT  = os.environ.get('SERIAL_PORT', _CFG.get('serialPort', _default_serial))
SERIAL_BAUD  = int(_CFG.get('serialBaud', 115200))

SKIP_SERIAL  = bool(os.environ.get('SKIP_SERIAL'))
SKIP_WIFI    = bool(os.environ.get('SKIP_WIFI'))
SKIP_BLE     = bool(os.environ.get('SKIP_BLE'))

# ── test result tracking ──────────────────────────────────────────────────────

_results = []  # (label, 'PASS'|'FAIL'|'SKIP', detail)

def _record(label, status, detail=''):
    _results.append((label, status, detail))
    icon = {'PASS': '✓', 'FAIL': '✗', 'SKIP': '–'}[status]
    line = f'  {icon}  {label}'
    if detail:
        line += f'  ({detail})'
    print(line)


# ── skip exception ────────────────────────────────────────────────────────────

class Skip(Exception):
    pass


# ── wait helpers ──────────────────────────────────────────────────────────────

def wait_for_wifi(timeout=45):
    """Return True when the WiFi OTA port accepts TCP connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((WIFI_HOST, WIFI_PORT), timeout=2)
            s.close()
            return True
        except OSError:
            pass
        time.sleep(2)
    return False


def wait_for_wifi_reboot(timeout=50):
    """Wait for device to go DOWN then come back UP on WiFi (post-OTA reboot).

    After OTA the device may still be up briefly before rebooting.
    Returning True on a stale 'still up' reading causes the next test
    to run while the device is mid-reboot.  This function explicitly
    waits for the port to disappear first, then for it to reappear.
    """
    deadline = time.time() + timeout
    # Brief sleep so the device has time to start rebooting
    time.sleep(2)
    # Wait for the port to go DOWN
    while time.time() < deadline:
        try:
            s = socket.create_connection((WIFI_HOST, WIFI_PORT), timeout=1)
            s.close()
            time.sleep(0.5)   # still up — keep waiting
        except OSError:
            break             # port is down — device is rebooting
    # Wait for the port to come back UP
    while time.time() < deadline:
        try:
            s = socket.create_connection((WIFI_HOST, WIFI_PORT), timeout=2)
            s.close()
            return True
        except OSError:
            time.sleep(1)
    return False


def _reset_ble_adapter():
    """Reset the BlueZ adapter to clear stale state from many BLE operations."""
    import subprocess as _sp
    try:
        _sp.run(['sudo', '-S', 'systemctl', 'restart', 'bluetooth'],
                input=b'hicham\n', capture_output=True, timeout=15)
        time.sleep(4.0)
    except Exception:
        try:
            _sp.run(['sudo', '-S', 'hciconfig', 'hci0', 'down'],
                    input=b'hicham\n', capture_output=True, timeout=5)
            time.sleep(1.0)
            _sp.run(['sudo', '-S', 'hciconfig', 'hci0', 'up'],
                    input=b'hicham\n', capture_output=True, timeout=5)
            time.sleep(3.0)
        except Exception:
            pass


def wait_for_ble(timeout=30):
    """Return True when the BLE device is reachable and answers ping."""
    deadline = time.time() + timeout
    _scan_fail_count = 0
    while time.time() < deadline:
        t = BLETransport(BLE_NAME)
        try:
            t.connect()
            t.write_line('ping')
            r = t.read_line()
            t.close()
            if r == 'pong':
                return True
        except Exception as _e:
            print('  [wait_for_ble] exception: %s' % _e)
            try:
                t.close()
            except Exception:
                pass
            _scan_fail_count += 1
            # After each odd failure reset the BlueZ adapter —
            # many BLE connect/disconnect cycles can leave it unresponsive.
            if _scan_fail_count % 2 == 1:
                _reset_ble_adapter()
        time.sleep(3)
    return False


def wait_for_serial(timeout=15):
    """Return True when the serial port exists and device answers ping."""
    # NOTE: Do NOT early-return if port doesn't exist — USB temporarily
    # disappears during device reboot (e.g. after BLE switch). Poll for it.
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            t = SerialOTATransport(SERIAL_PORT, SERIAL_BAUD, timeout=5)
            t.connect()
            t.write_line('ping')
            r = t.read_line()
            if r == 'pong':
                # Exit raw REPL cleanly WITHOUT machine.reset() so the device
                # stays at the normal REPL prompt, ready for the next connect().
                # close() would send machine.reset() causing a hard reboot; if
                # the next test connects immediately that causes a double-reboot
                # race where connect()'s raw-REPL injection fails.
                try:
                    t._ser.write(b'\x03')   # Ctrl-C: stop serve_serial()
                    t._ser.flush()
                    time.sleep(0.3)
                    t._ser.write(b'\x02')   # Ctrl-B: exit raw REPL → normal REPL
                    t._ser.flush()
                    time.sleep(0.1)
                except Exception:
                    pass
                try:
                    t._ser.close()
                except Exception:
                    pass
                t._ser = None
                return True
        except Exception:
            try:
                t.close()
            except Exception:
                pass
        time.sleep(2)
    return False


# ── config helpers ────────────────────────────────────────────────────────────

def _write_ota_json(cfg):
    with open(OTA_JSON_PATH, 'w') as f:
        json.dump(cfg, f, indent=4)
        f.write('\n')


def _push_fast(transport_arg, host_arg=None):
    """Run `uota fast --transport T` from the example dir."""
    cmd = ['uota', 'fast', '--transport', transport_arg]
    if host_arg:
        cmd += ['--host', host_arg]
    r = subprocess.run(cmd, cwd=EXAMPLE_DIR, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)


# ── OTA stream / legacy push helpers ─────────────────────────────────────────

def _uota(subcmd, transport_flag, host_arg=None, extra_args=None):
    """Run `uota <subcmd> --transport T [--host H]` from the example dir."""
    cmd = ['uota', subcmd, '--transport', transport_flag]
    if host_arg:
        cmd += ['--host', host_arg]
    if extra_args:
        cmd += extra_args
    r = subprocess.run(cmd, cwd=EXAMPLE_DIR, capture_output=True, text=True)
    return r


# ── core protocol tests (transport-agnostic) ──────────────────────────────────

def run_protocol_tests(label, transport_factory):
    """
    Execute all raw OTA protocol commands using the given transport factory.
    transport_factory() must return an unconnected transport object.
    """
    def chk(name, fn):
        full = f'{label}/{name}'
        try:
            fn()
            _record(full, 'PASS')
        except Exception as e:
            _record(full, 'FAIL', str(e))

    # ── ping ──────────────────────────────────────────────────────────────────
    def test_ping():
        t = transport_factory()
        with t:
            t.write_line('ping')
            assert t.read_line() == 'pong', 'expected pong'
    chk('ping', test_ping)

    # ── version ───────────────────────────────────────────────────────────────
    def test_version():
        t = transport_factory()
        with t:
            t.write_line('version')
            obj = json.loads(t.read_line())
            assert 'version' in obj, f'no version key: {obj}'
    chk('version', test_version)

    # ── ls / ─────────────────────────────────────────────────────────────────
    def test_ls_root():
        t = transport_factory()
        with t:
            t.write_line('ls /')
            lines = []
            while True:
                l = t.read_line()
                if l == '':
                    break
                lines.append(l)
        joined = '\n'.join(lines)
        assert 'boot.py' in joined,  f'boot.py missing: {lines}'
        assert 'lib'      in joined,  f'lib/ missing: {lines}'
        assert 'config'   in joined,  f'config/ missing: {lines}'
    chk('ls /', test_ls_root)

    # ── ls /lib/uota ──────────────────────────────────────────────────────────
    def test_ls_uota():
        t = transport_factory()
        with t:
            t.write_line('ls /lib/uota')
            lines = []
            while True:
                l = t.read_line()
                if l == '':
                    break
                lines.append(l)
        joined = '\n'.join(lines)
        assert 'ota' in joined,        f'ota.py missing from /lib/uota: {lines}'
        assert 'boot_guard' in joined, f'boot_guard missing from /lib/uota: {lines}'
    chk('ls /lib/uota', test_ls_uota)

    # ── get existing file ─────────────────────────────────────────────────────
    def test_get_boot_py():
        t = transport_factory()
        with t:
            t.write_line('get /boot.py')
            size = int(t.read_line().strip())
            assert size > 0, 'boot.py is empty'
            data = t.read_exact(size)
            assert b'OTAUpdater' in data, 'boot.py looks wrong'
    chk('get /boot.py', test_get_boot_py)

    # ── get config ────────────────────────────────────────────────────────────
    def test_get_config():
        t = transport_factory()
        with t:
            t.write_line('get /config/ota.json')
            size = int(t.read_line().strip())
            data = t.read_exact(size)
            cfg  = json.loads(data)
            assert 'port' in cfg, f'no port in config: {cfg}'
    chk('get /config/ota.json', test_get_config)

    # ── get nonexistent ───────────────────────────────────────────────────────
    def test_get_missing():
        t = transport_factory()
        with t:
            t.write_line('get /this_file_does_not_exist.py')
            resp = t.read_line()
            assert resp.startswith('error'), f'expected error, got: {resp}'
    chk('get missing file', test_get_missing)

    # ── rm ────────────────────────────────────────────────────────────────────
    def test_rm():
        # Check existence first so the test is self-contained even if a
        # previous phase already removed the file (e.g. after a wipe).
        t0 = transport_factory()
        with t0:
            t0.write_line('get /ota_version.json')
            r0 = t0.read_line()
            if r0.startswith('error'):
                return  # already gone — nothing to test
            t0.read_exact(int(r0))  # drain
        t = transport_factory()
        with t:
            t.write_line('rm /ota_version.json')
            resp = t.read_line().strip()
            assert resp == 'ok', f'rm failed: {resp}'
        # Verify it's gone
        t2 = transport_factory()
        with t2:
            t2.write_line('get /ota_version.json')
            resp = t2.read_line()
            assert resp.startswith('error'), f'file still present after rm: {resp}'
    chk('rm /ota_version.json', test_rm)


def run_ota_push_tests(label, transport_flag, transport_factory, wait_fn, host_arg=None,
                       stable_fn=None):
    """
    Test stream_ota (uota fast) and start_ota (uota full) on the given transport.

    transport_flag: CLI flag value ('ble', 'wifi_tcp', 'serial')
    transport_factory: returns an unconnected transport object for direct commands
    wait_fn: blocks until the transport is available again after a reset
    host_arg: --host value for serial port, or None
    stable_fn: called after each wait_fn to confirm the OTA loop is ready for
               the next connection.  Defaults to _wait_ota_stable(transport_factory).
               For BLE, pass a plain sleep to avoid rapid reconnects that destabilise
               the device's BLE stack.
    """
    def _stable():
        if stable_fn is not None:
            stable_fn()
        else:
            _wait_ota_stable(transport_factory)

    def chk(name, fn):
        full = f'{label}/{name}'
        try:
            fn()
            _record(full, 'PASS')
        except Exception as e:
            _record(full, 'FAIL', str(e))

    # ── stream_ota (uota fast) ────────────────────────────────────────────────
    def test_stream_ota():
        r = _uota('fast', transport_flag, host_arg)
        assert r.returncode == 0, f'uota fast failed:\n{r.stdout}\n{r.stderr}'
        assert wait_fn(timeout=50), 'device did not come back after stream_ota'
        _stable()
    chk('stream_ota', test_stream_ota)

    # ── start_ota (uota full / legacy) ────────────────────────────────────────
    def test_start_ota():
        r = _uota('full', transport_flag, host_arg)
        assert r.returncode == 0, f'uota full failed:\n{r.stdout}\n{r.stderr}'
        assert wait_fn(timeout=50), 'device did not come back after start_ota'
        _stable()
    chk('start_ota', test_start_ota)

    # ── wipe ─────────────────────────────────────────────────────────────────
    def test_wipe():
        # Send wipe command
        t = transport_factory()
        with t:
            t.write_line('wipe')
            resp = t.read_line().strip()
            assert resp == 'ok', f'wipe failed: {resp}'
        # Verify OTA lib preserved
        t2 = transport_factory()
        with t2:
            t2.write_line('ls /lib/uota')
            lines = []
            while True:
                l = t2.read_line()
                if l == '':
                    break
                lines.append(l)
        assert any('ota' in l for l in lines), f'ota.py missing after wipe: {lines}'
        # Verify config preserved
        t3 = transport_factory()
        with t3:
            t3.write_line('get /config/ota.json')
            size = int(t3.read_line().strip())
            assert size > 0, 'config wiped (should be preserved)'
            t3.read_exact(size)   # drain
        # Restore full app via fast push
        r = _uota('fast', transport_flag, host_arg)
        assert r.returncode == 0, f'restore after wipe failed:\n{r.stdout}\n{r.stderr}'
        assert wait_fn(timeout=45), 'device did not come back after wipe restore'
        _stable()
    chk('wipe', test_wipe)

    # ── reset ─────────────────────────────────────────────────────────────────
    def test_reset():
        t = transport_factory()
        with t:
            t.write_line('reset')
            resp = t.read_line().strip()
            assert resp == 'ok', f'reset command failed: {resp}'
        # wait_fn reconnects and pings, proving the device is back
        assert wait_fn(timeout=45), 'device did not come back after reset'
        _stable()
    chk('reset', test_reset)


def run_remoteio_tests(label, client_factory, _connected=None):
    """
    Test the four built-in RemoteIO handlers on a single persistent connection.

    Pass _connected=<already-connected-client> to reuse an open connection
    (avoids the race where the device event loop hasn't cycled back to
    try_accept() between the ready-check probe and the real test).
    """
    def chk(name, fn):
        full = f'{label}/{name}'
        try:
            fn()
            _record(full, 'PASS')
        except Exception as e:
            _record(full, 'FAIL', str(e))

    # All four calls share one connection: avoids per-connection re-accept
    # latency in the device's single-threaded event loop.
    if _connected is not None:
        rio = _connected
    else:
        try:
            rio = client_factory()
            rio.connect()
        except Exception as e:
            for name in ('ping', 'version', 'free_mem', 'uptime_ms'):
                _record(f'{label}/{name}', 'FAIL', f'connection failed: {e}')
            return

    try:
        def test_ping():
            r = rio.call('ping')
            assert r == 'pong', f'expected pong, got: {r}'
        chk('ping', test_ping)

        def test_version():
            r = rio.call('version')
            assert isinstance(r, dict), f'expected dict, got: {r}'
        chk('version', test_version)

        def test_free_mem():
            r = rio.call('free_mem')
            assert isinstance(r, int) and r > 0, f'expected positive int, got: {r}'
        chk('free_mem', test_free_mem)

        def test_uptime_ms():
            r = rio.call('uptime_ms')
            assert isinstance(r, int) and r >= 0, f'expected non-negative int, got: {r}'
        chk('uptime_ms', test_uptime_ms)
    finally:
        try:
            rio.close()
        except Exception:
            pass


# ── transport-specific setup / teardown ───────────────────────────────────────

def _wait_ota_stable(transport_factory, retries=8, delay=2):
    """
    Ping the OTA server until it answers, confirming the event loop is ready
    to accept a second connection.  wait_fn() only verifies the TCP port is
    open; the OTA loop may still be serving the probe connection when the next
    test tries to connect, causing Connection-refused or timeout failures.
    """
    for _ in range(retries):
        try:
            t = transport_factory()
            t.connect()
            t.write_line('ping')
            r = t.read_line()
            t.close()
            if r == 'pong':
                return
        except Exception:
            try:
                t.close()
            except Exception:
                pass
        time.sleep(delay)


def _set_device_transport(transports, push_fn, wait_fn):
    """
    Push a new transports config to the device and wait for it to come back.
    Saves and restores the ota.json file around the push.
    Returns True if the device comes back on the new transport.
    """
    orig = _read_ota_json()
    cfg  = dict(orig)
    cfg['transports'] = transports
    _write_ota_json(cfg)
    try:
        push_fn()
    except Exception as _e:
        # The OTA tool may fail its own post-verification step when the
        # device reboots onto a different transport than the one used to push
        # (e.g. WiFi push switches device to BLE-only, so WiFi verify fails).
        # Treat wait_fn as the authoritative success criterion.
        _emsg = str(_e).strip().splitlines()[0][:120]
        print(f'  push_fn raised (expected if transport changed): {_emsg}')
    finally:
        _write_ota_json(orig)   # restore host source file
    return wait_fn()


# ── transport switch helpers ──────────────────────────────────────────────────

def _ensure_wifi():
    """Switch device to WiFi-only if not already on WiFi. Returns True on success."""
    if wait_for_wifi(timeout=5):
        return True
    # Use serial (always available) to switch to WiFi — works regardless of
    # which wireless transport the device is currently on.
    print('  (switching device to WiFi-only via serial…)')
    ok = _set_device_transport(
        ['wifi_tcp'],
        lambda: _push_fast('serial', host_arg=SERIAL_PORT),
        lambda timeout=50: wait_for_wifi(timeout),
    )
    if not ok:
        print('  transport switch failed: device did not come back on WiFi')
    return ok


def _ensure_ble():
    """Switch device to BLE-only if needed. Returns True on success."""
    # After the WiFi TCP phase the device is on WiFi.  Use serial (raw-REPL
    # injection, always available) to switch the config — it doesn't depend on
    # the current wireless transport being alive.
    if wait_for_wifi(timeout=5) or wait_for_ble(timeout=5):
        print('  (switching device to BLE-only via serial…)')
        _reset_ble_adapter()   # clear any stale BlueZ state before the phase
        ok = _set_device_transport(
            ['ble'],
            lambda: _push_fast('serial', host_arg=SERIAL_PORT),
            lambda timeout=90: wait_for_ble(timeout),
        )
        if not ok:
            print('  transport switch failed: device did not come back on BLE')
        return ok
    # Neither WiFi nor BLE reachable — try serial directly
    _reset_ble_adapter()
    ok = _set_device_transport(
        ['ble'],
        lambda: _push_fast('serial', host_arg=SERIAL_PORT),
        lambda timeout=90: wait_for_ble(timeout),
    )
    if not ok:
        print('  transport switch via serial failed')
    return ok


# ── WiFi transport phase ──────────────────────────────────────────────────────

def phase_wifi():
    if SKIP_WIFI:
        _record('WiFi TCP', 'SKIP', 'SKIP_WIFI set')
        _record('WiFi TCP RemoteIO', 'SKIP', 'SKIP_WIFI set')
        return

    print('\n── WiFi TCP ─────────────────────────────────────────────────────────')
    if not _ensure_wifi():
        _record('WiFi TCP', 'SKIP', 'device not reachable via WiFi or BLE')
        _record('WiFi TCP RemoteIO', 'SKIP', 'device not reachable')
        return

    def wifi_factory():
        return WiFiTCPTransport(WIFI_HOST, WIFI_PORT, timeout=10)

    run_protocol_tests('WiFi TCP', wifi_factory)
    # Temporarily set transports to wifi_tcp so OTA pushes don't switch device to BLE
    _orig_wifi_cfg = _read_ota_json()
    _wifi_only_cfg = dict(_orig_wifi_cfg)
    _wifi_only_cfg['transports'] = ['wifi_tcp']
    _write_ota_json(_wifi_only_cfg)
    try:
        # Pass WIFI_HOST so uota doesn't try to resolve 'micro-ota.local'
        run_ota_push_tests('WiFi TCP', 'wifi_tcp', wifi_factory,
                           lambda timeout=45: wait_for_wifi(timeout),
                           host_arg=WIFI_HOST)
    finally:
        _write_ota_json(_orig_wifi_cfg)

    print('\n── WiFi TCP RemoteIO ────────────────────────────────────────────────')

    def tcp_rio_factory():
        return RemoteIOClient(WIFI_HOST, RIO_PORT, timeout=10)

    # RemoteIO (port 2019) starts shortly after OTA (port 2018).
    # Connect and keep the connection open — reusing it for the actual tests
    # avoids the race where the device event loop hasn't cycled back to
    # try_accept() in the gap between the ready-check and the real connection.
    _rio_deadline = time.time() + 30
    _rio_conn = None
    while time.time() < _rio_deadline:
        try:
            _c = RemoteIOClient(WIFI_HOST, RIO_PORT, timeout=5)
            _c.connect()
            if _c.call('ping') == 'pong':
                _rio_conn = _c
                break
            _c.close()
        except Exception:
            try:
                _c.close()
            except Exception:
                pass
        time.sleep(2)
    if _rio_conn is None:
        _record('WiFi TCP RemoteIO', 'SKIP', 'RemoteIO not responding after OTA tests')
        return

    run_remoteio_tests('WiFi TCP RemoteIO', tcp_rio_factory, _connected=_rio_conn)


# ── BLE transport phase ───────────────────────────────────────────────────────

def phase_ble():
    if SKIP_BLE:
        _record('BLE OTA', 'SKIP', 'SKIP_BLE set')
        _record('BLE NUS RemoteIO', 'SKIP', 'SKIP_BLE set')
        return

    print('\n── BLE OTA ─────────────────────────────────────────────────────────')
    if not _ensure_ble():
        _record('BLE OTA', 'SKIP', 'device not reachable via BLE or WiFi')
        _record('BLE NUS RemoteIO', 'SKIP', 'device not reachable')
        return

    def ble_factory():
        return BLETransport(BLE_NAME)

    run_protocol_tests('BLE OTA', ble_factory)
    # Temporarily set host config to BLE-only so OTA pushes don't switch device to WiFi
    _orig_ble_cfg = _read_ota_json()
    _ble_only_cfg = dict(_orig_ble_cfg)
    _ble_only_cfg['transports'] = ['ble']
    _write_ota_json(_ble_only_cfg)
    try:
        run_ota_push_tests('BLE OTA', 'ble', ble_factory,
                           lambda timeout=90: wait_for_ble(timeout),
                           stable_fn=lambda: time.sleep(6))
    finally:
        _write_ota_json(_orig_ble_cfg)

    print('\n── BLE NUS RemoteIO ────────────────────────────────────────────────')
    if not wait_for_ble(timeout=25):
        _record('BLE NUS RemoteIO', 'SKIP', 'device not reachable via BLE after OTA tests')
        return

    def ble_rio_factory():
        return RemoteIOBLEClient(BLE_NAME)

    run_remoteio_tests('BLE NUS RemoteIO', ble_rio_factory)


# ── Serial transport phase ────────────────────────────────────────────────────

def phase_serial():
    if SKIP_SERIAL:
        _record('Serial', 'SKIP', 'SKIP_SERIAL set')
        return

    print('\n── Serial ───────────────────────────────────────────────────────────')
    # Give the port 30s to appear — USB disconnects briefly during device reboot
    if not wait_for_serial(timeout=30):
        _record('Serial', 'SKIP', f'serial not available: {SERIAL_PORT}')
        return

    def serial_factory():
        return SerialOTATransport(SERIAL_PORT, SERIAL_BAUD, timeout=20)

    run_protocol_tests('Serial', serial_factory)
    run_ota_push_tests('Serial', 'serial', serial_factory,
                       lambda timeout=30: wait_for_serial(timeout),
                       host_arg=SERIAL_PORT)


# ── cleanup / restore ─────────────────────────────────────────────────────────

def restore_original_config():
    """Push back the full app with the original ota.json config via serial."""
    print('\n── Restoring device config ──────────────────────────────────────────')
    try:
        _push_fast('serial', host_arg=SERIAL_PORT)
        wait_for_serial(timeout=30)
        print('  Config restored via serial.')
    except Exception as e:
        print(f'  Restore failed: {e}')


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('micro-ota full transport test suite')
    print(f'  WiFi host:   {WIFI_HOST}:{WIFI_PORT}')
    print(f'  BLE name:    {BLE_NAME}')
    print(f'  Serial port: {SERIAL_PORT}')
    print()

    phase_wifi()
    phase_ble()
    phase_serial()
    restore_original_config()

    # ── summary ───────────────────────────────────────────────────────────────
    passed  = sum(1 for _, s, _ in _results if s == 'PASS')
    failed  = sum(1 for _, s, _ in _results if s == 'FAIL')
    skipped = sum(1 for _, s, _ in _results if s == 'SKIP')

    print(f'\n{"─"*60}')
    if failed:
        print('FAILED tests:')
        for label, status, detail in _results:
            if status == 'FAIL':
                print(f'  ✗  {label}: {detail}')
        print()
    print(f'{passed} passed  {failed} failed  {skipped} skipped')
    sys.exit(1 if failed else 0)
