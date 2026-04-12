"""HTTP file server for micro-ota HTTP pull transport."""

import http.server
import json
import os
import socketserver
import threading


class OTAHandler(http.server.BaseHTTPRequestHandler):
    project_root = None
    file_map     = {}
    manifest     = {}

    def log_message(self, fmt, *args):
        print('[serve]', fmt % args)

    def do_GET(self):
        path = self.path.lstrip('/')

        if path == 'manifest.json':
            body = json.dumps(self.manifest, separators=(',', ':')).encode()
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
        project_root = os.path.dirname(cfg_path)

    with open(cfg_path) as f:
        cfg = json.load(f)

    if version is None:
        version = cfg.get('version', '0.0.0')

    full_patterns    = cfg.get('fullOtaFiles', ['**/*.py'])
    exclude_patterns = cfg.get('excludedFiles', [])

    os.chdir(project_root)
    manifest = build_manifest(full_patterns, exclude_patterns, version)

    file_map = {rel: os.path.join(project_root, rel) for rel in manifest['files']}

    OTAHandler.project_root = project_root
    OTAHandler.file_map     = file_map
    OTAHandler.manifest     = manifest

    httpd = _ThreadingServer((host, port), OTAHandler)

    if on_ready:
        on_ready(host, port, manifest)

    print('[serve] Serving %d files (version %s) on http://%s:%d/'
          % (len(manifest['files']), version, host, port))
    print('[serve] Device manifestUrl: http://<this-ip>:%d/manifest.json' % port)
    print('[serve] Press Ctrl-C to stop.')

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n[serve] Stopped.')
    finally:
        httpd.server_close()


def _find_cfg():
    """Walk up from CWD looking for ota.json."""
    d = os.getcwd()
    for _ in range(5):
        candidate = os.path.join(d, 'ota.json')
        if os.path.exists(candidate):
            return candidate
        d = os.path.dirname(d)
    raise FileNotFoundError('ota.json not found')
