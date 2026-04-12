"""
Raw MicroPython REPL transport.
Used exclusively for bootstrap (first-time upload of OTA lib).
Implements just enough of the raw REPL protocol to write files.
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
