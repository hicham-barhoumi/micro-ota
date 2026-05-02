"""
Serial transports for micro-ota host.

RawREPL         — raw REPL file uploader, used by bootstrap only.
SerialOTATransport — OTA over USB/UART0 via raw REPL injection.
                   Enters raw REPL, injects an inline OTA server on the
                   device, then speaks the standard micro-ota protocol
                   directly over the serial port. No extra wiring needed.
"""

import base64
import pathlib
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

        # Interrupt any running code, then perform a soft reset so the device
        # boots fresh with no background threads (OTA thread, remoteio …).
        # Without this, threads started by boot.py keep running and their
        # UART0 writes corrupt the raw REPL framing mid-upload.
        self._ser.write(b'\r\x03\x03')   # Ctrl-C × 2: interrupt current code
        self._ser.flush()
        time.sleep(0.2)
        self._ser.write(b'\x02')         # Ctrl-B: exit raw REPL → normal REPL
        self._ser.flush()
        time.sleep(0.05)
        self._ser.write(b'\x04')         # Ctrl-D: soft reset
        self._ser.flush()
        # Spam Ctrl-C for 2 s during the reboot so we interrupt boot.py
        # before _thread.start_new_thread() is reached.  This prevents the
        # OTA background thread from being spawned at all.
        t0 = time.time()
        while time.time() - t0 < 2.0:
            self._ser.write(b'\x03')
            self._ser.flush()
            time.sleep(0.05)
        self._ser.reset_input_buffer()

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
        # Try several times: the device might still be printing boot messages
        # or running import statements when we first send Ctrl+A.
        data = b''
        for attempt in range(5):
            self._ser.write(b'\x01')   # Ctrl+A: enter raw REPL
            self._ser.flush()
            time.sleep(0.3)
            data = self._ser.read(self._ser.in_waiting or 1)
            if b'raw REPL' in data:
                return
            # Not ready yet — interrupt any pending code and retry.
            self._interrupt()
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
        # Background threads (OTA, RemoteIO) may write to the UART concurrently
        # and their output can arrive before the raw REPL 'OK'.  Scan forward
        # until we find 'OK' rather than assuming it starts at byte 0.
        buf = bytearray()
        t0 = time.time()
        found = False
        while time.time() - t0 < self.timeout:
            c = self._ser.read(1)
            if c:
                buf.extend(c)
                if len(buf) >= 2 and buf[-2:] == b'OK':
                    found = True
                    break
        if not found:
            raise RuntimeError('Raw REPL did not respond OK, got: ' + repr(bytes(buf)))

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

# Fallback inline OTA server — injected only when ota.py is absent on the
# device (bootstrap flow or un-bootstrapped device).  Source lives in
# _inline_server.py for syntax highlighting and independent testing.
#
# On bootstrapped devices SerialOTATransport.connect() instead invokes
#   import ota; ota.serve_serial()
# which re-uses all the protocol handlers already in ota.py without uploading
# any extra code.
_INLINE_SERVER = (pathlib.Path(__file__).parent / '_inline_server.py').read_text()


class SerialOTATransport:
    """
    OTA transport over USB serial (UART0) using raw REPL injection.

    Usage is identical to WiFiTCPTransport — connect() / read_line() /
    write_line() / read_exact() / write() / close().

    connect() enters raw REPL then:
      1. Tries  import ota; ota.serve_serial()  (fast — no code upload needed
         on already-bootstrapped devices).
      2. Falls back to injecting the self-contained _inline_server.py when
         ota.py is absent (un-bootstrapped device or bootstrap itself).

    After connect() returns, the caller speaks the normal micro-ota protocol.
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

        # Interrupt any running code.
        # Send Ctrl+C twice to kill any executing code, then Ctrl+B to exit
        # raw REPL in case the device was left in raw REPL mode by a previous
        # session (Ctrl+A does nothing when already in raw REPL, so we must
        # normalise the state first).
        self._ser.write(b'\r\x03\x03')
        self._ser.flush()
        time.sleep(0.3)
        self._ser.write(b'\x02')       # Ctrl+B: exit raw REPL → normal REPL
        self._ser.flush()
        time.sleep(0.1)
        self._ser.reset_input_buffer()

        # Enter raw REPL
        self._ser.write(b'\x01')       # Ctrl+A: enter raw REPL
        self._ser.flush()
        time.sleep(0.2)
        banner = self._ser.read(self._ser.in_waiting or 1)
        if b'raw REPL' not in banner:
            raise RuntimeError(
                'Could not enter raw REPL. Got: ' + repr(banner) +
                '\nCheck port/baud or press Reset on the device.'
            )

        # Fast path: invoke ota.serve_serial() already on the device.
        # This avoids injecting ~13 KB of inline server code on every call.
        # Falls back to the inline server when uota is absent (bootstrap).
        self._ser.write(b'from uota.ota import serve_serial as _s; _s()')
        self._ser.write(b'\x04')   # Ctrl+D: execute
        self._ser.flush()

        if not self._wait_for_ok(timeout=5):
            raise RuntimeError('Raw REPL did not respond OK during connect.')

        # If ota.py is missing, raw REPL immediately sends \x04 (end-of-stdout)
        # followed by the ImportError in stderr.  If serve_serial() is running,
        # it enters its loop and sends no \x04 — only \x06 ACK bytes.
        # Peek for 0.2 s: a \x04 means failure → fall back to inline server.
        time.sleep(0.2)
        peeked = self._ser.read(self._ser.in_waiting)   # non-blocking peek
        if b'\x04' in peeked:
            # Device still in raw REPL — inject self-contained inline server.
            self._ser.reset_input_buffer()
            self._ser.write(_INLINE_SERVER.encode())
            self._ser.write(b'\x04')
            self._ser.flush()
            if not self._wait_for_ok(timeout=5):
                raise RuntimeError('Inline OTA server failed to start.')

    def _wait_for_ok(self, timeout=5):
        """Scan the serial stream until the raw REPL 'OK' pair appears."""
        buf = bytearray()
        t0 = time.time()
        while time.time() - t0 < timeout:
            c = self._ser.read(1)
            if c:
                buf.extend(c)
                if len(buf) >= 2 and buf[-2:] == b'OK':
                    return True
        return False

    def close(self):
        if self._ser and self._ser.is_open:
            try:
                # Ctrl+C stops serve_serial() — it now raises KeyboardInterrupt,
                # which leaves the device in normal REPL mode (not raw REPL).
                # Re-enter raw REPL with Ctrl+A, then execute machine.reset().
                # Hard reset clears LWIP sockets that a soft reset (Ctrl+D) leaves
                # dangling, which would exhaust LWIP_MAX_SOCKETS after a few sessions.
                self._ser.write(b'\x03')
                self._ser.flush()
                time.sleep(0.3)
                self._ser.write(b'\x01')    # Ctrl+A: enter raw REPL
                self._ser.flush()
                time.sleep(0.2)
                self._ser.write(b'import machine;machine.reset()\x04')
                self._ser.flush()
                time.sleep(0.5)
            except Exception:
                pass
            self._ser.close()
        self._ser = None

    # ── protocol primitives ───────────────────────────────────────────────────

    def read_line(self):
        deadline = time.time() + (getattr(self._ser, 'timeout', self.timeout) or self.timeout) * 3
        while time.time() < deadline:
            buf = bytearray()
            while True:
                c = self._ser.read(1)
                if not c or c == b'\n':
                    break
                if c not in (b'\r', b'\x06'):  # \x06 = flow-control ACK
                    buf.extend(c)
            line = buf.decode(errors='replace')
            # Skip device debug output (e.g. "[OTA] Manifest: 1 files").
            # sys.stdout cannot be redirected in MicroPython, so debug prints
            # share the UART with protocol responses. All protocol responses
            # are plain words or JSON — they never start with '['.
            if not line.startswith('['):
                return line
        return ''

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
        # Escape bytes MicroPython's UART IRQ intercepts during execution:
        #   \x03 → \x1bC  (Ctrl+C → KeyboardInterrupt at hardware level)
        #   \x04 → \x1bD  (Ctrl+D = EOF, may terminate stdin.read)
        #   \x1b → \x1b\x1b  (escape byte itself — must be done first)
        data = (data.replace(b'\x1b', b'\x1b\x1b')
                    .replace(b'\x03', b'\x1bC')
                    .replace(b'\x04', b'\x1bD'))
        self._ser.write(data)
        self._ser.flush()

    def write_line(self, line):
        self.write(line if line.endswith('\n') else line + '\n')

    def __enter__(self):
        if not (self._ser and self._ser.is_open):
            self.connect()
        return self

    def __exit__(self, *_):
        self.close()
