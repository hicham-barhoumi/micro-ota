"""
Tests for host/manifest.py
"""

import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host.manifest import build, to_json, _sha256


def _write(dir_, rel, content=b'hello'):
    path = os.path.join(dir_, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(content)
    return path


def test_build_single_file():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _write(d, 'main.py', b'print("hello")')
        m = build(['*.py'])
        assert 'main.py' in m['files']
        assert m['files']['main.py']['size'] == len(b'print("hello")')


def test_build_nested():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _write(d, 'main.py', b'x=1')
        _write(d, 'lib/foo.py', b'y=2')
        _write(d, 'lib/sub/bar.py', b'z=3')
        m = build(['*.py', 'lib/**'])
        assert 'main.py' in m['files']
        assert 'lib/foo.py' in m['files']
        assert 'lib/sub/bar.py' in m['files']


def test_build_excludes():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _write(d, 'main.py', b'x=1')
        _write(d, 'boot.py', b'y=2')
        m = build(['*.py'], exclude_patterns=['boot.py'])
        assert 'main.py' in m['files']
        assert 'boot.py' not in m['files']


def test_build_version():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        _write(d, 'main.py', b'x=1')
        m = build(['*.py'], version='2.3.4')
        assert m['version'] == '2.3.4'


def test_build_empty_dir():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        m = build(['*.py'])
        assert m['files'] == {}


def test_sha256_correctness():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'f.bin')
        data = b'micro-ota test data'
        with open(path, 'wb') as f:
            f.write(data)
        expected = hashlib.sha256(data).hexdigest()
        assert _sha256(path) == expected


def test_sha256_in_manifest():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        data = b'some content'
        _write(d, 'app.py', data)
        m = build(['*.py'])
        assert m['files']['app.py']['sha256'] == hashlib.sha256(data).hexdigest()


def test_to_json_is_compact():
    m = {'version': '1.0', 'files': {'a.py': {'size': 1, 'sha256': 'abc'}}}
    j = to_json(m)
    # No spaces around separators
    assert ' ' not in j
    assert json.loads(j) == m


def test_directories_not_included():
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        os.makedirs(os.path.join(d, 'lib'))
        _write(d, 'lib/foo.py', b'x=1')
        m = build(['lib/**'])
        for key in m['files']:
            assert not os.path.isdir(key)


if __name__ == '__main__':
    tests = [v for k, v in list(globals().items()) if k.startswith('test_')]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f'  PASS  {t.__name__}')
            passed += 1
        except Exception as e:
            print(f'  FAIL  {t.__name__}: {e}')
            failed += 1
    print(f'\n{passed} passed, {failed} failed')
    sys.exit(failed)
