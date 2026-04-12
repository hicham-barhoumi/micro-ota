"""
Tests for host/transports/serial.py SerialOTATransport.

Uses MockSerial (two in-process queues) to avoid real hardware.

connect() handshake tests use a full MockSerialDevice thread.
OTA protocol tests bypass connect() (set _ser directly) and use a lighter
MockOTADevice that just speaks the OTA protocol without the REPL preamble.
"""

import json
import os
import queue
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'packages', 'cli'))

from uota.transports.serial import SerialOTATransport, _INLINE_SERVER
from uota.cli import send_ota
from uota.manifest import build as build_manifest


# ── mock serial port ──────────────────────────────────────────────────────────

class MockSerial:
    """
    Two-queue in-process serial port.
    host.write()  → _h2d → device reads
    device sends  → _d2h → host.read()
    """

    def __init__(self, timeout=5):
        self._h2d  = queue.Queue()
        self._d2h  = queue.Queue()
        self._rbuf = bytearray()
        self.is_open  = True
        self._timeout = timeout

    # ── pyserial-compatible API ───────────────────────────────────────────────

    def write(self, data):
        self._h2d.put(bytes(data))

    def flush(self):
        pass

    def read(self, n):
        """
        Block until at least 1 byte arrives, then return up to n bytes.
        Matches real pyserial: read(n) returns AT MOST n bytes.
        """
        if not self._rbuf:
            try:
                self._rbuf.extend(self._d2h.get(timeout=self._timeout))
            except queue.Empty:
                return b''
        while len(self._rbuf) < n and not self._d2h.empty():
            try:
                self._rbuf.extend(self._d2h.get_nowait())
            except queue.Empty:
                break
        out = bytes(self._rbuf[:n])
        self._rbuf = self._rbuf[n:]
        return out

    def reset_input_buffer(self):
        self._rbuf.clear()
        while not self._d2h.empty():
            try:
                self._d2h.get_nowait()
            except queue.Empty:
                break

    def close(self):
        self.is_open = False

    # ── device-side helpers ───────────────────────────────────────────────────

    def dev_send(self, data):
        self._d2h.put(data if isinstance(data, bytes) else data.encode())

    def dev_readline(self):
        """Device reads one \\n-terminated line from what the host wrote."""
        buf = bytearray()
        while True:
            chunk = self._h2d.get(timeout=5)
            buf.extend(chunk)
            if b'\n' in buf:
                idx = buf.index(b'\n')
                line = bytes(buf[:idx]).decode().strip()
                rest = bytes(buf[idx + 1:])
                if rest:
                    self._h2d.queue.appendleft(rest)
                return line

    def dev_read_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            buf.extend(self._h2d.get(timeout=5))
        return bytes(buf[:n])

    def dev_read_until(self, sentinel, max_buf=65536):
        buf = bytearray()
        while not buf.endswith(sentinel):
            buf.extend(self._h2d.get(timeout=5))
            if len(buf) > max_buf:
                raise RuntimeError('dev_read_until: overflow')
        return bytes(buf)


# ── mock: full device with raw REPL handshake ─────────────────────────────────

class MockSerialDevice:
    """
    Simulates ESP32 over serial:
      Phase 1 – raw REPL handshake
      Phase 2 – OTA protocol
    """

    def __init__(self, mock):
        self._s   = mock
        self.commands       = []
        self.files_received = {}
        self.manifest       = None
        self.error          = None
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()
        return self

    def join(self, timeout=5):
        self._t.join(timeout)

    def _run(self):
        try:
            self._handshake()
            self._serve()
        except queue.Empty:
            pass   # normal: host closed session without sending more commands
        except Exception as e:
            self.error = e

    def _handshake(self):
        self._s.dev_read_until(b'\x01')          # wait for Ctrl-A
        self._s.dev_send(b'raw REPL; CTRL-B to exit\r\n>')
        self._s.dev_read_until(b'\x04')          # drain code + Ctrl-D
        self._s.dev_send(b'OK')

    def _serve(self):
        while True:
            line = self._s.dev_readline()
            self.commands.append(line)
            if line == 'start_ota':
                self._s.dev_send(b'ready\n')
                self._serve_ota()
                return
            elif line == 'ping':
                self._s.dev_send(b'pong\n')
            elif line == 'version':
                self._s.dev_send(b'{"version":"1.0.0"}\n')
            else:
                self._s.dev_send(b'unknown\n')

    def _serve_ota(self):
        while True:
            line = self._s.dev_readline()
            self.commands.append(line)
            if line == 'end_ota':
                self._s.dev_send(b'ok\n')
                return
            if line == 'abort':
                self._s.dev_send(b'aborted\n')
                return
            parts = line.split(' ', 1)
            cmd, arg = parts[0], (parts[1] if len(parts) > 1 else '')
            if cmd == 'manifest':
                data = self._s.dev_read_exact(int(arg))
                self.manifest = json.loads(data)
                self._s.dev_send(b'ok\n')
            elif cmd == 'file':
                meta = arg.split(';')
                name, size = meta[0], int(meta[1])
                self.files_received[name] = self._s.dev_read_exact(size)
                self._s.dev_send(b'ok\n')
            else:
                self._s.dev_send(b'unknown\n')


# ── mock: OTA-only device (no REPL handshake) ─────────────────────────────────

class MockOTADevice:
    """
    Speaks only the OTA protocol. Used when connect() is bypassed.
    The first command it expects is 'start_ota'.
    """

    def __init__(self, mock):
        self._s             = mock
        self.commands       = []
        self.files_received = {}
        self.manifest       = None
        self.error          = None
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()
        return self

    def join(self, timeout=5):
        self._t.join(timeout)

    def _run(self):
        try:
            self._serve()
        except queue.Empty:
            pass
        except Exception as e:
            self.error = e

    def _serve(self):
        MockSerialDevice._serve(self)   # reuse same logic

    def _serve_ota(self):
        MockSerialDevice._serve_ota(self)


# ── patch helper (for handshake test only) ────────────────────────────────────

def _run_with_mock(mock, fn):
    """Replace serial.Serial in the transport module, call fn(), restore."""
    import uota.transports.serial as mod

    class _Fake:
        def Serial(_, port, baud, timeout=None):
            mock._timeout = timeout or 5
            return mock

    orig = mod.serial
    mod.serial = _Fake()
    try:
        fn()
    finally:
        mod.serial = orig


# ── helpers ───────────────────────────────────────────────────────────────────

def _serial_transport_with_mock(mock):
    """Return SerialOTATransport whose _ser is set to mock (bypasses connect)."""
    t = SerialOTATransport('/dev/ttyUSB0')
    t._ser = mock
    return t


def _make_files(specs):
    """Write {rel_path: bytes} into the current directory."""
    for name, content in specs.items():
        os.makedirs(os.path.dirname(name) if os.path.dirname(name) else '.', exist_ok=True)
        with open(name, 'wb') as f:
            f.write(content)


# ── tests: raw REPL handshake ─────────────────────────────────────────────────

def test_connect_handshake():
    """connect() successfully completes the raw REPL handshake."""
    mock = MockSerial(timeout=10)
    device = MockSerialDevice(mock).start()

    connected = []
    def run():
        t = SerialOTATransport('/dev/ttyUSB0', timeout=10)
        t.connect()
        connected.append(True)

    _run_with_mock(mock, run)
    device.join(timeout=10)

    assert device.error is None, str(device.error)
    assert connected, 'connect() raised unexpectedly'


# ── tests: OTA protocol over serial ──────────────────────────────────────────

def test_single_file_ota_over_serial():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _make_files({'main.py': b'print("hello")'})
        manifest = build_manifest(['*.py'], version='1.0.0')
        files = {p: p for p in manifest['files']}

        mock   = MockSerial()
        device = MockOTADevice(mock).start()
        transport = _serial_transport_with_mock(mock)
        send_ota(transport, files, manifest)
        device.join()

        assert device.error is None, str(device.error)
        assert device.files_received.get('main.py') == b'print("hello")'
        assert 'end_ota' in device.commands


def test_multiple_files_ota_over_serial():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        specs = {'main.py': b'x=1', 'lib/helper.py': b'y=2'}
        os.makedirs('lib', exist_ok=True)
        _make_files(specs)
        manifest = build_manifest(['*.py', 'lib/**'], version='1.0.0')
        files = {p: p for p in manifest['files']}

        mock   = MockSerial()
        device = MockOTADevice(mock).start()
        send_ota(_serial_transport_with_mock(mock), files, manifest)
        device.join()

        assert device.error is None, str(device.error)
        for name, content in specs.items():
            assert device.files_received[name] == content


def test_manifest_sent_before_files_serial():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _make_files({'main.py': b'x=1'})
        manifest = build_manifest(['*.py'], version='1.0.0')
        files = {p: p for p in manifest['files']}

        mock   = MockSerial()
        device = MockOTADevice(mock).start()
        send_ota(_serial_transport_with_mock(mock), files, manifest)
        device.join()

        cmds = device.commands
        mi = next(i for i, c in enumerate(cmds) if c.startswith('manifest'))
        fi = next(i for i, c in enumerate(cmds) if c.startswith('file'))
        assert mi < fi


def test_end_ota_sent_serial():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _make_files({'main.py': b'x=1'})
        manifest = build_manifest(['*.py'], version='1.0.0')
        files = {p: p for p in manifest['files']}

        mock   = MockSerial()
        device = MockOTADevice(mock).start()
        send_ota(_serial_transport_with_mock(mock), files, manifest)
        device.join()

        assert 'end_ota' in device.commands


def test_version_in_manifest_serial():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _make_files({'main.py': b'x=1'})
        manifest = build_manifest(['*.py'], version='7.8.9')
        files = {p: p for p in manifest['files']}

        mock   = MockSerial()
        device = MockOTADevice(mock).start()
        send_ota(_serial_transport_with_mock(mock), files, manifest)
        device.join()

        assert device.manifest['version'] == '7.8.9'


# ── tests: transport primitives ───────────────────────────────────────────────

def test_read_line_strips_cr():
    mock = MockSerial()
    mock.dev_send(b'hello\r\nworld\n')
    t = _serial_transport_with_mock(mock)
    assert t.read_line() == 'hello'
    assert t.read_line() == 'world'


def test_read_exact_reassembles_chunks():
    mock = MockSerial()
    data = bytes(range(256))
    for i in range(0, len(data), 16):
        mock.dev_send(data[i:i + 16])
    t = _serial_transport_with_mock(mock)
    assert t.read_exact(256) == data


def test_write_line_appends_newline():
    mock = MockSerial()
    t = _serial_transport_with_mock(mock)
    t.write_line('ping')
    assert mock._h2d.get(timeout=1) == b'ping\n'


# ── tests: inline server code ─────────────────────────────────────────────────

def test_inline_server_is_valid_python():
    compile(_INLINE_SERVER, '<inline>', 'exec')


def test_inline_server_contains_handle_loop():
    assert 'while True' in _INLINE_SERVER
    assert '_h(_C())' in _INLINE_SERVER or '_handle' in _INLINE_SERVER


if __name__ == '__main__':
    tests = [v for k, v in list(globals().items()) if k.startswith('test_')]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f'  PASS  {t.__name__}')
            passed += 1
        except Exception as e:
            import traceback
            print(f'  FAIL  {t.__name__}: {e}')
            traceback.print_exc()
            failed += 1
    print(f'\n{passed} passed, {failed} failed')
    sys.exit(failed)
