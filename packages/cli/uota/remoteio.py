"""
RemoteIO host client — connects to the device's RemoteIO side-channel.

Usage (CLI):
    python3 host/remoteio.py listen [host [port]]
    python3 host/remoteio.py call <name> [key=value ...] [host [port]]

Usage (Python):
    from host.remoteio import RemoteIOClient

    with RemoteIOClient('192.168.1.100') as rio:
        # Stream device print() output to stdout
        rio.listen()

    with RemoteIOClient('192.168.1.100') as rio:
        result = rio.call('free_mem')
        print('free:', result)

        # Keep listening for prints while making calls in background
        import threading
        threading.Thread(target=rio.listen, daemon=True).start()
        result = rio.call('echo', msg='hello')
"""

import json
import socket
import sys
import threading


class RemoteIOBLEClient:
    """
    RemoteIO client over BLE NUS (Nordic UART Service).

    Connects to the NUS service on the micro-ota device and speaks the same
    JSON-over-connection protocol as RemoteIOClient, but over BLE instead of TCP.

    NUS UUIDs:
      Service  6E400001-B5A3-F393-E0A9-E50E24DCCA9E
      RX char  6E400002-B5A3-F393-E0A9-E50E24DCCA9E  (host→device, WRITE NO RSP)
      TX char  6E400003-B5A3-F393-E0A9-E50E24DCCA9E  (device→host, NOTIFY)

    Usage:
        from uota.remoteio import RemoteIOBLEClient

        with RemoteIOBLEClient('micro-ota') as rio:
            print(rio.call('free_mem'))
            print(rio.call('ping'))
    """

    _NUS_RX = '6E400002-B5A3-F393-E0A9-E50E24DCCA9E'
    _NUS_TX = '6E400003-B5A3-F393-E0A9-E50E24DCCA9E'
    _SCAN_TIMEOUT = 10.0

    def __init__(self, name='micro-ota', timeout=10):
        import asyncio as _asyncio
        self.name    = name
        self.timeout = timeout
        self._client = None
        self._mtu    = 20
        # id → (Event, [result, error])
        self._pending  = {}
        self._call_id  = 0
        self._lock     = threading.Lock()
        self._buf      = b''  # partial line buffer (notifications may split lines)

        if sys.platform == 'win32':
            self._loop = _asyncio.ProactorEventLoop()
        else:
            self._loop = _asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name='ble-rio-loop'
        )
        self._thread.start()

    def connect(self):
        import asyncio as _asyncio
        fut = _asyncio.run_coroutine_threadsafe(self._async_connect(), self._loop)
        fut.result(timeout=self._SCAN_TIMEOUT + self.timeout)

    def close(self):
        import asyncio as _asyncio
        if self._client:
            fut = _asyncio.run_coroutine_threadsafe(self._async_disconnect(), self._loop)
            try:
                fut.result(timeout=5)
            except Exception:
                pass
            self._client = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)
        # Unblock any pending call()
        with self._lock:
            for ev, slot in self._pending.values():
                slot[1] = 'connection closed'
                ev.set()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    def listen(self):
        """Block and forward device print() output to stdout. Ctrl-C to stop."""
        import time as _time
        try:
            while self._client is not None and self._thread.is_alive():
                _time.sleep(0.1)
        except KeyboardInterrupt:
            pass

    def call(self, name, timeout=None, **kwargs):
        """Invoke a named RemoteIO handler on the device and return the result."""
        import asyncio as _asyncio
        timeout = timeout if timeout is not None else self.timeout
        ev   = threading.Event()
        slot = [None, None]
        with self._lock:
            self._call_id += 1
            mid = self._call_id
            self._pending[mid] = (ev, slot)
        msg = (json.dumps({'t': 'call', 'id': mid, 'name': name, 'args': kwargs})
               + '\n').encode()
        fut = _asyncio.run_coroutine_threadsafe(self._write_nus(msg), self._loop)
        fut.result(timeout=5)
        if not ev.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise TimeoutError('BLE RemoteIO call "%s" timed out' % name)
        with self._lock:
            self._pending.pop(mid, None)
        if slot[1] is not None:
            raise RuntimeError(slot[1])
        return slot[0]

    # ── async internals ───────────────────────────────────────────────────────

    async def _async_connect(self):
        from bleak import BleakScanner, BleakClient
        device = await BleakScanner.find_device_by_name(
            self.name, timeout=self._SCAN_TIMEOUT
        )
        if device is None:
            raise TimeoutError('BLE device "%s" not found' % self.name)
        self._client = BleakClient(device)
        await self._client.connect()
        try:
            backend = getattr(self._client, '_backend', self._client)
            if hasattr(backend, '_acquire_mtu'):
                await backend._acquire_mtu()
            mtu = self._client.mtu_size
            if mtu:
                self._mtu = max(20, mtu - 3)
        except Exception:
            pass
        await self._client.start_notify(self._NUS_TX, self._on_notify)

    async def _async_disconnect(self):
        try:
            await self._client.disconnect()
        except Exception:
            pass

    async def _write_nus(self, data):
        view, offset = memoryview(data), 0
        while offset < len(data):
            end = min(offset + self._mtu, len(data))
            await self._client.write_gatt_char(
                self._NUS_RX, bytes(view[offset:end]), response=False
            )
            offset = end

    def _on_notify(self, _handle, data: bytearray):
        # 0x06 = flow-control ACK from device — ignore
        if len(data) == 1 and data[0] == 0x06:
            return
        raw = self._buf + bytes(data)
        lines = raw.split(b'\n')
        self._buf = lines[-1]          # keep incomplete trailing fragment
        for line in lines[:-1]:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            t = msg.get('t')
            if t == 'print':
                sys.stdout.write(msg.get('d', ''))
            elif t in ('resp', 'err'):
                mid = msg.get('id')
                with self._lock:
                    entry = self._pending.get(mid)
                if entry:
                    ev, slot = entry
                    if t == 'resp':
                        slot[0] = msg.get('r')
                    else:
                        slot[1] = msg.get('e', 'unknown error')
                    ev.set()


class RemoteIOClient:
    DEFAULT_PORT = 2019

    def __init__(self, host, port=DEFAULT_PORT, timeout=10):
        self.host    = host
        self.port    = port
        self.timeout = timeout
        self._sock   = None
        self._buf    = b''
        self._lock   = threading.Lock()
        self._call_id     = 0
        self._pending     = {}          # id → (Event, result_slot)
        self._on_print    = None        # callback for print messages
        self._reader_thread = None

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self):
        self._sock = socket.create_connection((self.host, self.port),
                                              timeout=self.timeout)
        self._sock.settimeout(None)     # switch to blocking
        self._reader_thread = threading.Thread(target=self._reader,
                                               daemon=True, name='remoteio-rx')
        self._reader_thread.start()

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    # ── RPC ───────────────────────────────────────────────────────────────────

    def call(self, name, **args):
        """
        Invoke a named handler on the device and return its result.
        Raises RuntimeError on device-side errors or timeout.
        """
        event = threading.Event()
        slot  = [None, None]            # [result, error_msg]
        with self._lock:
            self._call_id += 1
            mid = self._call_id
            self._pending[mid] = (event, slot)

        msg = json.dumps({'t': 'call', 'id': mid, 'name': name,
                          'args': args}) + '\n'
        self._sock.sendall(msg.encode())

        if not event.wait(timeout=self.timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise TimeoutError('No response from device for call: ' + name)

        with self._lock:
            self._pending.pop(mid, None)

        if slot[1] is not None:
            raise RuntimeError(slot[1])
        return slot[0]

    # ── print streaming ───────────────────────────────────────────────────────

    def listen(self, on_print=None):
        """
        Block and stream device print() output to *on_print* (or stdout).
        Returns when the connection drops.  Press Ctrl-C to interrupt.
        """
        self._on_print = on_print or sys.stdout.write
        try:
            self._reader_thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            self._on_print = None

    # ── internal ──────────────────────────────────────────────────────────────

    def _reader(self):
        buf = b''
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._handle(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass
        # Wake up any blocked call() / listen()
        with self._lock:
            for (event, slot) in self._pending.values():
                slot[1] = 'connection closed'
                event.set()

    def _handle(self, msg):
        t = msg.get('t')
        if t == 'print':
            fn = self._on_print or sys.stdout.write
            fn(msg.get('d', ''))
        elif t in ('resp', 'err'):
            mid = msg.get('id')
            with self._lock:
                entry = self._pending.get(mid)
            if entry:
                event, slot = entry
                if t == 'resp':
                    slot[0] = msg.get('r')
                else:
                    slot[1] = msg.get('e', 'unknown error')
                event.set()


# ── CLI helpers ───────────────────────────────────────────────────────────────

def _load_config():
    """Read host/port from config/ota.json in the current working directory."""
    import os as _os
    path = _os.path.join(_os.getcwd(), 'config', 'ota.json')
    try:
        with open(path) as f:
            cfg = json.load(f)
        return (cfg.get('hostname', '192.168.1.100'),
                cfg.get('remoteioPort', RemoteIOClient.DEFAULT_PORT))
    except Exception:
        return ('192.168.1.100', RemoteIOClient.DEFAULT_PORT)


def _parse_kwargs(tokens):
    """Parse key=value tokens into a dict, JSON-decoding values where possible."""
    kwargs = {}
    for tok in tokens:
        if '=' in tok:
            k, v = tok.split('=', 1)
            try:
                v = json.loads(v)
            except Exception:
                pass
            kwargs[k] = v
    return kwargs


def _friendly(exc, host, port):
    """Map connection exceptions to clear one-liners."""
    import errno as _errno
    msg = str(exc)
    eno = getattr(exc, 'errno', None)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ('Timed out connecting to {}:{}.\n'
                '  Is the device on WiFi and is RemoteIO running?').format(host, port)
    if isinstance(exc, ConnectionRefusedError) or eno == _errno.ECONNREFUSED:
        return ('Connection refused at {}:{}.\n'
                '  RemoteIO server may not have started — wait a few seconds and retry.').format(host, port)
    if eno in (_errno.ENETUNREACH, _errno.EHOSTUNREACH):
        return 'Network unreachable. Check device IP and WiFi.'
    if isinstance(exc, TimeoutError):
        return 'RPC call timed out. Device may be busy.'
    return None


def main():
    argv = sys.argv[1:]

    # Parse -v / --verbose before the subcommand
    verbose = '-v' in argv or '--verbose' in argv
    argv = [a for a in argv if a not in ('-v', '--verbose')]

    if not argv:
        print('Usage:')
        print('  python3 host/remoteio.py [-v] listen [host [port]]')
        print('  python3 host/remoteio.py [-v] call <name> [key=val ...] [host [port]]')
        sys.exit(1)

    host, port = _load_config()
    cmd = argv[0]

    try:
        if cmd == 'listen':
            remaining = argv[1:]
            if remaining and not remaining[0].startswith('-'):
                host = remaining.pop(0)
            if remaining:
                port = int(remaining.pop(0))
            print('Connecting to %s:%d …' % (host, port))
            with RemoteIOClient(host, port) as rio:
                print('Connected. Streaming device output (Ctrl-C to stop).\n')
                rio.listen()

        elif cmd == 'call':
            if len(argv) < 2:
                print('call requires a handler name')
                sys.exit(1)
            name   = argv[1]
            tokens = argv[2:]
            remaining = []
            for tok in tokens:
                if '=' not in tok and not tok.lstrip('-').isdigit():
                    host = tok
                elif tok.isdigit():
                    port = int(tok)
                else:
                    remaining.append(tok)
            kwargs = _parse_kwargs(remaining)
            with RemoteIOClient(host, port) as rio:
                result = rio.call(name, **kwargs)
            print(json.dumps(result, indent=2))

        else:
            print('Unknown command: %s  (try listen or call)' % cmd)
            sys.exit(1)

    except KeyboardInterrupt:
        print('\nInterrupted.', file=sys.stderr)
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        if verbose:
            raise
        friendly = _friendly(exc, host, port)
        if friendly:
            print('ERROR:', friendly, file=sys.stderr)
        else:
            print('ERROR:', exc, file=sys.stderr)
            print('       Run with -v for the full traceback.', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
