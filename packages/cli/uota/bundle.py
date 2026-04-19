"""OTA bundle builder for micro-ota."""

import json
import os
import shutil
import zipfile


def build(out_dir='dist', make_zip=False, version=None,
          project_root=None, cfg_path=None):
    from .manifest import build as build_manifest

    if cfg_path is None:
        cfg_path = _find_cfg()
    cfg_path = os.path.abspath(cfg_path)
    if project_root is None:
        project_root = os.path.dirname(os.path.dirname(cfg_path))  # config/ → project root

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
        print('[bundle] %s' % rel)

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


def _find_cfg():
    d = os.getcwd()
    for _ in range(5):
        candidate = os.path.join(d, 'config', 'ota.json')
        if os.path.exists(candidate):
            return candidate
        d = os.path.dirname(d)
    raise FileNotFoundError('config/ota.json not found')
