"""
BLE transport for micro-ota — Nordic UART Service (NUS) GATT server.

Exposes the device as a BLE peripheral advertising the NUS service.
The host connects and speaks the standard micro-ota OTA protocol over the
TX/RX characteristics exactly as it would over TCP or UART.

NUS UUIDs (128-bit, little-endian in ubluetooth):
  Service  6E400001-B5A3-F393-E0A9-E50E24DCCA9E
  RX char  6E400002-B5A3-F393-E0A9-E50E24DCCA9E  (host→device, WRITE)
  TX char  6E400003-B5A3-F393-E0A9-E50E24DCCA9E  (device→host, NOTIFY)

ota.json keys:
  "bleName": "micro-ota"    # BLE advertisement name (max ~20 chars)
"""

import ubluetooth
import time

# ── NUS service definition ────────────────────────────────────────────────────

_SVC  = ubluetooth.UUID('6E400001-B5A3-F393-E0A9-E50E24DCCA9E')
_RX   = ubluetooth.UUID('6E400002-B5A3-F393-E0A9-E50E24DCCA9E')
_TX   = ubluetooth.UUID('6E400003-B5A3-F393-E0A9-E50E24DCCA9E')

_F_WRITE    = 0x0008
_F_WRITE_NR = 0x0004   # write without response (faster)
_F_READ     = 0x0002
_F_NOTIFY   = 0x0010

_IRQ_CONNECT    = 1
_IRQ_DISCONNECT = 2
_IRQ_WRITE      = 3
_IRQ_MTU        = 21


# ── connection object ─────────────────────────────────────────────────────────

class _BLEConn:
    """
    Socket-like wrapper over a BLE connection.
    recv() and sendall() use the same interface as TCP sockets so the OTA
    server (_handle in ota.py) needs no changes.
    """

    def __init__(self, ble, conn_handle, tx_handle, initial_mtu=20):
        self._ble  = ble
        self._conn = conn_handle
        self._tx   = tx_handle
        self._mtu  = max(20, initial_mtu - 3)  # payload per notify
        self._buf  = bytearray()
        self._closed = False

    def set_mtu(self, mtu):
        self._mtu = max(20, mtu - 3)

    def _push(self, data):
        self._buf.extend(data)

    def recv(self, n):
        deadline = time.ticks_add(time.ticks_ms(), 30_000)
        while len(self._buf) < n:
            if self._closed:
                if self._buf:
                    break
                raise OSError('BLE connection closed')
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                raise OSError('BLE recv timeout')
            time.sleep_ms(5)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def sendall(self, data):
        if isinstance(data, str):
            data = data.encode('latin-1')
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            chunk = bytes(view[offset:offset + self._mtu])
            self._ble.gatts_notify(self._conn, self._tx, chunk)
            offset += self._mtu
            if offset < len(data):
                time.sleep_ms(10)  # give BLE stack time to flush

    def close(self):
        self._closed = True
        try:
            self._ble.gap_disconnect(self._conn)
        except Exception:
            pass


# ── transport ─────────────────────────────────────────────────────────────────

class BLETransport:
    """
    OTA transport over BLE (Nordic UART Service).

    Works as a push-mode transport: start() registers the NUS service and
    begins advertising; accept() blocks until a central connects and returns
    a _BLEConn that the OTA server reads/writes.  After disconnect the
    transport re-advertises automatically.
    """

    def __init__(self, name='micro-ota'):
        self._name     = name[:20]   # BLE name length limit
        self._ble      = ubluetooth.BLE()
        self._rx_h     = None        # GATT handle: RX characteristic
        self._tx_h     = None        # GATT handle: TX characteristic
        self._conn     = None        # current _BLEConn (or None)
        self._pending  = None        # set by IRQ when a new client connects
        self._mtu      = 20          # updated by MTU exchange IRQ

    # ── transport interface ───────────────────────────────────────────────────

    def start(self):
        self._ble.active(True)
        self._ble.irq(self._irq)
        self._register()
        self._advertise()
        print('[BLE] Advertising as "%s"' % self._name)

    def stop(self):
        try:
            self._ble.active(False)
        except Exception:
            pass

    def accept(self):
        """Block until a BLE central connects; return the connection object."""
        self._pending = None
        while self._pending is None:
            time.sleep_ms(50)
        conn = self._pending
        self._pending = None
        return conn

    # ── GATT setup ────────────────────────────────────────────────────────────

    def _register(self):
        svc_def = (
            _SVC,
            (
                (_RX, _F_WRITE | _F_WRITE_NR),
                (_TX, _F_READ  | _F_NOTIFY),
            ),
        )
        ((self._rx_h, self._tx_h),) = self._ble.gatts_register_services((svc_def,))

    def _advertise(self):
        name_b  = self._name.encode()
        # AD structures: flags + NUS service UUID + complete local name
        flags   = b'\x02\x01\x06'
        # 128-bit UUID in little-endian (reversed)
        svc_uuid = bytes(reversed(bytes.fromhex(
            '6E400001B5A3F393E0A9E50E24DCCA9E'
        )))
        uuid_ad  = bytes([len(svc_uuid) + 1, 0x07]) + svc_uuid
        name_ad  = bytes([len(name_b) + 1, 0x09]) + name_b
        self._ble.gap_advertise(100_000, adv_data=flags + uuid_ad + name_ad)

    # ── IRQ handler ───────────────────────────────────────────────────────────

    def _irq(self, event, data):
        if event == _IRQ_CONNECT:
            conn_handle, _, _ = data
            conn = _BLEConn(self._ble, conn_handle, self._tx_h, self._mtu)
            self._conn    = conn
            self._pending = conn
            print('[BLE] Client connected')

        elif event == _IRQ_DISCONNECT:
            conn_handle, _, _ = data
            if self._conn and self._conn._conn == conn_handle:
                self._conn._closed = True
                self._conn = None
            print('[BLE] Client disconnected — re-advertising')
            self._advertise()

        elif event == _IRQ_WRITE:
            conn_handle, attr_handle = data
            if attr_handle == self._rx_h and self._conn:
                payload = self._ble.gatts_read(self._rx_h)
                self._conn._push(payload)

        elif event == _IRQ_MTU:
            conn_handle, mtu = data
            self._mtu = mtu
            if self._conn and self._conn._conn == conn_handle:
                self._conn.set_mtu(mtu)
            print('[BLE] MTU negotiated:', mtu)
