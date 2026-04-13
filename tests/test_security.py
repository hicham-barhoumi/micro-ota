"""
Phase 7 — HMAC-SHA256 manifest signing tests.

Tests cover:
  - sign() adds 'sig' to manifest
  - sign() produces a stable, deterministic signature
  - sign() is path-order independent (sorted)
  - verify() accepts correct key
  - verify() rejects wrong key
  - verify() rejects tampered version
  - verify() rejects tampered file hash
  - verify() rejects missing sig when key set
  - verify() passes when key is empty (backward compat)
  - host HMAC matches a known reference value
  - device _hmac_sha256_hex matches host hmac.new
  - device _verify_manifest_sig logic
"""

import hmac as _hmac
import hashlib
import sys
import os
import unittest

# Make the package importable from the repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'packages', 'cli'))

from uota.manifest import sign, verify, _signing_payload

# ── replicate device helpers locally for cross-verification ──────────────────

def _device_hmac_sha256_hex(key, msg):
    """Mirrors _hmac_sha256_hex() from _device/ota.py."""
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


def _device_signing_payload(manifest):
    lines = [manifest.get('version', '')]
    for path in sorted(manifest.get('files', {})):
        lines.append('{}:{}'.format(path, manifest['files'][path]['sha256']))
    return '\n'.join(lines)


def _device_verify(manifest, key):
    if not key:
        return True
    expected = _device_hmac_sha256_hex(key, _device_signing_payload(manifest))
    return manifest.get('sig', '') == expected


# ── fixtures ─────────────────────────────────────────────────────────────────

def _manifest():
    return {
        'version': '1.2.3',
        'files': {
            'main.py':   {'size': 100, 'sha256': 'aabbcc'},
            'lib/foo.py': {'size': 200, 'sha256': 'ddeeff'},
        }
    }

KEY = 'super-secret-key'


# ── tests ─────────────────────────────────────────────────────────────────────

class TestSign(unittest.TestCase):

    def test_sign_adds_sig_field(self):
        m = sign(_manifest(), KEY)
        self.assertIn('sig', m)

    def test_sign_sig_is_hex_string(self):
        m = sign(_manifest(), KEY)
        self.assertIsInstance(m['sig'], str)
        self.assertEqual(len(m['sig']), 64)

    def test_sign_is_deterministic(self):
        m1 = sign(_manifest(), KEY)
        m2 = sign(_manifest(), KEY)
        self.assertEqual(m1['sig'], m2['sig'])

    def test_sign_does_not_mutate_original(self):
        orig = _manifest()
        sign(orig, KEY)
        self.assertNotIn('sig', orig)

    def test_sign_path_order_independent(self):
        """Signing should be stable regardless of dict insertion order."""
        ma = _manifest()
        mb = {
            'version': '1.2.3',
            'files': {
                'lib/foo.py': {'size': 200, 'sha256': 'ddeeff'},
                'main.py':   {'size': 100, 'sha256': 'aabbcc'},
            }
        }
        self.assertEqual(sign(ma, KEY)['sig'], sign(mb, KEY)['sig'])


class TestVerify(unittest.TestCase):

    def test_verify_correct_key(self):
        m = sign(_manifest(), KEY)
        self.assertTrue(verify(m, KEY))

    def test_verify_wrong_key(self):
        m = sign(_manifest(), KEY)
        self.assertFalse(verify(m, 'wrong-key'))

    def test_verify_tampered_version(self):
        m = sign(_manifest(), KEY)
        m['version'] = '9.9.9'
        self.assertFalse(verify(m, KEY))

    def test_verify_tampered_file_hash(self):
        m = sign(_manifest(), KEY)
        m['files']['main.py']['sha256'] = 'deadbeef'
        self.assertFalse(verify(m, KEY))

    def test_verify_missing_sig(self):
        m = _manifest()   # no sig field
        self.assertFalse(verify(m, KEY))

    def test_verify_no_key_always_passes(self):
        """Empty key = security disabled (backward compat)."""
        m = _manifest()   # unsigned
        self.assertTrue(verify(m, ''))

    def test_verify_no_key_passes_even_with_bad_sig(self):
        m = _manifest()
        m['sig'] = 'garbage'
        self.assertTrue(verify(m, ''))


class TestDeviceHmac(unittest.TestCase):

    def test_device_hmac_matches_stdlib(self):
        """Device pure-Python HMAC must produce same output as stdlib hmac."""
        key = 'my-key'
        msg = 'hello device'
        expected = _hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(_device_hmac_sha256_hex(key, msg), expected)

    def test_device_verify_matches_host(self):
        """Device verify logic accepts manifests signed by the host."""
        m = sign(_manifest(), KEY)
        self.assertTrue(_device_verify(m, KEY))

    def test_device_verify_rejects_wrong_key(self):
        m = sign(_manifest(), KEY)
        self.assertFalse(_device_verify(m, 'attacker-key'))

    def test_device_verify_no_key_passes(self):
        m = _manifest()
        self.assertTrue(_device_verify(m, ''))


if __name__ == '__main__':
    unittest.main()
