import socket


class WiFiTCPTransport:
    """Host-side WiFi TCP connection to the device OTA server."""

    def __init__(self, host, port=2018, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None

    def connect(self):
        # Resolve mDNS hostname if needed
        try:
            ip = socket.gethostbyname(self.host)
        except OSError:
            ip = self.host
        self._sock = socket.create_connection((ip, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)

    def read_line(self):
        buf = bytearray()
        while True:
            c = self._sock.recv(1)
            if not c or c == b'\n':
                break
            if c != b'\r':
                buf.extend(c)
        return buf.decode()

    def read_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(min(4096, n - len(buf)))
            if not chunk:
                raise OSError('connection closed')
            buf.extend(chunk)
        return bytes(buf)

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        self._sock.sendall(data)

    def write_line(self, line):
        self.write(line if line.endswith('\n') else line + '\n')

    def close(self):
        if self._sock:
            self._sock.close()
            self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()
