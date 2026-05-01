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

import time

_F_WRITE    = 0x0008
_F_WRITE_NR = 0x0004
_F_READ     = 0x0002
_F_NOTIFY   = 0x0010

_IRQ_CONNECT    = 1
_IRQ_DISCONNECT = 2
_IRQ_WRITE      = 3
_IRQ_MTU        = 21

_ACK    = b'\x06'   # 1-byte flow-control credit sent to host
_WINDOW = 512       # host sends this many bytes between credit waits


# ── connection object ─────────────────────────────────────────────────────────

class _BLEConn:
    """
    Socket-like wrapper over a BLE connection.
    recv() and sendall() use the same interface as TCP sockets so the OTA
    server (_handle in ota.py) needs no changes.
    """

    def __init__(self, ble, conn_handle, tx_handle, initial_mtu=20):
        self._ble        = ble
        self._conn       = conn_handle
        self._tx         = tx_handle
        self._mtu        = max(20, initial_mtu - 3)
        self._buf        = bytearray()
        self._closed     = False
        self._rx_pending = 0   # bytes consumed since last credit

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
        self._buf[:n] = b''  # del self._buf[:n] is not supported in MicroPython
        self._rx_pending += len(chunk)
        while self._rx_pending >= _WINDOW:
            self._ble.gatts_notify(self._conn, self._tx, _ACK)
            self._rx_pending -= _WINDOW
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
                time.sleep_ms(10)

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
        self._name       = name[:20]
        self._ble        = None
        self._rx_h       = None
        self._tx_h       = None
        self._conn       = None
        self._pending    = None
        self._mtu        = 20
        self._registered = False

    # ── transport interface ───────────────────────────────────────────────────

    def start(self):
        import ubluetooth
        self._SVC = ubluetooth.UUID('6E400001-B5A3-F393-E0A9-E50E24DCCA9E')
        self._RX  = ubluetooth.UUID('6E400002-B5A3-F393-E0A9-E50E24DCCA9E')
        self._TX  = ubluetooth.UUID('6E400003-B5A3-F393-E0A9-E50E24DCCA9E')
        self._ble = ubluetooth.BLE()
        self._ble.irq(self._irq)
        if not self._registered:
            self._register()
            self._registered = True
        self._advertise()
        print('[BLE] Advertising as "%s"' % self._name)

    def stop(self):
        # Stop advertising only — do not deactivate BLE hardware.
        # The hardware was activated in the main thread and must stay up so
        # that _run_transport can restart this transport without cycling
        # ble.active() from the OTA thread (which causes an HCI error).
        try:
            if self._ble:
                self._ble.gap_advertise(None)
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
            self._SVC,
            (
                (self._RX, _F_WRITE | _F_WRITE_NR),
                (self._TX, _F_READ  | _F_NOTIFY),
            ),
        )
        ((self._rx_h, self._tx_h),) = self._ble.gatts_register_services((svc_def,))
        # Default GATT attribute buffer is 20 bytes; ATT writes from the host are
        # up to MTU-3 = 253 bytes.  Without this, Bluedroid silently truncates
        # every write to 20 bytes and still sends the ATT WRITE RESPONSE, so the
        # host reports 100% while the device receives ~8% of the stream.
        self._ble.gatts_set_buffer(self._rx_h, 512)
        print('[BLE] RX buffer set to 512 bytes')

    def _advertise(self):
        # BLE advertising data is limited to 31 bytes.
        # flags(3) + name_ad(2+len) fits easily; the 16-byte UUID goes in
        # scan response to avoid exceeding the limit.
        name_b   = self._name.encode()
        flags    = b'\x02\x01\x06'
        svc_uuid = bytes(reversed(bytes.fromhex('6E400001B5A3F393E0A9E50E24DCCA9E')))
        adv_data  = flags + bytes([len(name_b) + 1, 0x09]) + name_b
        resp_data = bytes([len(svc_uuid) + 1, 0x07]) + svc_uuid
        self._ble.gap_advertise(100_000, adv_data=adv_data, resp_data=resp_data)

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
