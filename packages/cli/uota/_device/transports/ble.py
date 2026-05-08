"""
BLE transport for micro-ota.

Two GATT services are registered on the same BLE peripheral:

  OTA service  ("uota" UUIDs) — for the uota CLI
    Service  756F7461-B5A3-F393-E0A9-E50E24DCCA9E
    RX char  756F7462-B5A3-F393-E0A9-E50E24DCCA9E  (host→device, WRITE)
    TX char  756F7463-B5A3-F393-E0A9-E50E24DCCA9E  (device→host, NOTIFY)

  RemoteIO service  (Nordic UART Service / NUS UUIDs) — for user terminal apps
    Service  6E400001-B5A3-F393-E0A9-E50E24DCCA9E
    RX char  6E400002-B5A3-F393-E0A9-E50E24DCCA9E  (host→device, WRITE)
    TX char  6E400003-B5A3-F393-E0A9-E50E24DCCA9E  (device→host, NOTIFY)

Using NUS for RemoteIO lets the user connect with any standard NUS BLE
terminal (nRF UART, LightBlue, etc.) without any custom app.

ota.json keys:
  "bleName": "micro-ota"    # BLE advertisement name (max ~20 chars)
"""

import time
import _thread

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
    Socket-like wrapper over one GATT service within a BLE connection.
    recv() and sendall() use the same interface as TCP sockets so the OTA
    server and RemoteIO handler need no changes.
    """

    def __init__(self, ble, conn_handle, tx_handle, initial_mtu=20):
        self._ble         = ble
        self._conn        = conn_handle
        self._tx          = tx_handle
        self._mtu         = max(20, initial_mtu - 3)
        self._buf         = bytearray()
        self._closed      = False
        self._rx_received = 0
        # Outbound buffer: app-thread callers enqueue here; main thread drains
        # via _flush() so all gatts_notify() calls stay on the main thread.
        self._out_buf     = bytearray()
        self._out_lock    = _thread.allocate_lock()

    def set_mtu(self, mtu):
        self._mtu = max(20, mtu - 3)

    def _push(self, data):
        self._buf.extend(data)
        self._rx_received += len(data)
        while self._rx_received >= _WINDOW:
            self._ble.gatts_notify(self._conn, self._tx, _ACK)
            self._rx_received -= _WINDOW

    def _flush(self):
        """Drain the outbound buffer via gatts_notify. Main thread only."""
        with self._out_lock:
            if not self._out_buf:
                return
            data = bytes(self._out_buf)
            self._out_buf = bytearray()
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            chunk = bytes(view[offset:offset + self._mtu])
            self._ble.gatts_notify(self._conn, self._tx, chunk)
            offset += self._mtu
            if offset < len(data):
                time.sleep_ms(10)

    def recv(self, n):
        # Wait for at least 1 byte (like TCP recv: return what's available up to n).
        # No fixed timeout — _closed is set by _IRQ_CENTRAL_DISCONNECT so the
        # loop always terminates when the host drops the BLE connection.
        # While waiting we flush any outbound data queued by the app thread so
        # that all gatts_notify() calls happen here, on the main thread.
        while len(self._buf) == 0:
            if self._closed:
                return b''   # EOF — empty bytes signals end-of-stream
            self._flush()
            time.sleep_ms(5)
        chunk = bytes(self._buf[:n])
        self._buf = self._buf[len(chunk):]
        return chunk

    def sendall(self, data):
        # Buffer the data; the main thread drains it in recv()'s spin-wait.
        if isinstance(data, str):
            data = data.encode('latin-1')
        with self._out_lock:
            self._out_buf.extend(data)

    def close(self):
        self._flush()   # send any pending output before disconnecting
        self._closed = True
        try:
            self._ble.gap_disconnect(self._conn)
        except Exception:
            pass


# ── transport ─────────────────────────────────────────────────────────────────

class BLETransport:
    """
    OTA transport over BLE.

    Registers two GATT services:
      - OTA service (custom "uota" UUIDs): accept() / try_accept() return
        connections for the OTA protocol.
      - NUS service (standard NUS UUIDs): try_accept_remoteio() returns
        connections for the RemoteIO channel (any NUS terminal app).

    Both services share the same physical BLE connection.
    """

    def __init__(self, name='micro-ota'):
        self._name           = name[:20]
        self._ble            = None
        self._ota_rx_h       = None
        self._ota_tx_h       = None
        self._nus_rx_h       = None
        self._nus_tx_h       = None
        self._conn           = None     # physical connection
        self._ota_pending    = None     # _BLEConn for OTA
        self._nus_pending    = None     # _BLEConn for NUS/RemoteIO
        self._mtu            = 20
        self._registered     = False

    # ── transport interface ───────────────────────────────────────────────────

    def start(self):
        import ubluetooth
        # OTA service UUIDs ("uota" prefix)
        self._OTA_SVC = ubluetooth.UUID('756F7461-B5A3-F393-E0A9-E50E24DCCA9E')
        self._OTA_RX  = ubluetooth.UUID('756F7462-B5A3-F393-E0A9-E50E24DCCA9E')
        self._OTA_TX  = ubluetooth.UUID('756F7463-B5A3-F393-E0A9-E50E24DCCA9E')
        # NUS service UUIDs (standard, for RemoteIO / user terminals)
        self._NUS_SVC = ubluetooth.UUID('6E400001-B5A3-F393-E0A9-E50E24DCCA9E')
        self._NUS_RX  = ubluetooth.UUID('6E400002-B5A3-F393-E0A9-E50E24DCCA9E')
        self._NUS_TX  = ubluetooth.UUID('6E400003-B5A3-F393-E0A9-E50E24DCCA9E')
        self._ble = ubluetooth.BLE()
        if not self._ble.active():
            self._ble.active(True)
            time.sleep_ms(100)
        self._ble.irq(self._irq)
        if not self._registered:
            self._register()
            self._registered = True
        self._advertise()
        print('[BLE] Advertising as "%s"' % self._name)

    def stop(self):
        try:
            if self._ble:
                self._ble.gap_advertise(None)
        except Exception:
            pass

    def radio_pause(self):
        self.stop()
        try:
            if self._ble:
                self._ble.active(False)
        except Exception:
            pass
        self._ble        = None
        self._conn       = None
        self._ota_pending = None
        self._nus_pending = None
        self._registered = False

    def radio_resume(self):
        self.start()

    def accept(self):
        """Block until a BLE central connects; return the OTA connection."""
        self._ota_pending = None
        while self._ota_pending is None:
            time.sleep_ms(50)
        conn = self._ota_pending
        self._ota_pending = None
        return conn

    def try_accept(self):
        """Return a pending OTA connection immediately, or None."""
        conn = self._ota_pending
        if conn is not None:
            self._ota_pending = None
        return conn

    def try_accept_remoteio(self):
        """Return a pending NUS/RemoteIO connection immediately, or None."""
        conn = self._nus_pending
        if conn is not None:
            self._nus_pending = None
        return conn

    # ── GATT setup ────────────────────────────────────────────────────────────

    def _register(self):
        ota_svc = (
            self._OTA_SVC,
            (
                (self._OTA_RX, _F_WRITE | _F_WRITE_NR),
                (self._OTA_TX, _F_READ  | _F_NOTIFY),
            ),
        )
        nus_svc = (
            self._NUS_SVC,
            (
                (self._NUS_RX, _F_WRITE | _F_WRITE_NR),
                (self._NUS_TX, _F_READ  | _F_NOTIFY),
            ),
        )
        ((self._ota_rx_h, self._ota_tx_h),
         (self._nus_rx_h, self._nus_tx_h)) = self._ble.gatts_register_services(
            (ota_svc, nus_svc)
        )
        self._ble.gatts_set_buffer(self._ota_rx_h, 512)
        self._ble.gatts_set_buffer(self._nus_rx_h, 512)

    def _advertise(self):
        name_b    = self._name.encode()
        flags     = b'\x02\x01\x06'
        # Advertise OTA service UUID in the scan response so scanners can
        # identify this as a micro-ota device; NUS is discoverable via GATT.
        ota_uuid  = bytes(reversed(bytes.fromhex('756F7461B5A3F393E0A9E50E24DCCA9E')))
        adv_data  = flags + bytes([len(name_b) + 1, 0x09]) + name_b
        resp_data = bytes([len(ota_uuid) + 1, 0x07]) + ota_uuid
        # 500 ms interval: BLE broadcasts twice per second, leaving the 2.4 GHz
        # radio mostly free for WiFi during coexistence windows.
        self._ble.gap_advertise(500_000, adv_data=adv_data, resp_data=resp_data)

    # ── IRQ handler ───────────────────────────────────────────────────────────

    def _irq(self, event, data):
        if event == _IRQ_CONNECT:
            conn_handle, _, _ = data
            ota = _BLEConn(self._ble, conn_handle, self._ota_tx_h, self._mtu)
            nus = _BLEConn(self._ble, conn_handle, self._nus_tx_h, self._mtu)
            self._conn = (ota, nus)
            # Pending is set on first write to each characteristic, not on
            # connect, so the event loop can route OTA vs NUS RemoteIO based
            # on which service the central actually uses.
            print('[BLE] Client connected')

        elif event == _IRQ_DISCONNECT:
            conn_handle, _, _ = data
            if self._conn:
                ota, nus = self._conn
                if ota._conn == conn_handle:
                    ota._closed = True
                    nus._closed = True
                    self._conn = None
            print('[BLE] Client disconnected — re-advertising')
            self._advertise()

        elif event == _IRQ_WRITE:
            conn_handle, attr_handle = data
            if self._conn:
                ota, nus = self._conn
                payload = self._ble.gatts_read(attr_handle)
                if attr_handle == self._ota_rx_h:
                    ota._push(payload)
                    if self._ota_pending is None:
                        self._ota_pending = ota
                elif attr_handle == self._nus_rx_h:
                    nus._push(payload)
                    if self._nus_pending is None:
                        self._nus_pending = nus

        elif event == _IRQ_MTU:
            conn_handle, mtu = data
            self._mtu = mtu
            if self._conn:
                ota, nus = self._conn
                if ota._conn == conn_handle:
                    ota.set_mtu(mtu)
                    nus.set_mtu(mtu)
            print('[BLE] MTU negotiated:', mtu)
