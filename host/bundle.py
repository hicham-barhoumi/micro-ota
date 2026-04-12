"""
OTA bundle builder for micro-ota.

Creates a self-contained release directory (and optionally a ZIP) that can
be uploaded to any HTTP server, cloud storage, or shared manually.

The bundle layout:
    dist/
    ├── manifest.json          ← auto-generated SHA-256 manifest
    ├── main.py                ← managed files at their relative paths
    └── lib/
        └── utils.py

A device with the http_pull transport will fetch manifest.json, then
download only the files whose SHA-256 changed.

Usage:
    python3 host/bundle.py [--out dist] [--zip] [--version 1.2.3]
    python3 host/uota.py bundle [--out dist] [--zip] [--version 1.2.3]
"""

import json
import os
import shutil
import zipfile


def build(out_dir='dist', make_zip=False, version=None,
          project_root=None, cfg_path=None):
    """
    Build an OTA bundle into *out_dir*.

    out_dir      — output directory (created / overwritten)
    make_zip     — also produce <out_dir>.zip
    version      — version string (read from ota.json if None)
    project_root — project directory (auto-detected if None)
    cfg_path     — path to ota.json (auto-detected if None)

    Returns the manifest dict.
    """
    from host.manifest import build as build_manifest

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

    # Build manifest from project root
    prev_dir = os.getcwd()
    os.chdir(project_root)
    try:
        manifest = build_manifest(full_patterns, exclude_patterns, version)
    finally:
        os.chdir(prev_dir)

    # Create output directory (relative to project root)
    out_abs = os.path.join(project_root, out_dir)
    if os.path.exists(out_abs):
        shutil.rmtree(out_abs)
    os.makedirs(out_abs)

    # Write manifest
    manifest_path = os.path.join(out_abs, 'manifest.json')
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    print('[bundle] manifest.json (%d files, version %s)'
          % (len(manifest['files']), version))

    # Copy managed files
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
                    arc  = os.path.relpath(full, out_abs)
                    zf.write(full, arc)
        print('[bundle] %s' % os.path.relpath(zip_path, project_root))

    print('[bundle] Done → %s/' % os.path.relpath(out_abs, project_root))
    return manifest


# ── helpers ───────────────────────────────────────────────────────────────────

def _find_cfg():
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        candidate = os.path.join(d, 'ota.json')
        if os.path.exists(candidate):
            return candidate
        d = os.path.dirname(d)
    raise FileNotFoundError('ota.json not found')


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description='micro-ota bundle builder')
    p.add_argument('--out',     default='dist', help='Output directory')
    p.add_argument('--zip',     action='store_true', help='Also create a ZIP')
    p.add_argument('--version', default=None, help='Version string override')
    args = p.parse_args()
    build(out_dir=args.out, make_zip=args.zip, version=args.version)


if __name__ == '__main__':
    main()
