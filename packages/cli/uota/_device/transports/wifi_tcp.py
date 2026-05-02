import network
import socket
import time
import uselect


class WiFiTCPTransport:
    def __init__(self, ssid, password, hostname="micropython", port=2018):
        self.ssid = ssid
        self.password = password
        self.hostname = hostname
        self.port = port
        self._server = None
        self._poll = None

    def start(self):
        self._connect_wifi()
        if self._server is not None:
            return   # socket already open — reuse it
        import gc
        gc.collect()
        try:
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError as e:
            raise OSError('socket() failed errno=' + str(e.args[0]))
        try:
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind(('0.0.0.0', self.port))
            self._server.listen(1)
            self._poll = uselect.poll()
            self._poll.register(self._server, uselect.POLLIN)
        except Exception:
            self._server.close()
            self._server = None
            self._poll = None
            raise
        print('[OTA] TCP server ready on port', self.port)

    def accept(self):
        conn, addr = self._server.accept()
        print('[OTA] Connection from', addr)
        return conn

    def try_accept(self):
        """Return a connection immediately if one is waiting, else None."""
        if self._server is None:
            return None
        if not self._poll.poll(0):
            return None
        conn, addr = self._server.accept()
        print('[OTA] Connection from', addr)
        return conn

    def stop(self):
        if self._server:
            self._server.close()
            self._server = None
        self._poll = None

    def _connect_wifi(self):
        sta = network.WLAN(network.STA_IF)
        if not sta.active():
            sta.active(True)
        if sta.isconnected():
            print('[OTA] WiFi already connected:', sta.ifconfig()[0])
            return
        if not self.ssid:
            raise OSError('No SSID configured and WiFi not connected')
        print('[OTA] Connecting to', self.ssid, '...')
        try:
            sta.config(dhcp_hostname=self.hostname)
        except Exception:
            pass
        sta.connect(self.ssid, self.password)
        deadline = time.time() + 20
        while not sta.isconnected():
            if time.time() > deadline:
                raise OSError('WiFi connection timed out')
            time.sleep(0.5)
        print('[OTA] WiFi connected:', sta.ifconfig()[0])
