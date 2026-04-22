"""HTTP file server for micro-ota HTTP pull transport."""

import fnmatch
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading


class OTAHandler(http.server.BaseHTTPRequestHandler):
    project_root  = None
    file_map      = {}     # url_path → local abs path  (covers both .py and .mpyN files)
    manifests     = {}     # url_path → manifest dict   ('manifest.json', 'manifest.mpy6.json', …)

    def log_message(self, fmt, *args):
        print('[serve]', fmt % args)

    def do_GET(self):
        path = self.path.lstrip('/')

        if path in self.manifests:
            body = json.dumps(self.manifests[path], separators=(',', ':')).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        local = self.file_map.get(path)
        if local is None or not os.path.isfile(local):
            self.send_error(404, 'Not found: ' + path)
            return

        size = os.path.getsize(local)
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(size))
        self.end_headers()
        with open(local, 'rb') as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host='0.0.0.0', port=8080, project_root=None, version=None,
          cfg_path=None, on_ready=None):
    from .manifest import build as build_manifest

    if cfg_path is None:
        cfg_path = _find_cfg()
    cfg_path = os.path.abspath(cfg_path)
    if project_root is None:
        project_root = os.path.dirname(os.path.dirname(cfg_path))

    with open(cfg_path) as f:
        cfg = json.load(f)

    if version is None:
        version = cfg.get('version', '0.0.0')

    full_patterns    = cfg.get('fastOtaFiles', []) + cfg.get('fullOtaFiles', [])
    exclude_patterns = cfg.get('excludedFiles', [])

    os.chdir(project_root)
    manifest = build_manifest(full_patterns, exclude_patterns, version)

    file_map  = {rel: os.path.join(project_root, rel) for rel in manifest['files']}
    manifests = {'manifest.json': manifest}

    # Compile mpy variant if configured
    mpy_patterns = cfg.get('mpyFiles', [])
    tmp_dir = None
    if mpy_patterns and shutil.which('mpy-cross'):
        mpy_ver = _read_mpy_cache()
        if mpy_ver is not None:
            tmp_dir = tempfile.mkdtemp()
            mpy_manifest, mpy_file_map = _compile_mpy_variant(
                manifest, project_root, mpy_patterns, mpy_ver, version, tmp_dir)
            manifests['manifest.mpy{}.json'.format(mpy_ver)] = mpy_manifest
            file_map.update(mpy_file_map)
            print('[serve] mpy v%d variant ready (%d compiled files)'
                  % (mpy_ver, sum(1 for v in mpy_manifest['files'].values() if 'src' in v)))
        else:
            print('[serve] mpyFiles configured but no cached mpy version —'
                  ' run  uota info  first to serve mpy variant')

    OTAHandler.project_root = project_root
    OTAHandler.file_map     = file_map
    OTAHandler.manifests    = manifests

    httpd = _ThreadingServer((host, port), OTAHandler)

    if on_ready:
        on_ready(host, port, manifest)

    print('[serve] Serving %d files (version %s) on http://%s:%d/'
          % (len(manifest['files']), version, host, port))
    for name in manifests:
        print('[serve]   manifest: http://<ip>:%d/%s' % (port, name))
    print('[serve] Press Ctrl-C to stop.')

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n[serve] Stopped.')
    finally:
        httpd.server_close()
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _compile_mpy_variant(base_manifest, project_root, mpy_patterns,
                          mpy_ver, version, tmp_dir):
    """
    Compile .py files matching mpy_patterns to .mpy in tmp_dir.
    Returns (mpy_manifest_dict, extra_file_map).

    extra_file_map: {'lib/uota/ota.mpy6': '/tmp/.../ota.mpy', ...}
    mpy_manifest: files dict with .mpy device paths + "src" pointing to .mpyN URL paths.
    """
    mpy_cross = shutil.which('mpy-cross')
    mpy_files    = {}
    extra_routes = {}

    for rel, info in base_manifest['files'].items():
        if rel.endswith('.py') and any(fnmatch.fnmatch(rel, p) for p in mpy_patterns):
            src = os.path.join(project_root, rel)
            server_name = rel[:-3] + '.mpy{}'.format(mpy_ver)
            device_path = rel[:-3] + '.mpy'
            out_file    = os.path.join(tmp_dir, server_name.replace('/', '__'))

            r = subprocess.run(
                [mpy_cross, '-b', str(mpy_ver), '-o', out_file, src],
                capture_output=True,
            )
            if r.returncode == 0 and os.path.exists(out_file):
                mpy_files[device_path] = {
                    'size':   os.path.getsize(out_file),
                    'sha256': _sha256(out_file),
                    'src':    server_name,
                }
                extra_routes[server_name] = out_file
            else:
                err = r.stderr.decode(errors='replace').strip()
                print('[serve] compile failed: %s (%s) — falling back to .py' % (rel, err or '?'))
                mpy_files[rel] = info
        else:
            mpy_files[rel] = info

    mpy_manifest = {'version': version, 'files': mpy_files}
    return mpy_manifest, extra_routes


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            h.update(chunk)
    return h.hexdigest()


def _read_mpy_cache():
    try:
        with open('.uota_cache.json') as f:
            return json.load(f).get('mpy_version')
    except Exception:
        return None


def _find_cfg():
    """Walk up from CWD looking for config/ota.json."""
    d = os.getcwd()
    for _ in range(5):
        candidate = os.path.join(d, 'config', 'ota.json')
        if os.path.exists(candidate):
            return candidate
        d = os.path.dirname(d)
    raise FileNotFoundError('config/ota.json not found')
