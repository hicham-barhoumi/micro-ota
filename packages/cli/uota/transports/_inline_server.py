import sys
import os
import json
import hashlib
import machine
import time
import uselect

try:
    stdout = sys.stdout.buffer
    stdin  = sys.stdin.buffer
except AttributeError:
    stdout = sys.stdout
    stdin  = sys.stdin

# ── I/O helpers ───────────────────────────────────────────────────────────────

_poll = uselect.poll()
_poll.register(stdin, uselect.POLLIN)

ESCAPE = 27   # \x1b — starts a two-byte escape sequence on the wire
ACK    = b'\x06'  # sent to host whenever the RX buffer drains (flow control)
# Host escaping: \x1b→\x1b\x1b, \x03→\x1bC, \x04→\x1bD


class Connection:
    """Stdin/stdout wrapper with escape decoding and ACK flow control.

    The host sends data in 64-byte windows and pauses after each window.
    _read1() sends ACK whenever poll(0) reports the RX buffer is empty,
    which signals the host that all pending bytes have been consumed and
    it may release the next window.  This prevents UART overflow regardless
    of how fast (or slow) the device reads.
    """

    def _read1(self, timeout_ms=100):
        if _poll.poll(0):        # byte already waiting — fast path, no ACK
            b = stdin.read(1)
            if b:
                return b[0]
        # Buffer empty — tell host to send the next window, then wait.
        stdout.write(ACK)
        try:
            stdout.flush()
        except Exception:
            pass
        if not _poll.poll(timeout_ms):
            raise OSError('recv timeout')
        b = stdin.read(1)
        if not b:
            raise OSError('recv timeout')
        return b[0]

    def recv(self, n):
        out = bytearray(n)
        for i in range(n):
            b = self._read1()
            if b == ESCAPE:
                nx = self._read1(500)
                b = 3 if nx == ord('C') else 4 if nx == ord('D') else nx
            out[i] = b
        return bytes(out)

    def sendall(self, data):
        if isinstance(data, str):
            data = data.encode('latin-1')
        stdout.write(data)
        try:
            stdout.flush()
        except Exception:
            pass

    def close(self):
        pass


def read_line(conn):
    """Read a newline-terminated line from conn, stripping CR/LF."""
    buf = bytearray()
    while True:
        b = conn.recv(1)
        if b == b'\n':
            break
        if b != b'\r':
            buf.extend(b)
    return bytes(buf)


def read_exact(conn, n):
    """Read exactly n bytes from conn."""
    buf = bytearray(n)
    mv  = memoryview(buf)
    pos = 0
    while pos < n:
        chunk = conn.recv(min(512, n - pos))
        mv[pos:pos + len(chunk)] = chunk
        pos += len(chunk)
    return bytes(buf)


def send(conn, msg):
    if isinstance(msg, str):
        msg = msg.encode()
    conn.sendall(msg)


# ── filesystem helpers ────────────────────────────────────────────────────────

def is_dir(path):
    try:
        return os.stat(path)[0] & 0x4000 != 0
    except Exception:
        return False


def makedirs(path):
    cur = ''
    for part in [p for p in path.split('/') if p]:
        cur += '/' + part
        try:
            os.mkdir(cur)
        except Exception:
            pass


def remove_tree(path):
    try:
        if is_dir(path):
            for entry in os.listdir(path):
                remove_tree(path + '/' + entry)
            os.rmdir(path)
        else:
            os.remove(path)
    except Exception:
        pass


# ── HMAC-SHA256 ───────────────────────────────────────────────────────────────

def hmac_hex(key, msg):
    if isinstance(key, str):
        key = key.encode()
    if isinstance(msg, str):
        msg = msg.encode()
    BLOCK = 64
    if len(key) > BLOCK:
        h = hashlib.sha256()
        h.update(key)
        key = h.digest()
    key = key + bytes(BLOCK - len(key))
    ipad = bytes(b ^ 0x36 for b in key)
    opad = bytes(b ^ 0x5C for b in key)
    inner = hashlib.sha256()
    inner.update(ipad)
    inner.update(msg)
    outer = hashlib.sha256()
    outer.update(opad)
    outer.update(inner.digest())
    return ''.join('%02x' % b for b in outer.digest())


class HmacState:
    """Streaming HMAC-SHA256 — accumulates data then produces digest."""

    def __init__(self, key):
        if isinstance(key, str):
            key = key.encode()
        BLOCK = 64
        if len(key) > BLOCK:
            h = hashlib.sha256()
            h.update(key)
            key = h.digest()
        key = key + bytes(BLOCK - len(key))
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


def verify_sig(manifest, key):
    if not key:
        return True
    parts = [manifest.get('version', '')]
    for path in sorted(manifest.get('files', {})):
        parts.append('{}:{}'.format(path, manifest['files'][path]['sha256']))
    return manifest.get('sig', '') == hmac_hex(key, '\n'.join(parts))


# ── OTA state ─────────────────────────────────────────────────────────────────

try:
    config = json.load(open('/config/ota.json'))
except Exception:
    config = {}

STAGE_DIR = '/ota_stage'



def commit(manifest):
    """Move all staged files to their final locations and update manifests."""
    try:
        old_files = set(json.load(open('/ota_manifest.json')).get('files', {}).keys())
    except Exception:
        old_files = set()

    # Remove files that are no longer in the manifest
    for path in old_files - set(manifest.get('files', {}).keys()):
        try:
            os.remove('/' + path.lstrip('/'))
        except Exception:
            pass

    # Walk staging dir and collect (staged_path, final_path) pairs
    pairs = []

    def walk(directory, accumulator):
        for entry in os.listdir(directory):
            full = directory + '/' + entry
            if is_dir(full):
                walk(full, accumulator)
            else:
                accumulator.append((full, full[len(STAGE_DIR):]))

    if is_dir(STAGE_DIR):
        walk(STAGE_DIR, pairs)

    for staged, final in pairs:
        makedirs('/'.join(final.split('/')[:-1]))
        try:
            os.remove(final)
        except Exception:
            pass
        os.rename(staged, final)

    remove_tree(STAGE_DIR)

    with open('/ota_manifest.json', 'w') as f:
        json.dump(manifest, f)
    with open('/ota_version.json', 'w') as f:
        json.dump({'version': manifest.get('version', 'unknown')}, f)


# ── legacy OTA (command-based protocol) ──────────────────────────────────────

def handle_ota(conn):
    send(conn, 'ready\n')
    manifest = None
    try:
        while True:
            line = read_line(conn)
            parts = line.split(b' ', 1)
            cmd   = parts[0]
            arg   = parts[1].decode() if len(parts) > 1 else ''

            if cmd == b'abort':
                remove_tree(STAGE_DIR)
                send(conn, 'aborted\n')
                return

            if cmd == b'end_ota':
                if manifest:
                    commit(manifest)
                send(conn, 'ok\n')
                time.sleep(0.3)
                machine.reset()
                return

            if cmd == b'manifest':
                manifest = json.loads(read_exact(conn, int(arg)))
                if not verify_sig(manifest, config.get('signingKey', '')):
                    remove_tree(STAGE_DIR)
                    send(conn, 'sig_mismatch\n')
                    return
                send(conn, 'ok\n')

            elif cmd == b'file':
                meta      = arg.split(';')
                filename  = meta[0]
                size      = int(meta[1])
                expected  = meta[2] if len(meta) > 2 else None
                stage_path = STAGE_DIR + '/' + filename.lstrip('/')
                makedirs('/'.join(stage_path.split('/')[:-1]))
                hasher = hashlib.sha256()
                rem    = size
                with open(stage_path, 'wb') as f:
                    while rem > 0:
                        chunk = conn.recv(min(512, rem))
                        f.write(chunk)
                        hasher.update(chunk)
                        rem -= len(chunk)
                actual = ''.join('%02x' % b for b in hasher.digest())
                if expected and actual != expected:
                    os.remove(stage_path)
                    send(conn, 'sha256_mismatch ' + filename + '\n')
                    raise OSError('sha256 mismatch: ' + filename)
                send(conn, 'ok\n')

            else:
                send(conn, 'unknown\n')

    except Exception as e:
        remove_tree(STAGE_DIR)
        try:
            send(conn, 'error: ' + str(e) + '\n')
        except Exception:
            pass


# ── stream OTA (single-pass binary protocol) ─────────────────────────────────

def handle_stream_ota(conn):
    send(conn, 'ready\n')
    ota_key = config.get('signingKey', '')
    hmac    = HmacState(ota_key) if ota_key else None

    def read_uint(n_bytes):
        data = read_exact(conn, n_bytes)
        if hmac:
            hmac.update(data)
        value = 0
        for byte in data:
            value = (value << 8) | byte
        return value

    def read_bytes(n):
        data = read_exact(conn, n)
        if hmac:
            hmac.update(data)
        return data

    try:
        if read_exact(conn, 4) != b'OTAS':
            send(conn, 'error: bad magic\n')
            return

        version    = read_bytes(read_uint(2)).decode()
        file_count = read_uint(2)
        manifest   = {'version': version, 'files': {}}
        remove_tree(STAGE_DIR)

        for _ in range(file_count):
            path       = read_bytes(read_uint(2)).decode()
            size       = read_uint(4)
            sha_bytes  = read_bytes(32)
            stage_path = STAGE_DIR + '/' + path.lstrip('/')
            makedirs('/'.join(stage_path.split('/')[:-1]))

            hasher = hashlib.sha256()
            rem    = size
            with open(stage_path, 'wb') as f:
                while rem > 0:
                    chunk = conn.recv(min(512, rem))
                    f.write(chunk)
                    hasher.update(chunk)
                    if hmac:
                        hmac.update(chunk)
                    rem -= len(chunk)

            got = bytes(hasher.digest())
            if got != sha_bytes:
                remove_tree(STAGE_DIR)
                got_hex = ''.join('%02x' % b for b in got)
                exp_hex = ''.join('%02x' % b for b in sha_bytes)
                send(conn, 'sha256_mismatch ' + path + ' got=' + got_hex + ' exp=' + exp_hex + '\n')
                return

            manifest['files'][path] = {
                'sha256': ''.join('%02x' % b for b in sha_bytes),
                'size':   size,
            }

        trailer = read_exact(conn, 32)
        if hmac and hmac.digest() != trailer:
            remove_tree(STAGE_DIR)
            send(conn, 'sig_mismatch\n')
            return

        commit(manifest)
        send(conn, 'ok\n')

    except Exception as e:
        remove_tree(STAGE_DIR)
        try:
            send(conn, 'error: ' + str(e) + '\n')
        except Exception:
            pass
        return

    time.sleep(0.3)
    machine.reset()


# ── command dispatcher ────────────────────────────────────────────────────────

def handle_command(conn):
    line  = read_line(conn)
    parts = line.split(b' ', 1)
    cmd   = parts[0]
    arg   = parts[1].decode().strip() if len(parts) > 1 else ''

    if cmd == b'ping':
        send(conn, 'pong\n')

    elif cmd == b'stream_ota':
        handle_stream_ota(conn)

    elif cmd == b'start_ota':
        handle_ota(conn)

    elif cmd == b'version':
        try:
            send(conn, open('/ota_version.json').read() + '\n')
        except Exception:
            send(conn, '{"version":"unknown"}\n')

    elif cmd == b'ls':
        try:
            path = arg or '/'
            base = path.rstrip('/')
            entries = []
            for name in os.listdir(path):
                try:
                    if os.stat(base + '/' + name)[0] & 0x4000:
                        entries.append(name + '/')
                    else:
                        entries.append(name)
                except Exception:
                    entries.append(name)
            send(conn, '\n'.join(entries) + '\n\n')  # blank line = end of listing
        except Exception:
            send(conn, 'error\n')

    elif cmd == b'get':
        try:
            data = open(arg, 'rb').read()
            send(conn, str(len(data)) + '\n')
            send(conn, data)
        except Exception:
            send(conn, 'error\n')

    elif cmd == b'rm':
        try:
            os.remove(arg)
            send(conn, 'ok\n')
        except Exception:
            send(conn, 'error\n')

    elif cmd == b'reset':
        send(conn, 'ok\n')
        time.sleep(0.3)
        machine.reset()

    elif cmd == b'wipe':
        for item in os.listdir('/'):
            remove_tree('/' + item)
        send(conn, 'ok\n')

    else:
        send(conn, 'unknown\n')


# ── main loop ─────────────────────────────────────────────────────────────────

while True:
    try:
        handle_command(Connection())
    except Exception as err:
        try:
            stdout.write(('[ERR] ' + str(err) + '\n').encode())
            stdout.flush()
        except Exception:
            pass
