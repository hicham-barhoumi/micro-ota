"""
Tests for the host-side OTA protocol (host/uota.py send_ota).

A mock TCP server runs in a thread and simulates device responses,
letting us verify the full push flow without real hardware.
"""

import json
import os
import socket
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'packages', 'cli'))
from uota.manifest import build as build_manifest, to_json as manifest_to_json
from uota.transports.wifi_tcp import WiFiTCPTransport
from uota.cli import send_ota


# ── mock device server ────────────────────────────────────────────────────────

class MockDevice:
    """
    Listens on a random local port and handles one OTA session.
    Records all commands received so tests can assert on them.
    """

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.commands = []      # list of commands received
        self.files_received = {}  # rel_path → bytes
        self._error_on = None   # force sha256_mismatch on this filename
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def join(self, timeout=5):
        self._thread.join(timeout)

    def fail_file(self, filename):
        """Make the server return sha256_mismatch for this filename."""
        self._error_on = filename

    def _readline(self, conn):
        buf = bytearray()
        while True:
            c = conn.recv(1)
            if not c or c == b'\n':
                return buf.decode().strip()
            if c != b'\r':
                buf.extend(c)

    def _send(self, conn, msg):
        conn.sendall(msg.encode() if isinstance(msg, str) else msg)

    def _read_exact(self, conn, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(min(4096, n - len(buf)))
            if not chunk:
                raise OSError('connection closed')
            buf.extend(chunk)
        return bytes(buf)

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
            self.sock.close()
            with conn:
                self._handle_session(conn)
        except Exception as e:
            self.error = e

    def _handle_session(self, conn):
        # Auth handshake: consume password line and reply ok (no real check in tests)
        self._readline(conn)
        self._send(conn, 'ok\n')

        # Each command arrives on its own connection in real device,
        # but send_ota keeps one connection open for the full OTA session.
        # The device accepts start_ota then keeps the connection.
        while True:
            line = self._readline(conn)
            if not line:
                break
            self.commands.append(line)

            if line == 'start_ota':
                self._send(conn, 'ready\n')
                self._handle_ota(conn)
                break
            elif line == 'ping':
                self._send(conn, 'pong\n')
            elif line == 'version':
                self._send(conn, '{"version":"1.0.0"}\n')
            else:
                self._send(conn, 'unknown\n')

    def _handle_ota(self, conn):
        while True:
            line = self._readline(conn)
            if not line:
                return
            self.commands.append(line)

            if line == 'abort':
                self._send(conn, 'aborted\n')
                return

            if line == 'end_ota':
                self._send(conn, 'ok\n')
                return

            parts = line.split(' ', 1)
            cmd, arg = parts[0], parts[1] if len(parts) > 1 else ''

            if cmd == 'manifest':
                data = self._read_exact(conn, int(arg))
                self.manifest = json.loads(data)
                self._send(conn, 'ok\n')

            elif cmd == 'file':
                meta = arg.split(';')
                filename, size = meta[0], int(meta[1])
                data = self._read_exact(conn, size)
                self.files_received[filename] = data
                if self._error_on == filename:
                    self._send(conn, 'sha256_mismatch ' + filename + '\n')
                else:
                    self._send(conn, 'ok\n')


def _make_transport(port):
    return WiFiTCPTransport('127.0.0.1', port)


def _make_files(tmpdir, specs):
    """specs: {rel_path: content_bytes}"""
    for rel, content in specs.items():
        path = os.path.join(tmpdir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(content)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_single_file_ota():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _make_files(d, {'main.py': b'print("hello")'})
        manifest = build_manifest(['*.py'], version='1.0.0')
        files = {p: p for p in manifest['files']}

        device = MockDevice().start()
        transport = _make_transport(device.port)
        send_ota(transport, files, manifest)
        device.join()

        assert 'main.py' in device.files_received
        assert device.files_received['main.py'] == b'print("hello")'


def test_multiple_files_ota():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        specs = {
            'main.py': b'x=1',
            'lib/helper.py': b'y=2',
        }
        _make_files(d, specs)
        manifest = build_manifest(['*.py', 'lib/**'], version='1.0.0')
        files = {p: p for p in manifest['files']}

        device = MockDevice().start()
        send_ota(_make_transport(device.port), files, manifest)
        device.join()

        assert set(device.files_received.keys()) == set(specs.keys())
        for rel, content in specs.items():
            assert device.files_received[rel] == content


def test_manifest_sent_before_files():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _make_files(d, {'main.py': b'x=1'})
        manifest = build_manifest(['*.py'], version='1.0.0')
        files = {p: p for p in manifest['files']}

        device = MockDevice().start()
        send_ota(_make_transport(device.port), files, manifest)
        device.join()

        cmds = device.commands
        assert cmds[0] == 'start_ota'
        manifest_idx = next(i for i, c in enumerate(cmds) if c.startswith('manifest '))
        file_idx = next(i for i, c in enumerate(cmds) if c.startswith('file '))
        assert manifest_idx < file_idx


def test_end_ota_sent():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _make_files(d, {'main.py': b'x=1'})
        manifest = build_manifest(['*.py'], version='1.0.0')
        files = {p: p for p in manifest['files']}

        device = MockDevice().start()
        send_ota(_make_transport(device.port), files, manifest)
        device.join()

        assert 'end_ota' in device.commands


def test_sha256_mismatch_aborts():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _make_files(d, {'main.py': b'x=1'})
        manifest = build_manifest(['*.py'], version='1.0.0')
        files = {p: p for p in manifest['files']}

        device = MockDevice().start()
        device.fail_file('main.py')

        transport = _make_transport(device.port)
        try:
            send_ota(transport, files, manifest)
            assert False, 'should have raised SystemExit'
        except SystemExit:
            pass
        device.join()

        assert 'abort' in device.commands


def test_manifest_contains_correct_version():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _make_files(d, {'main.py': b'x=1'})
        manifest = build_manifest(['*.py'], version='9.8.7')
        files = {p: p for p in manifest['files']}

        device = MockDevice().start()
        send_ota(_make_transport(device.port), files, manifest)
        device.join()

        assert device.manifest['version'] == '9.8.7'


def test_empty_ota_no_files():
    """OTA with zero files should still send manifest and end_ota."""
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        manifest = {'version': '1.0.0', 'files': {}}
        files = {}

        device = MockDevice().start()
        send_ota(_make_transport(device.port), files, manifest)
        device.join()

        assert 'end_ota' in device.commands
        assert device.files_received == {}


# ── transport unit tests ──────────────────────────────────────────────────────

def test_transport_read_line():
    """WiFiTCPTransport.read_line strips \\r\\n correctly."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    port = server.getsockname()[1]

    def _serve():
        c, _ = server.accept()
        c.sendall(b'hello\r\nworld\n')
        c.close()
        server.close()

    threading.Thread(target=_serve, daemon=True).start()

    t = WiFiTCPTransport('127.0.0.1', port)
    with t:
        assert t.read_line() == 'hello'
        assert t.read_line() == 'world'


def test_transport_read_exact():
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    port = server.getsockname()[1]

    data = b'\x00\x01\x02\x03' * 256   # 1 KB of binary

    def _serve():
        c, _ = server.accept()
        # Send in small chunks to exercise the loop
        for i in range(0, len(data), 64):
            c.sendall(data[i:i+64])
        c.close()
        server.close()

    threading.Thread(target=_serve, daemon=True).start()

    t = WiFiTCPTransport('127.0.0.1', port)
    with t:
        received = t.read_exact(len(data))
    assert received == data


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
