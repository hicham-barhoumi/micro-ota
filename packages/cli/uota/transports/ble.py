"""
BLE transport for micro-ota host — Nordic UART Service (NUS) client.

Uses the `bleak` library (async BLE) wrapped in a dedicated background
asyncio event loop so the rest of the host code stays synchronous.

NUS UUIDs:
  Service  6E400001-B5A3-F393-E0A9-E50E24DCCA9E
  RX char  6E400002-B5A3-F393-E0A9-E50E24DCCA9E  (host→device, WRITE NO RSP)
  TX char  6E400003-B5A3-F393-E0A9-E50E24DCCA9E  (device→host, NOTIFY)

ota.json key: "bleName": "micro-ota"
"""

import asyncio
import sys
import threading
import time

_NUS_RX = '6E400002-B5A3-F393-E0A9-E50E24DCCA9E'   # host → device
_NUS_TX = '6E400003-B5A3-F393-E0A9-E50E24DCCA9E'   # device → host (notify)

_SCAN_TIMEOUT  = 10.0   # seconds to scan for device
_RECV_TIMEOUT  = 30.0   # seconds to wait for data
_CHUNK_SIZE    = 20     # conservative MTU payload (negotiated at runtime)


def _require_bleak():
    try:
        import bleak
        return bleak
    except ImportError:
        raise RuntimeError(
            'bleak is required for BLE transport.\n'
            '  Install it with:  pip install bleak'
        )


class BLETransport:
    """
    Synchronous host-side BLE transport over Nordic UART Service.

    Mirrors the WiFiTCPTransport interface: connect/close/read_line/
    read_exact/write/write_line, plus context-manager support.
    """

    def __init__(self, name='micro-ota', timeout=10):
        _require_bleak()
        self._name    = name
        self._timeout = timeout
        self._client  = None
        self._rx_buf  = bytearray()
        self._rx_lock = threading.Lock()
        self._rx_event = threading.Event()
        self._mtu     = _CHUNK_SIZE

        # Dedicated event loop in a daemon thread.
        # On Windows, bleak's WinRT backend requires ProactorEventLoop — create
        # it explicitly rather than relying on policy defaults across Python versions.
        if sys.platform == 'win32':
            self._loop = asyncio.ProactorEventLoop()
        else:
            self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name='ble-loop',
        )
        self._thread.start()

    # ── transport interface ───────────────────────────────────────────────────

    def connect(self):
        """Scan for the device by name and connect."""
        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        try:
            future.result(timeout=_SCAN_TIMEOUT + self._timeout)
        except (asyncio.TimeoutError, TimeoutError) as e:
            msg = 'BLE scan timed out. Device "%s" not found.' % self._name
            if sys.platform == 'win32':
                msg += (
                    '\n  Windows checklist:'
                    '\n    - Bluetooth adapter enabled in Device Manager'
                    '\n    - Device is powered on and advertising'
                    '\n    - No other app has an exclusive BLE connection to the device'
                )
            raise TimeoutError(msg) from None

    def close(self):
        if self._client is not None:
            future = asyncio.run_coroutine_threadsafe(
                self._disconnect(), self._loop
            )
            try:
                future.result(timeout=5)
            except Exception:
                pass
            self._client = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)

    def read_line(self):
        """Read until '\\n' and return the line (without '\\r\\n')."""
        deadline = time.monotonic() + _RECV_TIMEOUT
        while True:
            with self._rx_lock:
                if b'\n' in self._rx_buf:
                    idx = self._rx_buf.index(b'\n')
                    line = bytes(self._rx_buf[:idx])
                    del self._rx_buf[:idx + 1]
                    return line.rstrip(b'\r').decode()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError('BLE recv timeout waiting for newline')
            self._rx_event.wait(timeout=min(remaining, 0.05))
            self._rx_event.clear()

    def read_exact(self, n):
        """Read exactly n bytes."""
        deadline = time.monotonic() + _RECV_TIMEOUT
        while True:
            with self._rx_lock:
                if len(self._rx_buf) >= n:
                    chunk = bytes(self._rx_buf[:n])
                    del self._rx_buf[:n]
                    return chunk
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError('BLE recv timeout waiting for %d bytes' % n)
            self._rx_event.wait(timeout=min(remaining, 0.05))
            self._rx_event.clear()

    def write(self, data):
        """Send raw bytes to the device RX characteristic."""
        if isinstance(data, str):
            data = data.encode()
        future = asyncio.run_coroutine_threadsafe(
            self._write_chunks(data), self._loop
        )
        future.result(timeout=self._timeout)

    def write_line(self, line):
        self.write(line if line.endswith('\n') else line + '\n')

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    # ── async internals ───────────────────────────────────────────────────────

    async def _connect(self):
        from bleak import BleakScanner, BleakClient

        print('[BLE] Scanning for "%s" …' % self._name)
        device = await BleakScanner.find_device_by_name(
            self._name, timeout=_SCAN_TIMEOUT
        )
        if device is None:
            raise TimeoutError(
                'Device "%s" not found. Is it powered on and advertising?' % self._name
            )

        print('[BLE] Found %s (%s), connecting …' % (self._name, device.address))
        self._client = BleakClient(device)
        await self._client.connect()

        # Negotiate larger MTU where possible (bleak handles this automatically
        # on supported platforms; the device IRQ will update its own _mtu)
        try:
            mtu = self._client.mtu_size
            if mtu:
                self._mtu = max(20, mtu - 3)
        except AttributeError:
            pass

        # Subscribe to TX notifications (device → host)
        await self._client.start_notify(_NUS_TX, self._on_notify)
        print('[BLE] Connected. MTU payload=%d bytes' % self._mtu)

    async def _disconnect(self):
        try:
            await self._client.disconnect()
        except Exception:
            pass

    async def _write_chunks(self, data):
        view   = memoryview(data)
        offset = 0
        while offset < len(data):
            chunk = bytes(view[offset:offset + self._mtu])
            await self._client.write_gatt_char(_NUS_RX, chunk, response=False)
            offset += self._mtu
            if offset < len(data):
                await asyncio.sleep(0.01)   # brief pause to avoid BLE stack overflow

    def _on_notify(self, _handle, data: bytearray):
        """Called by bleak in the event loop thread when the device sends data."""
        with self._rx_lock:
            self._rx_buf.extend(data)
        self._rx_event.set()
