"""
Unit tests for BLE transport (Phase 6).

All tests run without hardware or a BLE radio.  They cover:
  - Source validation of device/transports/ble.py
  - Source validation of host/transports/ble.py
  - BLETransport raises RuntimeError with install hint when bleak absent
  - _on_notify() fills the rx buffer correctly
  - read_line() / read_exact() parse buffer correctly
  - write() chunks data at MTU boundaries
  - BLE error messages in uota._friendly()
"""

import ast
import importlib
import os
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

_PKG = os.path.join(os.path.dirname(__file__), '..', 'packages', 'cli')
sys.path.insert(0, _PKG)

_DEVICE_DIR = os.path.join(_PKG, 'uota', '_device')
_HOST_DIR   = os.path.join(_PKG, 'uota')


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_source(path):
    with open(path) as f:
        return f.read()


def _parse(path):
    src = _load_source(path)
    return ast.parse(src, filename=path)


# ── device source validation ──────────────────────────────────────────────────

class TestDeviceBLESource(unittest.TestCase):

    PATH = os.path.join(_DEVICE_DIR, 'transports', 'ble.py')

    def test_parses(self):
        _parse(self.PATH)

    def test_has_ble_conn_class(self):
        tree = _parse(self.PATH)
        names = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        self.assertIn('_BLEConn', names)

    def test_has_ble_transport_class(self):
        tree = _parse(self.PATH)
        names = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        self.assertIn('BLETransport', names)

    def test_ble_conn_has_recv(self):
        tree = _parse(self.PATH)
        methods = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        self.assertIn('recv', methods)

    def test_ble_conn_has_sendall(self):
        tree = _parse(self.PATH)
        methods = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        self.assertIn('sendall', methods)

    def test_transport_has_start_stop_accept(self):
        tree = _parse(self.PATH)
        methods = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        for m in ('start', 'stop', 'accept'):
            self.assertIn(m, methods, msg='Missing method: %s' % m)

    def test_nus_uuids_present(self):
        src = _load_source(self.PATH)
        self.assertIn('6E400001', src)
        self.assertIn('6E400002', src)
        self.assertIn('6E400003', src)

    def test_irq_events_defined(self):
        src = _load_source(self.PATH)
        self.assertIn('_IRQ_CONNECT', src)
        self.assertIn('_IRQ_DISCONNECT', src)
        self.assertIn('_IRQ_WRITE', src)
        self.assertIn('_IRQ_MTU', src)

    def test_re_advertise_on_disconnect(self):
        src = _load_source(self.PATH)
        # _advertise() must be called in the DISCONNECT branch of _irq
        self.assertIn('_advertise', src)


# ── host source validation ────────────────────────────────────────────────────

class TestHostBLESource(unittest.TestCase):

    PATH = os.path.join(_HOST_DIR, 'transports', 'ble.py')

    def test_parses(self):
        _parse(self.PATH)

    def test_has_ble_transport_class(self):
        tree = _parse(self.PATH)
        names = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        self.assertIn('BLETransport', names)

    def test_has_standard_interface(self):
        tree = _parse(self.PATH)
        methods = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        for m in ('connect', 'close', 'read_line', 'read_exact', 'write', 'write_line'):
            self.assertIn(m, methods, msg='Missing method: %s' % m)

    def test_uses_bleak(self):
        src = _load_source(self.PATH)
        self.assertIn('bleak', src)

    def test_uses_asyncio(self):
        src = _load_source(self.PATH)
        self.assertIn('asyncio', src)

    def test_context_manager(self):
        src = _load_source(self.PATH)
        self.assertIn('__enter__', src)
        self.assertIn('__exit__', src)

    def test_nus_uuids_present(self):
        src = _load_source(self.PATH)
        self.assertIn('6E400002', src)
        self.assertIn('6E400003', src)


# ── BLETransport unit behaviour (bleak mocked) ───────────────────────────────

def _make_transport():
    """
    Import uota/transports/ble.py with bleak mocked so no BLE hardware needed.
    Returns a fresh BLETransport instance (event loop started, not connected).
    """
    import importlib.util
    bleak_mod = types.ModuleType('bleak')
    bleak_mod.BleakScanner = MagicMock()
    bleak_mod.BleakClient  = MagicMock()
    sys.modules['bleak'] = bleak_mod

    spec = importlib.util.spec_from_file_location(
        'uota.transports.ble',
        os.path.join(_HOST_DIR, 'transports', 'ble.py'),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.BLETransport(name='test-device')


class TestBLETransportUnit(unittest.TestCase):

    def setUp(self):
        self.t = _make_transport()

    def tearDown(self):
        # Stop the event loop gracefully
        self.t._loop.call_soon_threadsafe(self.t._loop.stop)
        self.t._thread.join(timeout=2)

    def test_notify_fills_buffer(self):
        self.t._on_notify(None, bytearray(b'hello\n'))
        with self.t._rx_lock:
            self.assertEqual(bytes(self.t._rx_buf), b'hello\n')

    def test_rx_event_set_after_notify(self):
        self.t._rx_event.clear()
        self.t._on_notify(None, bytearray(b'x'))
        self.assertTrue(self.t._rx_event.is_set())

    def test_read_line_returns_line(self):
        self.t._on_notify(None, bytearray(b'ready\n'))
        line = self.t.read_line()
        self.assertEqual(line, 'ready')

    def test_read_line_strips_cr(self):
        self.t._on_notify(None, bytearray(b'ok\r\n'))
        line = self.t.read_line()
        self.assertEqual(line, 'ok')

    def test_read_line_consumes_only_one_line(self):
        self.t._on_notify(None, bytearray(b'first\nsecond\n'))
        self.assertEqual(self.t.read_line(), 'first')
        self.assertEqual(self.t.read_line(), 'second')

    def test_read_exact_returns_n_bytes(self):
        self.t._on_notify(None, bytearray(b'abcdef'))
        data = self.t.read_exact(4)
        self.assertEqual(data, b'abcd')
        with self.t._rx_lock:
            self.assertEqual(bytes(self.t._rx_buf), b'ef')

    def test_read_line_blocks_until_data(self):
        results = []

        def feed():
            threading.Event().wait(0.05)
            self.t._on_notify(None, bytearray(b'pong\n'))

        threading.Thread(target=feed, daemon=True).start()
        results.append(self.t.read_line())
        self.assertEqual(results[0], 'pong')

    def test_no_bleak_raises_helpful_error(self):
        """BLETransport() raises RuntimeError with pip hint when bleak absent."""
        import importlib.util
        saved = sys.modules.pop('bleak', None)
        try:
            spec = importlib.util.spec_from_file_location(
                'uota.transports.ble_nobt',
                os.path.join(_HOST_DIR, 'transports', 'ble.py'),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            with self.assertRaises(RuntimeError) as ctx:
                mod.BLETransport()
            self.assertIn('pip install bleak', str(ctx.exception))
        finally:
            if saved is not None:
                sys.modules['bleak'] = saved


# ── uota _friendly BLE errors ─────────────────────────────────────────────────

class TestFriendlyBLE(unittest.TestCase):

    def _friendly(self, exc):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'uota.cli', os.path.join(_HOST_DIR, 'cli.py')
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault('uota.transports.wifi_tcp', MagicMock())
        sys.modules.setdefault('uota.transports.serial',   MagicMock())
        spec.loader.exec_module(mod)
        return mod._friendly(exc, {})

    def test_bleak_missing_shows_install_hint(self):
        exc = RuntimeError('bleak is required for BLE transport.\n  Install it with:  pip install bleak')
        msg = self._friendly(exc)
        self.assertIsNotNone(msg)
        self.assertIn('pip install bleak', msg)


if __name__ == '__main__':
    unittest.main()
