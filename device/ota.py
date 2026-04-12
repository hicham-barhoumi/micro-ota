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
_PROTECTED = frozenset(['ota.py', 'boot_guard.py', 'transports', 'ota.json', 'ota_manifest.json', 'ota_version.json', 'ota_boot_state.json'])


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

def _handle_ota(conn):
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


# ── command terminal ──────────────────────────────────────────────────────────

def _handle(conn):
    line = _read_line(conn)
    parts = line.split(b' ', 1)
    cmd = parts[0]
    arg = parts[1].decode().strip() if len(parts) > 1 else ''

    if cmd == b'ping':
        _send(conn, 'pong\n')

    elif cmd == b'start_ota':
        _handle_ota(conn)

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

    def _make_transport(self):
        cfg = self._config
        for name in cfg.get('transports', ['wifi_tcp']):
            try:
                if name == 'wifi_tcp':
                    from transports.wifi_tcp import WiFiTCPTransport
                    return WiFiTCPTransport(
                        ssid=cfg.get('ssid', ''),
                        password=cfg.get('password', ''),
                        hostname=cfg.get('hostname', 'micropython'),
                        port=cfg.get('port', 2018),
                    )
            except Exception as e:
                print('[OTA] Transport', name, 'unavailable:', e)
        raise RuntimeError('No OTA transport available')

    def run(self):
        transport = self._make_transport()
        while True:
            try:
                transport.start()
                while True:
                    conn = transport.accept()
                    try:
                        _handle(conn)
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

    def run_background(self):
        _thread.start_new_thread(self.run, ())
