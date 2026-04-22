"""OTA bundle builder for micro-ota."""

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import zipfile


def build(out_dir='dist', make_zip=False, version=None,
          project_root=None, cfg_path=None):
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

    prev_dir = os.getcwd()
    os.chdir(project_root)
    try:
        manifest = build_manifest(full_patterns, exclude_patterns, version)
    finally:
        os.chdir(prev_dir)

    out_abs = os.path.join(project_root, out_dir)
    if os.path.exists(out_abs):
        shutil.rmtree(out_abs)
    os.makedirs(out_abs)

    # Write manifest.json and copy source files
    manifest_path = os.path.join(out_abs, 'manifest.json')
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    print('[bundle] manifest.json (%d files, version %s)'
          % (len(manifest['files']), version))

    for rel in manifest['files']:
        src = os.path.join(project_root, rel)
        dst = os.path.join(out_abs, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        print('[bundle]   %s' % rel)

    # Compile mpy variants if configured
    mpy_patterns = cfg.get('mpyFiles', [])
    if mpy_patterns and shutil.which('mpy-cross'):
        mpy_ver = _read_mpy_cache()
        if mpy_ver is not None:
            _build_mpy_variant(out_abs, manifest, project_root,
                               mpy_patterns, mpy_ver, version)
        else:
            print('[bundle] mpyFiles configured but no cached mpy version —'
                  ' run  uota info  first to enable mpy bundle')

    if make_zip:
        zip_path = out_abs + '.zip'
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(out_abs):
                for fname in files:
                    full = os.path.join(root, fname)
                    arc  = os.path.relpath(full, out_abs).replace('\\', '/')
                    zf.write(full, arc)
        print('[bundle] %s' % os.path.relpath(zip_path, project_root))

    print('[bundle] Done → %s/' % os.path.relpath(out_abs, project_root))
    return manifest


def _build_mpy_variant(out_abs, base_manifest, project_root,
                        mpy_patterns, mpy_ver, version):
    """
    Compile .py files matching mpy_patterns to .mpy{N} and write
    manifest.mpy{N}.json alongside the base bundle.

    Server layout added:
      lib/uota/ota.mpy6          ← compiled bytes, served at this URL
      manifest.mpy6.json         ← lists target paths as .mpy with "src" field

    The "src" field tells the device which URL to fetch; the manifest key
    is where the device saves the file.  Example entry:
      "lib/uota/ota.mpy": {"size": 8317, "sha256": "...", "src": "lib/uota/ota.mpy6"}
    """
    mpy_cross = shutil.which('mpy-cross')
    mpy_files = {}
    compiled = 0

    for rel, info in base_manifest['files'].items():
        if rel.endswith('.py') and any(fnmatch.fnmatch(rel, p) for p in mpy_patterns):
            src = os.path.join(project_root, rel)
            server_name = rel[:-3] + '.mpy{}'.format(mpy_ver)  # e.g. lib/uota/ota.mpy6
            device_path = rel[:-3] + '.mpy'                     # e.g. lib/uota/ota.mpy
            out_file = os.path.join(out_abs, server_name)
            os.makedirs(os.path.dirname(out_file), exist_ok=True)

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
                compiled += 1
                print('[bundle]   %s → %s (.mpy%d)' % (rel, device_path, mpy_ver))
            else:
                err = r.stderr.decode(errors='replace').strip()
                print('[bundle]   compile failed: %s (%s) — omitted from mpy manifest'
                      % (rel, err or '?'))
        else:
            mpy_files[rel] = info  # non-compiled file: same entry, no "src"

    mpy_manifest = {'version': version, 'files': mpy_files}
    mpy_path = os.path.join(out_abs, 'manifest.mpy{}.json'.format(mpy_ver))
    with open(mpy_path, 'w') as f:
        json.dump(mpy_manifest, f, indent=2)
    print('[bundle] manifest.mpy%d.json (%d/%d files compiled)'
          % (mpy_ver, compiled, len(base_manifest['files'])))


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
    d = os.getcwd()
    for _ in range(5):
        candidate = os.path.join(d, 'config', 'ota.json')
        if os.path.exists(candidate):
            return candidate
        d = os.path.dirname(d)
    raise FileNotFoundError('config/ota.json not found')
