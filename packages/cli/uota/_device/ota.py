"""
micro-ota device module.
Drop into the root of your MicroPython filesystem as /ota.py.
Starts a server that accepts OTA pushes from the host tool (uota).

Protocol (all text lines are \n terminated):
  ping                         → pong
  version                      → {"version":"x.y.z"}
  ls [path]                    → newline-separated filenames
  get <path>                   → <size>\n<binary>
  rm <path>                    → ok / error
  reset                        → ok  (then resets)
  wipe                         → ok  (deletes user files, keeps OTA lib)
  start_ota                    → ready  (enters OTA session)
    manifest <size>\n<json>    → ok
    file <name>;<size>;<sha256>\n<binary>  → ok / sha256_mismatch
    ...
    end_ota                    → ok  (commits, resets)
    abort                      → aborted  (discards staging)
"""

import os
import json
import hashlib
import machine
import time
import _thread


_STAGE   = '/ota_stage'
_MANIFEST = '/ota_manifest.json'
_VERSION  = '/ota_version.json'
_PROTECTED = frozenset(['lib', 'boot.py', 'ota.json', 'ota_manifest.json', 'ota_version.json', 'ota_boot_state.json'])


# ── HMAC-SHA256 (pure Python — MicroPython has no hmac module) ────────────────

def _hmac_sha256_hex(key, msg):
    """Return HMAC-SHA256(key, msg) as a lowercase hex string."""
    if isinstance(key, str):
        key = key.encode()
    if isinstance(msg, str):
        msg = msg.encode()
    B = 64
    if len(key) > B:
        h = hashlib.sha256(); h.update(key); key = h.digest()
    key = key + bytes(B - len(key))
    ipad = bytes(b ^ 0x36 for b in key)
    opad = bytes(b ^ 0x5C for b in key)
    inner = hashlib.sha256(); inner.update(ipad); inner.update(msg)
    outer = hashlib.sha256(); outer.update(opad); outer.update(inner.digest())
    return ''.join('%02x' % b for b in outer.digest())


class _HMACStream:
    """
    Incremental HMAC-SHA256.  Feed data chunk by chunk via update();
    call digest() once at the end to get the 32-byte result.
    Used to verify the stream_ota HMAC trailer without buffering the payload.
    """
    def __init__(self, key):
        if isinstance(key, str):
            key = key.encode()
        B = 64
        if len(key) > B:
            h = hashlib.sha256()
            h.update(key)
            key = h.digest()
        key = key + bytes(B - len(key))
        self._opad  = bytes(b ^ 0x5C for b in key)
        self._inner = hashlib.sha256()
        self._inner.update(bytes(b ^ 0x36 for b in key))

    def update(self, data):
        self._inner.update(data)

    def digest(self):
        outer = hashlib.sha256()
        outer.update(self._opad)
        outer.update(self._inner.digest())
        return bytes(outer.digest())


def _signing_payload(manifest):
    """Same canonical form as the host: version\npath:sha256\n..."""
    lines = [manifest.get('version', '')]
    for path in sorted(manifest.get('files', {})):
        lines.append('{}:{}'.format(path, manifest['files'][path]['sha256']))
    return '\n'.join(lines)


def _verify_manifest_sig(manifest, key):
    """Return True if sig is valid or key is empty (backward compat)."""
    if not key:
        return True
    expected = _hmac_sha256_hex(key, _signing_payload(manifest))
    return manifest.get('sig', '') == expected


# ── helpers ──────────────────────────────────────────────────────────────────

def _is_dir(path):
    try:
        return os.stat(path)[0] & 0x4000 != 0
    except OSError:
        return False


def _makedirs(path):
    parts = [p for p in path.split('/') if p]
    cur = ''
    for p in parts:
        cur += '/' + p
        try:
            os.mkdir(cur)
        except OSError:
            pass


def _remove_tree(path):
    try:
        if _is_dir(path):
            for e in os.listdir(path):
                _remove_tree(path + '/' + e)
            os.rmdir(path)
        else:
            os.remove(path)
    except OSError:
        pass


def _sha256_hex(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(512)
            if not chunk:
                break
            h.update(chunk)
    return ''.join('%02x' % b for b in h.digest())


def _read_line(conn):
    buf = bytearray()
    while True:
        c = conn.recv(1)
        if not c or c == b'\n':
            break
        if c != b'\r':
            buf.extend(c)
    return bytes(buf)


def _read_exact(conn, n):
    buf = bytearray(n)
    mv = memoryview(buf)
    pos = 0
    while pos < n:
        chunk = conn.recv(min(512, n - pos))
        if not chunk:
            raise OSError('connection closed')
        mv[pos:pos + len(chunk)] = chunk
        pos += len(chunk)
    return bytes(buf)


def _send(conn, msg):
    if isinstance(msg, str):
        msg = msg.encode()
    conn.sendall(msg)


# ── staging ───────────────────────────────────────────────────────────────────

def _stage_path(filename):
    return _STAGE + '/' + filename.lstrip('/')


def _walk_stage():
    """Yield (stage_path, final_path) for every file under _STAGE."""
    def _walk(d):
        for entry in os.listdir(d):
            full = d + '/' + entry
            if _is_dir(full):
                yield from _walk(full)
            else:
                yield full, full[len(_STAGE):]   # final_path keeps leading /
    if _is_dir(_STAGE):
        yield from _walk(_STAGE)


# ── OTA session ───────────────────────────────────────────────────────────────

def _handle_ota(conn, cfg):
    _send(conn, 'ready\n')
    new_manifest = None
    staged = []          # list of final_paths that were staged

    try:
        while True:
            header = _read_line(conn)

            if header == b'abort':
                _remove_tree(_STAGE)
                _send(conn, 'aborted\n')
                print('[OTA] Aborted')
                return

            if header == b'end_ota':
                _commit(new_manifest, staged)
                _send(conn, 'ok\n')
                time.sleep(0.3)
                machine.reset()
                return

            parts = header.split(b' ', 1)
            cmd = parts[0]
            arg = parts[1].decode() if len(parts) > 1 else ''

            if cmd == b'manifest':
                data = _read_exact(conn, int(arg))
                new_manifest = json.loads(data)
                key = cfg.get('otaKey', '')
                if not _verify_manifest_sig(new_manifest, key):
                    print('[OTA] Manifest signature invalid — rejecting')
                    _send(conn, 'sig_mismatch\n')
                    _remove_tree(_STAGE)
                    return
                print('[OTA] Manifest:', len(new_manifest.get('files', {})), 'files')
                _send(conn, 'ok\n')

            elif cmd == b'file':
                meta = arg.split(';')
                filename  = meta[0]
                size      = int(meta[1])
                expected  = meta[2] if len(meta) > 2 else None

                sp = _stage_path(filename)
                _makedirs('/'.join(sp.split('/')[:-1]))

                h = hashlib.sha256()
                rem = size
                with open(sp, 'wb') as f:
                    while rem > 0:
                        chunk = conn.recv(min(512, rem))
                        if not chunk:
                            raise OSError('connection dropped')
                        f.write(chunk)
                        h.update(chunk)
                        rem -= len(chunk)

                actual = ''.join('%02x' % b for b in h.digest())
                if expected and actual != expected:
                    os.remove(sp)
                    _send(conn, 'sha256_mismatch ' + filename + '\n')
                    raise OSError('SHA256 mismatch: ' + filename)

                staged.append('/' + filename.lstrip('/'))
                print('[OTA] Staged:', filename, '(%d B)' % size)
                _send(conn, 'ok\n')

            else:
                _send(conn, 'unknown\n')

    except Exception as e:
        print('[OTA] Session error:', e)
        _remove_tree(_STAGE)
        try:
            _send(conn, 'error: ' + str(e) + '\n')
        except Exception:
            pass


def _commit(new_manifest, staged):
    # 1. Load old manifest → find files to delete
    old_files = set()
    try:
        with open(_MANIFEST, 'r') as f:
            old_files = set(json.load(f).get('files', {}).keys())
    except OSError:
        pass

    new_files = set(new_manifest.get('files', {}).keys()) if new_manifest else set()
    for rel in old_files - new_files:
        path = '/' + rel.lstrip('/')
        try:
            os.remove(path)
            print('[OTA] Removed old file:', path)
        except OSError:
            pass

    # 2. Move staged files to final locations
    for stage_path, final_path in _walk_stage():
        _makedirs('/'.join(final_path.split('/')[:-1]))
        try:
            os.remove(final_path)
        except OSError:
            pass
        os.rename(stage_path, final_path)
        print('[OTA] Installed:', final_path)

    _remove_tree(_STAGE)

    # 3. Persist manifest and version
    if new_manifest:
        with open(_MANIFEST, 'w') as f:
            json.dump(new_manifest, f)
        version = new_manifest.get('version', 'unknown')
        with open(_VERSION, 'w') as f:
            json.dump({'version': version}, f)
        print('[OTA] Committed version:', version)


# ── streaming OTA session ────────────────────────────────────────────────────

def _handle_stream_ota(conn, cfg):
    """
    Handle a stream_ota session.

    Wire format (received after the 'ready' acknowledgement):
      [4B]  magic  b'OTAS'
      [2B]  version_len
      [N]   version (UTF-8)
      [2B]  file_count
      ── file_count times ──
      [2B]  path_len
      [N]   path
      [4B]  file_size
      [32B] sha256  (binary)
      [M]   file data   ← written to staging 512 B at a time, no full-file buffer
      ──────────────────
      [32B] HMAC-SHA256 trailer  (covers version_len..end-of-data; zeros = no auth)

    Staging is discarded on any per-file sha256 mismatch or HMAC failure so
    the device is never left in a partially-updated state.
    """
    _send(conn, 'ready\n')

    magic = _read_exact(conn, 4)
    if magic != b'OTAS':
        _send(conn, 'error: bad magic\n')
        return

    key = cfg.get('otaKey', '')
    hm  = _HMACStream(key) if key else None

    def _ru16():
        b = _read_exact(conn, 2)
        if hm: hm.update(b)
        return (b[0] << 8) | b[1]

    def _ru32():
        b = _read_exact(conn, 4)
        if hm: hm.update(b)
        return (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]

    def _rn(n):
        b = _read_exact(conn, n)
        if hm: hm.update(b)
        return b

    # version
    version = _rn(_ru16()).decode('utf-8', 'replace')

    # file_count
    file_count   = _ru16()
    new_manifest = {'version': version, 'files': {}}
    _remove_tree(_STAGE)

    try:
        for _ in range(file_count):
            path     = _rn(_ru16()).decode('utf-8', 'replace')
            size     = _ru32()
            sha_b    = _rn(32)          # expected sha256 as 32 raw bytes

            sp = _stage_path(path)
            _makedirs('/'.join(sp.split('/')[:-1]))

            fh  = hashlib.sha256()
            rem = size
            with open(sp, 'wb') as f:
                while rem > 0:
                    chunk = conn.recv(min(512, rem))
                    if not chunk:
                        raise OSError('connection dropped')
                    f.write(chunk)
                    fh.update(chunk)
                    if hm: hm.update(chunk)
                    rem -= len(chunk)

            if bytes(fh.digest()) != sha_b:
                _remove_tree(_STAGE)
                _send(conn, 'sha256_mismatch ' + path + '\n')
                print('[OTA] SHA256 mismatch:', path)
                return

            sha_hex = ''.join('%02x' % b for b in sha_b)
            new_manifest['files'][path] = {'sha256': sha_hex, 'size': size}
            print('[OTA] Staged:', path, '(%d B)' % size)

        # verify HMAC trailer
        trailer = _read_exact(conn, 32)
        if hm and hm.digest() != trailer:
            _remove_tree(_STAGE)
            _send(conn, 'sig_mismatch\n')
            print('[OTA] Stream HMAC mismatch — update rejected')
            return

        _commit(new_manifest, list(new_manifest['files'].keys()))
        _send(conn, 'ok\n')
        time.sleep(0.3)
        machine.reset()

    except Exception as e:
        print('[OTA] Stream error:', e)
        _remove_tree(_STAGE)
        try:
            _send(conn, 'error: ' + str(e) + '\n')
        except Exception:
            pass


# ── command terminal ──────────────────────────────────────────────────────────

def _handle(conn, cfg):
    line = _read_line(conn)
    parts = line.split(b' ', 1)
    cmd = parts[0]
    arg = parts[1].decode().strip() if len(parts) > 1 else ''

    if cmd == b'ping':
        _send(conn, 'pong\n')

    elif cmd == b'stream_ota':
        _handle_stream_ota(conn, cfg)

    elif cmd == b'start_ota':
        _handle_ota(conn, cfg)

    elif cmd == b'version':
        try:
            with open(_VERSION, 'r') as f:
                _send(conn, f.read() + '\n')
        except OSError:
            _send(conn, '{"version":"unknown"}\n')

    elif cmd == b'ls':
        path = arg if arg else '/'
        try:
            entries = os.listdir(path)
            _send(conn, '\n'.join(entries) + '\n')
        except OSError as e:
            _send(conn, 'error: ' + str(e) + '\n')

    elif cmd == b'get':
        try:
            with open(arg, 'rb') as f:
                data = f.read()
            _send(conn, str(len(data)) + '\n')
            _send(conn, data)
        except OSError:
            _send(conn, 'error\n')

    elif cmd == b'rm':
        try:
            os.remove(arg)
            _send(conn, 'ok\n')
        except OSError as e:
            _send(conn, 'error: ' + str(e) + '\n')

    elif cmd == b'reset':
        _send(conn, 'ok\n')
        time.sleep(0.3)
        machine.reset()

    elif cmd == b'wipe':
        for item in os.listdir('/'):
            if item not in _PROTECTED:
                _remove_tree('/' + item)
        _send(conn, 'ok\n')

    else:
        _send(conn, 'unknown\n')


# ── serial REPL entry point ───────────────────────────────────────────────────

def serve_serial():
    """
    Serve the OTA protocol over UART0 (the USB / REPL port) when the host
    enters raw REPL and runs:  import ota; ota.serve_serial()

    Provides the same ACK-based flow control and escape decoding as the
    inline server injected during bootstrap, but re-uses all the protocol
    handlers already in this module instead of duplicating them.

    The host's SerialOTATransport.connect() tries this first; it falls back
    to injecting _inline_server.py only when ota.py is absent (un-bootstrapped
    device or very old firmware).
    """
    import sys
    import uselect

    try:
        _out = sys.stdout.buffer
        _in  = sys.stdin.buffer
    except AttributeError:
        _out = sys.stdout
        _in  = sys.stdin

    # Best-effort: suppress print() so device debug lines don't corrupt the
    # binary protocol.  Two attempts:
    #   1. Redirect sys.stdout (fails on frozen/read-only sys builds).
    #   2. Shadow builtins.print (fails on some minimal builds).
    # If both fail the host's ACK-wait loop drains non-ACK bytes, so the
    # protocol still works — just slightly slower.
    class _Null:
        def write(self, *a): pass
        def flush(self, *a): pass
    _suppressed = False
    try:
        sys.stdout = _Null()
        _suppressed = True
    except AttributeError:
        pass
    if not _suppressed:
        try:
            import builtins
            builtins.print = lambda *a, **kw: None
        except (AttributeError, ImportError):
            pass

    _poll = uselect.poll()
    _poll.register(_in, uselect.POLLIN)
    _ACK = b'\x06'
    _ESC = 27

    class _Conn:
        def _read1(self, tms=100):
            if _poll.poll(0):          # byte ready — fast path, no ACK
                b = _in.read(1)
                if b:
                    return b[0]
            # Buffer empty — tell host to send the next window, then wait.
            _out.write(_ACK)
            try: _out.flush()
            except: pass
            if not _poll.poll(tms): raise OSError('recv timeout1')
            b = _in.read(1)
            if not b: raise OSError('recv timeout2')
            return b[0]

        def recv(self, n):
            buf = bytearray(n)
            for i in range(n):
                b = self._read1()
                if b == _ESC:
                    nx = self._read1(500)
                    b = 3 if nx == ord('C') else 4 if nx == ord('D') else nx
                buf[i] = b
            return bytes(buf)

        def sendall(self, data):
            if isinstance(data, str):
                data = data.encode('latin-1')
            _out.write(data)
            try: _out.flush()
            except: pass

        def close(self): pass

    cfg = {}
    try:
        cfg = json.load(open('/ota.json'))
    except Exception:
        pass

    while True:
        try:
            _handle(_Conn(), cfg)
        except Exception as e:
            try:
                _out.write(('[ERR] ' + str(e) + '\n').encode())
                _out.flush()
            except Exception:
                pass


# ── main ──────────────────────────────────────────────────────────────────────

class OTAUpdater:
    def __init__(self):
        self._config = self._load_config()
        # Clean up any leftover staging dir from a previous interrupted OTA
        _remove_tree(_STAGE)

    def _load_config(self):
        try:
            with open('ota.json', 'r') as f:
                return json.load(f)
        except OSError:
            return {}

    def _make_transports(self):
        """Return a list of all configured transports that can be instantiated."""
        cfg = self._config
        result = []
        for name in cfg.get('transports', ['wifi_tcp']):
            try:
                if name == 'wifi_tcp':
                    from .transports.wifi_tcp import WiFiTCPTransport
                    result.append(WiFiTCPTransport(
                        ssid=cfg.get('ssid', ''),
                        password=cfg.get('password', ''),
                        hostname=cfg.get('hostname', 'micropython'),
                        port=cfg.get('port', 2018),
                    ))
                elif name == 'serial':
                    from .transports.serial import SerialTransport
                    result.append(SerialTransport(
                        uart_id=cfg.get('serialUartId', 1),
                        baud=cfg.get('serialBaud', 115200),
                        tx=cfg.get('serialTx', 10),
                        rx=cfg.get('serialRx', 9),
                    ))
                elif name == 'http_pull':
                    url = cfg.get('manifestUrl', '')
                    if not url:
                        print('[OTA] http_pull requires manifestUrl in ota.json')
                        continue
                    from .transports.http_pull import HttpPullTransport
                    result.append(HttpPullTransport(
                        manifest_url=url,
                        interval=cfg.get('pullInterval', 60),
                        timeout=cfg.get('pullTimeout', 15),
                    ))
                elif name == 'ble':
                    from .transports.ble import BLETransport
                    result.append(BLETransport(
                        name=cfg.get('bleName', 'micro-ota'),
                    ))
                else:
                    print('[OTA] Unknown transport:', name)
            except Exception as e:
                print('[OTA] Transport', name, 'unavailable:', e)
        if not result:
            raise RuntimeError('No OTA transport available')
        return result

    def _run_transport(self, transport):
        """Serve one transport forever, restarting on errors."""
        if getattr(transport, 'is_pull', False):
            # Pull-mode transport: poll() on an interval instead of accept() loop
            print('[OTA] Pull transport started, interval=%ds' % transport.interval)
            while True:
                try:
                    transport.poll()
                except Exception as e:
                    print('[OTA] Poll error:', e)
                time.sleep(transport.interval)
            return

        # Push-mode transport: accept incoming connections
        while True:
            try:
                transport.start()
                while True:
                    conn = transport.accept()
                    try:
                        _handle(conn, self._config)
                    except Exception as e:
                        print('[OTA] Handler error:', e)
                    finally:
                        try:
                            conn.close()
                        except Exception:
                            pass
            except Exception as e:
                print('[OTA] Transport error:', e)
                try:
                    transport.stop()
                except Exception:
                    pass
                time.sleep(3)

    def run(self):
        """Start all configured transports. Each runs in its own thread."""
        transports = self._make_transports()
        # Spawn a thread for every transport except the last, which runs here.
        for t in transports[:-1]:
            _thread.start_new_thread(self._run_transport, (t,))
        self._run_transport(transports[-1])

    def run_background(self):
        _thread.start_new_thread(self.run, ())
