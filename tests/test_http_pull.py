"""
Unit tests for Phase 4: HTTP pull transport + bundle/serve.

Tests the host-side serve and bundle commands, and validates the device-side
HttpPullTransport source (syntax + structure).  No real ESP32 required.
"""

import json
import os
import sys
import tempfile
import threading
import urllib.request
import zipfile
import hashlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

_DEVICE_HTTP_PULL = os.path.join(
    os.path.dirname(__file__), '..', 'device', 'transports', 'http_pull.py'
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _write(path, content):
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else '.', exist_ok=True)
    mode = 'wb' if isinstance(content, bytes) else 'w'
    with open(path, mode) as f:
        f.write(content)


def _make_project(tmp, files, version='1.0.0', extra_cfg=None):
    """Write files and ota.json into tmp; return cfg_path."""
    for name, content in files.items():
        _write(os.path.join(tmp, name), content)
    cfg = {
        'version': version,
        'fullOtaFiles': list(files.keys()),
        'excludedFiles': [],
    }
    if extra_cfg:
        cfg.update(extra_cfg)
    cfg_path = os.path.join(tmp, 'ota.json')
    with open(cfg_path, 'w') as f:
        json.dump(cfg, f)
    return cfg_path


# ── bundle tests ──────────────────────────────────────────────────────────────

def test_bundle_creates_manifest_and_files():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _make_project(tmp, {'main.py': b'x=1\n', 'lib/helper.py': b'pass\n'})
        from host.bundle import build
        manifest = build(out_dir='dist', cfg_path=cfg)
        assert os.path.isfile(os.path.join(tmp, 'dist', 'manifest.json'))
        assert os.path.isfile(os.path.join(tmp, 'dist', 'main.py'))
        assert os.path.isfile(os.path.join(tmp, 'dist', 'lib', 'helper.py'))
        assert manifest['version'] == '1.0.0'
        assert 'main.py' in manifest['files']
        assert 'lib/helper.py' in manifest['files']


def test_bundle_version_override():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _make_project(tmp, {'main.py': b'x=1\n'}, version='1.0.0')
        from host.bundle import build
        manifest = build(out_dir='dist', version='9.9.9', cfg_path=cfg)
        assert manifest['version'] == '9.9.9'
        with open(os.path.join(tmp, 'dist', 'manifest.json')) as f:
            on_disk = json.load(f)
        assert on_disk['version'] == '9.9.9'


def test_bundle_creates_zip():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _make_project(tmp, {'main.py': b'x=1\n'})
        from host.bundle import build
        build(out_dir='dist', make_zip=True, cfg_path=cfg)
        zip_path = os.path.join(tmp, 'dist.zip')
        assert os.path.isfile(zip_path)
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
        assert 'manifest.json' in names
        assert 'main.py' in names


def test_bundle_sha256_in_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        content = b'print("hello")\n'
        cfg = _make_project(tmp, {'main.py': content})
        from host.bundle import build
        manifest = build(out_dir='dist', cfg_path=cfg)
        expected = hashlib.sha256(content).hexdigest()
        assert manifest['files']['main.py']['sha256'] == expected


def test_bundle_excludes_respected():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _make_project(tmp, {'main.py': b'x=1\n', 'secret.py': b'pass\n'},
                            extra_cfg={'excludedFiles': ['secret.py']})
        from host.bundle import build
        manifest = build(out_dir='dist', cfg_path=cfg)
        assert 'main.py' in manifest['files']
        assert 'secret.py' not in manifest['files']


# ── serve tests ───────────────────────────────────────────────────────────────

def _start_server(tmp, port, files=None, version='1.0.0'):
    """Start serve() in a daemon thread; return (thread, ready_event)."""
    if files is None:
        files = {'main.py': b'x=1\n'}
    cfg = _make_project(tmp, files, version=version)
    from host.serve import serve
    ready = threading.Event()
    t = threading.Thread(
        target=serve,
        kwargs=dict(host='127.0.0.1', port=port, version=version,
                    cfg_path=cfg, on_ready=lambda h, p, m: ready.set()),
        daemon=True,
    )
    t.start()
    ready.wait(timeout=5)
    return t


def test_serve_manifest_endpoint():
    with tempfile.TemporaryDirectory() as tmp:
        _start_server(tmp, 18181)
        data = urllib.request.urlopen(
            'http://127.0.0.1:18181/manifest.json', timeout=3).read()
        m = json.loads(data)
        assert m['version'] == '1.0.0'
        assert 'main.py' in m['files']


def test_serve_file_endpoint():
    with tempfile.TemporaryDirectory() as tmp:
        content = b'hello = True\n'
        _start_server(tmp, 18182, files={'main.py': content})
        got = urllib.request.urlopen(
            'http://127.0.0.1:18182/main.py', timeout=3).read()
        assert got == content


def test_serve_404_for_unknown():
    with tempfile.TemporaryDirectory() as tmp:
        _start_server(tmp, 18183)
        try:
            urllib.request.urlopen(
                'http://127.0.0.1:18183/nonexistent.py', timeout=3)
            assert False, 'expected 404'
        except urllib.error.HTTPError as e:
            assert e.code == 404


def test_serve_manifest_sha256_matches_file():
    with tempfile.TemporaryDirectory() as tmp:
        content = b'value = 42\n'
        _start_server(tmp, 18184, files={'main.py': content})
        m = json.loads(urllib.request.urlopen(
            'http://127.0.0.1:18184/manifest.json', timeout=3).read())
        expected = hashlib.sha256(content).hexdigest()
        assert m['files']['main.py']['sha256'] == expected


def test_serve_nested_file():
    with tempfile.TemporaryDirectory() as tmp:
        content = b'UTIL = 1\n'
        _start_server(tmp, 18185, files={'lib/util.py': content})
        got = urllib.request.urlopen(
            'http://127.0.0.1:18185/lib/util.py', timeout=3).read()
        assert got == content


# ── device http_pull source validation ───────────────────────────────────────

def test_http_pull_valid_python():
    with open(_DEVICE_HTTP_PULL) as f:
        src = f.read()
    compile(src, _DEVICE_HTTP_PULL, 'exec')


def test_http_pull_has_is_pull_marker():
    with open(_DEVICE_HTTP_PULL) as f:
        src = f.read()
    assert 'is_pull = True' in src


def test_http_pull_has_poll_method():
    with open(_DEVICE_HTTP_PULL) as f:
        src = f.read()
    assert 'def poll(self)' in src


def test_http_pull_has_commit():
    with open(_DEVICE_HTTP_PULL) as f:
        src = f.read()
    assert '_commit_pull' in src


def test_http_pull_sha256_verification():
    with open(_DEVICE_HTTP_PULL) as f:
        src = f.read()
    assert 'sha256' in src
    assert 'mismatch' in src.lower()


# ── runner ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    tests = [v for k, v in list(globals().items()) if k.startswith('test_')]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print('  PASS ', t.__name__)
            passed += 1
        except Exception as e:
            import traceback
            print('  FAIL ', t.__name__, ':', e)
            traceback.print_exc()
            failed += 1
    print('\n%d passed, %d failed' % (passed, failed))
    sys.exit(failed)
