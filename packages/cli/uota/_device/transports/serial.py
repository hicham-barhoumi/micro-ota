"""
Serial UART transport for micro-ota device.

Runs the OTA server on a hardware UART (default UART1, GPIO9/10).
Use UART1 to avoid conflicts with the REPL which lives on UART0.

For OTA over USB/UART0 without extra wiring, the host uses raw REPL
injection instead — see host/transports/serial.py SerialOTATransport.

ota.json keys used:
  serialUartId   (int, default 1)    UART peripheral index
  serialBaud     (int, default 115200)
  serialTx       (int, default 10)   TX GPIO pin
  serialRx       (int, default 9)    RX GPIO pin
"""

import time
import machine


# Magic header the host must send before an OTA session begins.
# Prevents random serial noise from triggering OTA.
MAGIC = b'\x18\x01UOTA\n'     # Ctrl-X  Ctrl-A  "UOTA\n"


class _UARTConn:
    """Wraps machine.UART with recv() / sendall() so ota._handle() can use it."""

    def __init__(self, uart):
        self._u = uart

    def recv(self, n):
        while not self._u.any():
            time.sleep_ms(1)
        return self._u.read(min(n, self._u.any()))

    def sendall(self, data):
        if isinstance(data, str):
            data = data.encode()
        self._u.write(data)

    def close(self):
        pass


class SerialTransport:
    """
    OTA server transport over a hardware UART.

    Listens for MAGIC then returns one _UARTConn per OTA session.
    Call start() once, then loop on accept().
    """

    def __init__(self, uart_id=1, baud=115200, tx=10, rx=9):
        self._uart_id = uart_id
        self._baud    = baud
        self._tx      = tx
        self._rx      = rx
        self._uart    = None

    def start(self):
        self._uart = machine.UART(
            self._uart_id, self._baud,
            tx=self._tx, rx=self._rx,
        )
        print('[OTA] Serial transport ready on UART{} TX={} RX={}'.format(
            self._uart_id, self._tx, self._rx))

    def accept(self):
        """Block until the host sends MAGIC, then return a connection."""
        buf = bytearray()
        mlen = len(MAGIC)
        while True:
            if self._uart.any():
                buf.extend(self._uart.read(self._uart.any()))
                if buf.endswith(MAGIC):
                    self._uart.write(b'UOTA_OK\n')
                    return _UARTConn(self._uart)
                if len(buf) > mlen * 4:
                    buf = buf[-mlen:]
            else:
                time.sleep_ms(10)

    def stop(self):
        if self._uart:
            self._uart.deinit()
            self._uart = None
