"""
Serial transports for micro-ota host.

RawREPL         — raw REPL file uploader, used by bootstrap only.
SerialOTATransport — OTA over USB/UART0 via raw REPL injection.
                   Enters raw REPL, injects an inline OTA server on the
                   device, then speaks the standard micro-ota protocol
                   directly over the serial port. No extra wiring needed.
"""

import base64
import time
import serial
import serial.tools.list_ports


def auto_detect_port():
    """Return the first serial port that looks like an ESP32."""
    ESP_VIDS = {0x10C4, 0x1A86, 0x0403, 0x303A}   # CP2102, CH340, FTDI, Espressif native
    for p in serial.tools.list_ports.comports():
        if p.vid in ESP_VIDS:
            return p.device
    # Fallback: first available port
    ports = serial.tools.list_ports.comports()
    if ports:
        return ports[0].device
    return None


class RawREPL:
    """MicroPython raw REPL file uploader."""

    # Max bytes of binary data per exec chunk.
    # base64 overhead is ~4/3, raw REPL exec buffer is generous but
    # keep chunks small for reliability on slow links.
    CHUNK_BINARY = 192

    def __init__(self, port, baud=115200, timeout=5):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser = None

    def open(self):
        self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        time.sleep(0.5)
        self._interrupt()
        self._enter_raw()

    def close(self):
        if self._ser and self._ser.is_open:
            try:
                self._ser.write(b'\x02')   # Ctrl+B: exit raw REPL
                self._ser.flush()
            except Exception:
                pass
            self._ser.close()
        self._ser = None

    def soft_reset(self):
        """Exit raw REPL and soft-reset the device."""
        self._ser.write(b'\x02')   # Ctrl+B
        time.sleep(0.1)
        self._ser.write(b'\x04')   # Ctrl+D soft reset
        self._ser.flush()
        time.sleep(1)

    # ── raw REPL protocol ────────────────────────────────────────────────────

    def _interrupt(self):
        self._ser.write(b'\r\x03\x03')
        self._ser.flush()
        time.sleep(0.3)
        self._ser.reset_input_buffer()

    def _enter_raw(self):
        self._ser.write(b'\x01')   # Ctrl+A
        self._ser.flush()
        time.sleep(0.1)
        data = self._ser.read(200)
        if b'raw REPL' not in data:
            # Try once more after another interrupt
            self._interrupt()
            self._ser.write(b'\x01')
            self._ser.flush()
            time.sleep(0.2)
            data = self._ser.read(200)
            if b'raw REPL' not in data:
                raise RuntimeError(
                    'Could not enter raw REPL. Got: ' + repr(data) +
                    '\nCheck the port/baud or press Reset on the device.'
                )

    def exec(self, code):
        """Execute a snippet of Python code. Raises on MicroPython error."""
        if isinstance(code, str):
            code = code.encode()
        self._ser.write(code)
        self._ser.write(b'\x04')   # Ctrl+D: execute
        self._ser.flush()

        # Response: b'OK' + stdout + b'\x04' + stderr + b'\x04'
        header = self._ser.read(2)
        if header != b'OK':
            raise RuntimeError('Raw REPL did not respond OK, got: ' + repr(header))

        out = self._read_until(b'\x04')
        err = self._read_until(b'\x04')
        # Consume the trailing '>' prompt the raw REPL sends after each exec
        self._ser.read(1)
        if err:
            raise RuntimeError('MicroPython: ' + err.decode(errors='replace'))
        return out

    def _read_until(self, sentinel):
        buf = bytearray()
        while True:
            c = self._ser.read(1)
            if not c:
                raise TimeoutError('Timeout reading REPL response')
            if c == sentinel:
                return bytes(buf)
            buf.extend(c)

    # ── filesystem helpers ────────────────────────────────────────────────────

    def makedirs(self, path):
        self.exec(
            "import os\n"
            "_c=''\n"
            "for _p in {!r}.strip('/').split('/'):\n"
            "    _c+='/'+_p\n"
            "    (lambda:None)()\n"
            "    try:os.mkdir(_c)\n"
            "    except:pass\n".format(path)
        )

    def put_file(self, local_path, remote_path, on_progress=None):
        """Upload a local file to the device at remote_path."""
        with open(local_path, 'rb') as f:
            data = f.read()

        total = len(data)
        remote_dir = '/'.join(remote_path.replace('\\', '/').split('/')[:-1])
        if remote_dir:
            self.makedirs(remote_dir)

        # Open file on device
        self.exec("_f=open({!r},'wb')".format(remote_path))

        sent = 0
        while sent < total:
            chunk = data[sent:sent + self.CHUNK_BINARY]
            b64 = base64.b64encode(chunk).decode()
            self.exec(
                "import ubinascii as _u\n"
                "_f.write(_u.a2b_base64({!r}))\n".format(b64)
            )
            sent += len(chunk)
            if on_progress:
                on_progress(sent, total)

        self.exec("_f.close()\ndel _f")
        if on_progress:
            on_progress(total, total)

    def write_text(self, remote_path, content):
        """Write a string directly to a file on the device."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py',
                                         delete=False, encoding='utf-8') as tf:
            tf.write(content)
            tmp = tf.name
        try:
            self.put_file(tmp, remote_path)
        finally:
            os.unlink(tmp)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()


# ── SerialOTATransport ────────────────────────────────────────────────────────

# Minimal inline OTA server injected into the device via raw REPL.
# Reads OTA protocol commands from sys.stdin and writes responses to
# sys.stdout, which in raw REPL mode are the raw UART bytes.
# latin-1 encoding is used for binary transparency (0x00-0xFF ↔ 1 byte).
_INLINE_SERVER = (
    # Use .buffer on stdin/stdout for raw binary access — text-mode sys.stdout
    # translates \n → \r\n which corrupts binary file transfers.  .buffer bypasses
    # that.  Falls back to the text stream on older MicroPython builds that lack it.
    # Device print() still shares this UART; host filters those lines in read_line().
    'import sys as _s\n'
    'try:\n'
    ' _out=_s.stdout.buffer;_inp=_s.stdin.buffer\n'
    'except AttributeError:\n'
    ' _out=_s.stdout;_inp=_s.stdin\n'
    'class _C:\n'
    ' def recv(self,n):\n'
    '  b=b""\n'
    '  while len(b)<n:\n'
    '   c=_inp.read(1)\n'
    '   if isinstance(c,str):c=c.encode("latin-1")\n'
    '   b+=c\n'
    '  return b\n'
    ' def sendall(self,d):\n'
    '  if isinstance(d,str):d=d.encode("latin-1")\n'
    '  _out.write(d)\n'
    ' def close(self):pass\n'
    'from ota import _handle as _h\n'
    'while True:_h(_C())\n'
)


class SerialOTATransport:
    """
    OTA transport over USB serial (UART0) using raw REPL injection.

    Usage is identical to WiFiTCPTransport — connect() / read_line() /
    write_line() / read_exact() / write() / close().

    connect() enters raw REPL, executes the inline OTA server on the
    device, and verifies readiness. After that the caller speaks the
    normal micro-ota protocol over the serial port.

    close() sends Ctrl-C + Ctrl-B to restore the interactive REPL.
    """

    def __init__(self, port, baud=115200, timeout=10):
        self.port    = port
        self.baud    = baud
        self.timeout = timeout
        self._ser    = None

    # ── connection ────────────────────────────────────────────────────────────

    def connect(self):
        self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        time.sleep(0.5)

        # Interrupt any running code and flush
        self._ser.write(b'\r\x03\x03')
        self._ser.flush()
        time.sleep(0.3)
        self._ser.reset_input_buffer()

        # Enter raw REPL
        self._ser.write(b'\x01')
        self._ser.flush()
        time.sleep(0.2)
        banner = self._ser.read(200)
        if b'raw REPL' not in banner:
            raise RuntimeError(
                'Could not enter raw REPL. Got: ' + repr(banner) +
                '\nCheck port/baud or press Reset on the device.'
            )

        # Inject inline OTA server
        self._ser.write(_INLINE_SERVER.encode())
        self._ser.write(b'\x04')   # Ctrl+D: execute
        self._ser.flush()

        # Raw REPL replies 'OK' when code starts executing
        header = self._ser.read(2)
        if header != b'OK':
            raise RuntimeError(
                'Inline OTA server failed to start. Got: ' + repr(header)
            )

    def close(self):
        if self._ser and self._ser.is_open:
            try:
                # Ctrl-C interrupts the running loop; Ctrl-B exits raw REPL
                self._ser.write(b'\x03\x02')
                self._ser.flush()
            except Exception:
                pass
            self._ser.close()
        self._ser = None

    # ── protocol primitives ───────────────────────────────────────────────────

    def read_line(self):
        while True:
            buf = bytearray()
            while True:
                c = self._ser.read(1)
                if not c or c == b'\n':
                    break
                if c != b'\r':
                    buf.extend(c)
            line = buf.decode(errors='replace')
            # Skip device debug output (e.g. "[OTA] Manifest: 1 files").
            # sys.stdout cannot be redirected in MicroPython, so debug prints
            # share the UART with protocol responses. All protocol responses
            # are plain words or JSON — they never start with '['.
            if not line.startswith('['):
                return line

    def read_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self._ser.read(min(4096, n - len(buf)))
            if not chunk:
                raise OSError('serial connection closed')
            buf.extend(chunk)
        return bytes(buf)

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        self._ser.write(data)

    def write_line(self, line):
        self.write(line if line.endswith('\n') else line + '\n')

    def __enter__(self):
        if not (self._ser and self._ser.is_open):
            self.connect()
        return self

    def __exit__(self, *_):
        self.close()
