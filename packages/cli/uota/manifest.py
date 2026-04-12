"""
Build an OTA manifest: walks files matching glob patterns,
computes SHA256 for each, returns a dict ready to send to the device.
"""

import os
import glob
import hashlib
import json


def build(patterns, exclude_patterns=None, version='unknown'):
    """
    Args:
        patterns:         list of glob patterns to include  (e.g. ['*.py', 'lib/**'])
        exclude_patterns: list of glob patterns to exclude  (e.g. ['.git/**'])
        version:          version string to embed in manifest

    Returns dict:
        {
            'version': '1.0.0',
            'files': {
                'main.py':      {'size': 1234, 'sha256': 'abc...'},
                'lib/foo.py':   {'size':  456, 'sha256': 'def...'},
            }
        }
    """
    excluded = set()
    for pat in (exclude_patterns or []):
        excluded.update(glob.glob(pat, recursive=True))

    files = {}
    for pat in patterns:
        for path in glob.glob(pat, recursive=True):
            norm = path.replace('\\', '/')
            if os.path.isfile(path) and norm not in excluded:
                files[norm] = {
                    'size':   os.path.getsize(path),
                    'sha256': _sha256(path),
                }

    return {'version': version, 'files': files}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            h.update(chunk)
    return h.hexdigest()


def to_json(manifest):
    return json.dumps(manifest, separators=(',', ':'))


def from_zip(zip_path):
    """Extract manifest.json from a .zip bundle and return the dict."""
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open('manifest.json') as mf:
            return json.load(mf)
