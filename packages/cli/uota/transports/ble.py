"""
BLE transport for micro-ota host — micro-ota OTA service client.

Uses the `bleak` library (async BLE) wrapped in a dedicated background
asyncio event loop so the rest of the host code stays synchronous.

micro-ota OTA service UUIDs ("uota" prefix, NOT NUS):
  Service  756F7461-B5A3-F393-E0A9-E50E24DCCA9E
  RX char  756F7462-B5A3-F393-E0A9-E50E24DCCA9E  (host→device, WRITE NO RSP)
  TX char  756F7463-B5A3-F393-E0A9-E50E24DCCA9E  (device→host, NOTIFY)

The NUS UUIDs (6E4000xx) are reserved on the device for RemoteIO so the
user can connect with any standard NUS terminal app (nRF UART, LightBlue…).

ota.json key: "bleName": "micro-ota"
"""

import asyncio
import sys
import threading
import time

_OTA_RX = '756F7462-B5A3-F393-E0A9-E50E24DCCA9E'   # host → device
_OTA_TX = '756F7463-B5A3-F393-E0A9-E50E24DCCA9E'   # device → host (notify)

_SCAN_TIMEOUT  = 10.0   # seconds to scan for device
_RECV_TIMEOUT  = 30.0   # seconds to wait for data
_CHUNK_SIZE    = 20     # conservative MTU payload (negotiated at runtime)
_WINDOW        = 512    # flow-control window in bytes (must match device _WINDOW)


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

    def __init__(self, name='micro-ota', timeout=10, device=None):
        _require_bleak()
        self._name    = name
        self._device  = device  # pre-discovered BLEDevice — skips re-scan
        self._timeout = timeout
        self._client  = None
        self._rx_buf  = bytearray()
        self._rx_lock = threading.Lock()
        self._rx_event = threading.Event()
        self._mtu     = _CHUNK_SIZE
        self._ack_sem = None  # asyncio.Semaphore for flow-control credits

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
        # Outer timeout: n_ack_waits × 5s (per-ACK inner timeout) + self._timeout margin.
        # This ensures the outer timeout never fires before the inner ACK timeouts can
        # raise a descriptive error.
        n_acks = max(1, (len(data) + _WINDOW - 1) // _WINDOW)
        outer_timeout = n_acks * 5.0 + self._timeout
        future = asyncio.run_coroutine_threadsafe(
            self._write_chunks(data), self._loop
        )
        try:
            future.result(timeout=outer_timeout)
        except TimeoutError:
            raise TimeoutError('BLE write timed out after %.0fs' % outer_timeout) from None

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

        if self._device is not None:
            device = self._device
            print('[BLE] Connecting to "%s" (%s) …' % (self._name, device.address))
        else:
            print('[BLE] Scanning for "%s" …' % self._name)
            device = await BleakScanner.find_device_by_name(
                self._name, timeout=_SCAN_TIMEOUT
            )
            if device is None:
                raise TimeoutError(
                    'Device "%s" not found. Is it powered on and advertising?' % self._name
                )
            print('[BLE] Found %s (%s), connecting …' % (self._name, device.address))
        # Retry on transient BlueZ 'br-connection-canceled' which happens when
        # the adapter hasn't finished processing a previous disconnect yet.
        last_exc = None
        for attempt in range(3):
            client = BleakClient(device)
            try:
                await client.connect()
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                try:
                    await client.disconnect()
                except Exception:
                    pass
                if 'br-connection-canceled' in str(e) and attempt < 2:
                    await asyncio.sleep(2)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        self._client = client

        # Negotiate larger MTU. On Linux/BlueZ, BleakClient wraps a backend
        # that exposes _acquire_mtu(); calling it issues an AcquireWrite DBus
        # call which returns the real negotiated MTU (default stays at 23).
        try:
            backend = getattr(self._client, '_backend', self._client)
            if hasattr(backend, '_acquire_mtu'):
                await backend._acquire_mtu()
            mtu = self._client.mtu_size
            if mtu:
                self._mtu = max(20, mtu - 3)
        except Exception:
            pass

        self._ack_sem = asyncio.Semaphore(0)

        # Subscribe to TX notifications (device → host)
        await self._client.start_notify(_OTA_TX, self._on_notify)
        print('[BLE] Connected. MTU payload=%d bytes' % self._mtu)

    async def _disconnect(self):
        try:
            await self._client.disconnect()
        except Exception:
            pass

    async def _write_chunks(self, data):
        view         = memoryview(data)
        offset       = 0
        window_sent  = 0
        while offset < len(data):
            end   = min(offset + self._mtu, len(data))
            chunk = bytes(view[offset:end])
            await self._client.write_gatt_char(_OTA_RX, chunk, response=False)
            window_sent += len(chunk)
            offset = end
            # Wait for device credit before sending the next window.
            # Skip the wait on the last bytes — no more data to gate.
            if window_sent >= _WINDOW and offset < len(data):
                try:
                    await asyncio.wait_for(self._ack_sem.acquire(), timeout=5.0)
                except (asyncio.TimeoutError, TimeoutError):
                    raise TimeoutError('BLE ACK timeout — device stopped responding') from None
                window_sent = 0

    def _on_notify(self, _handle, data: bytearray):
        """Called by bleak in the event loop thread when the device sends data."""
        if len(data) == 1 and data[0] == 0x06:
            # Flow-control credit from device (see _WINDOW / _ACK on device side)
            if self._ack_sem is not None:
                self._ack_sem.release()
            return
        with self._rx_lock:
            self._rx_buf.extend(data)
        self._rx_event.set()
