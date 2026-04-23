"""
HTTP pull transport for micro-ota.

The device periodically fetches a manifest from a URL, compares it against
the installed version, downloads only the changed files, and commits them
atomically.  No host connection is required after initial configuration.

ota.json keys used:
    "manifestUrl":   "http://192.168.1.10:8080/manifest.json"
    "pullInterval":  60      # seconds between polls (default 60)

The manifest URL format is the same as the one served by  uota serve  on
the host, e.g. http://host:8080/manifest.json.  Individual files are fetched
from  http://host:8080/<relative-path>.
"""

import hashlib
import json
import os
import socket
import time


# ── tiny HTTP client ──────────────────────────────────────────────────────────

def _http_get(url, stream_to=None, timeout=15):
    """
    Minimal HTTP/1.0 GET.  Returns response body as bytes, or writes it in
    chunks to the file object *stream_to* (for large files).  Raises on
    non-200 status.
    """
    # Parse http://host[:port]/path
    if url.startswith('http://'):
        url = url[7:]
    host_part, _, path = url.partition('/')
    path = '/' + path
    if ':' in host_part:
        host, port = host_part.rsplit(':', 1)
        port = int(port)
    else:
        host, port = host_part, 80

    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(socket.getaddrinfo(host, port)[0][-1])
        req = ('GET %s HTTP/1.0\r\n'
               'Host: %s\r\n'
               'Connection: close\r\n\r\n') % (path, host_part)
        s.send(req.encode())

        # Read header
        hdr = b''
        while b'\r\n\r\n' not in hdr:
            chunk = s.recv(256)
            if not chunk:
                break
            hdr += chunk
        header_end = hdr.find(b'\r\n\r\n')
        status_line = hdr[:hdr.find(b'\r\n')].decode(errors='replace')
        if '200' not in status_line:
            raise OSError('HTTP error: ' + status_line)

        body_start = hdr[header_end + 4:]

        if stream_to is None:
            body = body_start
            while True:
                chunk = s.recv(1024)
                if not chunk:
                    break
                body += chunk
            return body
        else:
            stream_to.write(body_start)
            while True:
                chunk = s.recv(1024)
                if not chunk:
                    break
                stream_to.write(chunk)
            return None
    finally:
        s.close()


# ── filesystem helpers (self-contained, no import from ota.py) ────────────────

_STAGE = '/ota_stage'
_MANIFEST = '/ota_manifest.json'
_VERSION  = '/ota_version.json'
_PROTECTED = frozenset(['lib', 'data', 'config', 'boot.py', 'ota_manifest.json', 'ota_version.json', 'ota_boot_state.json'])


def _makedirs(path):
    parts = path.strip('/').split('/')
    cur = ''
    for p in parts:
        cur += '/' + p
        try:
            os.mkdir(cur)
        except OSError:
            pass


def _is_dir(path):
    try:
        return os.stat(path)[0] & 0x4000 != 0
    except OSError:
        return False


def _remove_tree(path):
    try:
        if _is_dir(path):
            for entry in os.listdir(path):
                _remove_tree(path + '/' + entry)
            os.rmdir(path)
        else:
            os.remove(path)
    except OSError:
        pass


def _walk(d):
    """Yield (full_path, path_relative_to_d) for every file under d."""
    for entry in os.listdir(d):
        full = d + '/' + entry
        if _is_dir(full):
            yield from _walk(full)
        else:
            yield full, full[len(d):]   # rel keeps leading /


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(512)
            if not chunk:
                break
            h.update(chunk)
    return ''.join('%02x' % b for b in h.digest())


def _commit_pull(new_manifest):
    """Atomic commit: move staged files to final locations, update manifest."""
    # 1. Delete files removed from manifest
    try:
        with open(_MANIFEST) as f:
            old_files = set(json.load(f).get('files', {}).keys())
    except OSError:
        old_files = set()
    new_files = set(new_manifest.get('files', {}).keys())
    for rel in old_files - new_files:
        if rel.lstrip('/').split('/')[0] in _PROTECTED:
            continue
        try:
            os.remove('/' + rel.lstrip('/'))
            print('[HTTPPull] Removed:', rel)
        except OSError:
            pass

    # 2. Move staged files to final locations
    for stage_path, rel_path in _walk(_STAGE):
        final = rel_path   # already has leading /
        _makedirs('/'.join(final.split('/')[:-1]))
        try:
            os.remove(final)
        except OSError:
            pass
        os.rename(stage_path, final)
        print('[HTTPPull] Installed:', final)

    _remove_tree(_STAGE)

    # 3. Write manifest and version
    with open(_MANIFEST, 'w') as f:
        json.dump(new_manifest, f)
    version = new_manifest.get('version', 'unknown')
    with open(_VERSION, 'w') as f:
        json.dump({'version': version}, f)
    print('[HTTPPull] Committed version:', version)


# ── transport ─────────────────────────────────────────────────────────────────

def _mpy_version():
    """Return device mpy bytecode version int, or None if not exposed."""
    import sys
    return getattr(sys.implementation, 'mpy', None)


class HttpPullTransport:
    """
    Pull-mode OTA transport.  Instead of listening for incoming connections,
    it periodically polls a manifest URL and applies any available update.

    This transport uses the *poll()* interface detected by OTAUpdater:
        while True:
            transport.poll()
            time.sleep(transport.interval)

    mpy negotiation: if the device exposes sys.implementation.mpy, poll()
    first tries  manifest.mpyN.json  (e.g. manifest.mpy6.json).  A 404
    silently falls back to manifest.json.  The mpy manifest lists .mpy target
    paths and adds a "src" field pointing to the .mpyN server file:
        "lib/uota/ota.mpy": {"sha256": "...", "size": 8317, "src": "lib/uota/ota.mpy6"}
    """

    def __init__(self, manifest_url, interval=60, timeout=15):
        self.manifest_url = manifest_url
        self.interval     = interval
        self.timeout      = timeout
        self._base_url    = manifest_url.rsplit('/', 1)[0]

    # Marker for OTAUpdater to treat this as a pull transport
    is_pull = True

    def _mpy_manifest_url(self):
        mpy = _mpy_version()
        if mpy is None:
            return None
        return self.manifest_url.replace('.json', '.mpy{}.json'.format(mpy))

    def poll(self):
        """Check for updates and apply if a newer version is available."""
        # 1. Fetch remote manifest — prefer mpy-specific variant
        remote = None
        mpy_url = self._mpy_manifest_url()
        if mpy_url:
            try:
                remote = json.loads(_http_get(mpy_url, timeout=self.timeout))
                print('[HTTPPull] Using', mpy_url.rsplit('/', 1)[-1])
            except OSError:
                pass  # 404 or network error — fall through to py manifest
        if remote is None:
            try:
                data = _http_get(self.manifest_url, timeout=self.timeout)
                remote = json.loads(data)
            except Exception as e:
                print('[HTTPPull] Manifest fetch failed:', e)
                return

        remote_version = remote.get('version', '')

        # 2. Compare to installed version
        try:
            with open(_VERSION) as f:
                local_version = json.load(f).get('version', '')
        except OSError:
            local_version = ''

        if remote_version == local_version:
            return   # already up to date

        print('[HTTPPull] Update:', local_version or '(none)', '->', remote_version)

        # 3. Determine which files changed
        try:
            with open(_MANIFEST) as f:
                local_files = json.load(f).get('files', {})
        except OSError:
            local_files = {}

        remote_files = remote.get('files', {})

        # 4. Stage changed / new files
        _remove_tree(_STAGE)
        os.mkdir(_STAGE)

        ok = True
        for rel, info in remote_files.items():
            if local_files.get(rel, {}).get('sha256') == info['sha256']:
                # Unchanged — copy from installed location to stage
                try:
                    src = '/' + rel.lstrip('/')
                    dst = _STAGE + '/' + rel.lstrip('/')
                    _makedirs('/'.join(dst.split('/')[:-1]))
                    with open(src, 'rb') as r, open(dst, 'wb') as w:
                        while True:
                            chunk = r.read(512)
                            if not chunk:
                                break
                            w.write(chunk)
                except Exception as e:
                    print('[HTTPPull] Stage copy failed:', rel, e)
                    ok = False
                    break
            else:
                # Changed or new — download.
                # "src" points to the server-side file (e.g. ota.mpy6);
                # rel is where the device saves it (e.g. lib/uota/ota.mpy).
                src_path = info.get('src', rel)
                url = self._base_url + '/' + src_path.lstrip('/')
                dst = _STAGE + '/' + rel.lstrip('/')
                _makedirs('/'.join(dst.split('/')[:-1]))
                try:
                    h = hashlib.sha256()
                    with open(dst, 'wb') as f:
                        class _Writer:
                            def write(self_, data):
                                h.update(data)
                                f.write(data)
                        _http_get(url, stream_to=_Writer(), timeout=self.timeout)
                    actual = ''.join('%02x' % b for b in h.digest())
                    if actual != info.get('sha256', actual):
                        print('[HTTPPull] SHA256 mismatch:', rel)
                        ok = False
                        break
                    print('[HTTPPull] Downloaded:', rel)
                except Exception as e:
                    print('[HTTPPull] Download failed:', rel, e)
                    ok = False
                    break

        if not ok:
            _remove_tree(_STAGE)
            return

        # 5. Atomic commit + reset
        try:
            _commit_pull(remote)
        except Exception as e:
            print('[HTTPPull] Commit failed:', e)
            _remove_tree(_STAGE)
            return

        import machine
        time.sleep(0.3)
        machine.reset()
