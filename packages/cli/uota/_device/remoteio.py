"""
RemoteIO — persistent side-channel TCP server for micro-ota.

Drop this file into the device root as /remoteio.py.

Features:
  • Forwards device print() output to the connected host via os.dupterm()
  • RPC: host calls named handlers registered with @remoteio.on('name')
  • One persistent connection; reconnecting replaces the old one

Protocol (newline-delimited JSON over TCP port 2019):
  Device → Host:  {"t":"print","d":"text"}              print chunk
  Host   → Device:{"t":"call","id":N,"name":"x","args":{}}
  Device → Host:  {"t":"resp","id":N,"r":result}         success
                  {"t":"err", "id":N,"e":"msg"}           error

Usage in user code:
    import remoteio

    @remoteio.on('echo')
    def _(msg=''):
        return msg

    @remoteio.on('free_mem')
    def _():
        import gc; gc.collect()
        return gc.mem_free()

The server is started automatically when run() is called (e.g. from boot.py).
"""

import json
import os
import socket
import _thread
import time


# ── handler registry ──────────────────────────────────────────────────────────

_handlers = {}


def on(name):
    """Decorator: register an RPC handler for the given name."""
    def decorator(fn):
        _handlers[name] = fn
        return fn
    return decorator


# ── built-in handlers ─────────────────────────────────────────────────────────

@on('ping')
def _ping():
    return 'pong'


@on('version')
def _version():
    try:
        with open('/ota_version.json') as f:
            return json.load(f)
    except Exception:
        return {}


@on('free_mem')
def _free_mem():
    import gc
    gc.collect()
    return gc.mem_free()


@on('uptime_ms')
def _uptime():
    return time.ticks_ms()


# ── dupterm print forwarder ───────────────────────────────────────────────────

class _RemoteStream:
    """
    os.dupterm-compatible stream.  Writes are forwarded as {"t":"print"} JSON
    messages over the current active connection.
    Reads always return 0 (we don't forward stdin from the host here).
    """
    def __init__(self):
        self._conn = None
        self._lock = _thread.allocate_lock()

    def attach(self, conn):
        with self._lock:
            self._conn = conn

    def detach(self):
        with self._lock:
            self._conn = None

    def write(self, data):
        with self._lock:
            c = self._conn
        if c is None:
            return
        try:
            if isinstance(data, bytes):
                data = data.decode('utf-8', 'replace')
            msg = '{"t":"print","d":' + json.dumps(data) + '}\n'
            c.sendall(msg.encode())
        except Exception:
            pass

    def readinto(self, buf):
        return 0


_stream = _RemoteStream()


def _dupterm_set(stream):
    """Enable/disable secondary dupterm (index 1).  Silently skips if unsupported."""
    try:
        os.dupterm(stream, 1)
    except Exception:
        pass


# ── connection handler ────────────────────────────────────────────────────────

def _serve(conn):
    """Handle one persistent client connection."""
    _stream.attach(conn)
    _dupterm_set(_stream)
    buf = b''
    try:
        while True:
            chunk = conn.recv(256)
            if not chunk:
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    _dispatch(conn, json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        _dupterm_set(None)
        _stream.detach()
        try:
            conn.close()
        except Exception:
            pass


def _dispatch(conn, msg):
    if msg.get('t') != 'call':
        return
    mid  = msg.get('id')
    name = msg.get('name', '')
    args = msg.get('args') or {}
    handler = _handlers.get(name)
    if handler is None:
        resp = {'t': 'err', 'id': mid, 'e': 'unknown handler: ' + name}
    else:
        try:
            resp = {'t': 'resp', 'id': mid, 'r': handler(**args)}
        except Exception as e:
            resp = {'t': 'err', 'id': mid, 'e': str(e)}
    try:
        conn.sendall((json.dumps(resp) + '\n').encode())
    except Exception:
        pass


# ── server entry point ────────────────────────────────────────────────────────

def run(port=2019):
    """
    Start the RemoteIO TCP server (blocking).
    Call from a background thread in boot.py:

        import _thread, remoteio
        _thread.start_new_thread(remoteio.run, ())
    """
    srv = socket.socket()
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('', port))
        srv.listen(1)
    except Exception:
        srv.close()   # don't leak the fd if bind/listen fails
        raise
    print('[RemoteIO] Listening on :%d' % port)
    try:
        while True:
            try:
                conn, addr = srv.accept()
                print('[RemoteIO] Client connected')
                _serve(conn)
                print('[RemoteIO] Client disconnected')
            except Exception as e:
                print('[RemoteIO] Error:', e)
                time.sleep(1)
    finally:
        srv.close()
