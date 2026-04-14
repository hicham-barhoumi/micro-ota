#!/usr/bin/env python3
"""
Cross-platform build script for micro-ota.
Works on Linux, macOS, and Windows.

Usage:
    python scripts/build.py           # pip wheel + sdist + VS Code .vsix
    python scripts/build.py --pip     # pip artifacts only
    python scripts/build.py --vscode  # VS Code extension only

Produces in dist/:
    micro_ota-<version>-py3-none-any.whl
    micro_ota-<version>.tar.gz
    micro-ota-<version>.vsix
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT    = Path(__file__).resolve().parent.parent
DIST    = ROOT / 'dist'
CLI_DIR = ROOT / 'packages' / 'cli'
VS_DIR  = ROOT / 'packages' / 'vscode'


def _supports_ansi():
    """Enable and detect ANSI support (Windows 10+ virtual terminal)."""
    if sys.platform != 'win32':
        return True
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.SetConsoleMode(kernel.GetStdHandle(-11), 7)
        return True
    except Exception:
        return False


_ANSI  = _supports_ansi()
GREEN  = '\033[32m' if _ANSI else ''
BLUE   = '\033[34m' if _ANSI else ''
RED    = '\033[31m' if _ANSI else ''
RESET  = '\033[0m'  if _ANSI else ''


def ok(msg):   print(f'  {GREEN}✔{RESET}  {msg}')
def info(msg): print(f'  {BLUE}→{RESET}  {msg}')
def err(msg):  print(f'  {RED}✘{RESET}  {msg}', file=sys.stderr)


def _run(*cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check,
                          capture_output=False, text=True)


def build_pip():
    print('\n[ pip package ]')

    # Ensure build frontend
    try:
        import build  # noqa: F401
    except ImportError:
        info('installing build frontend…')
        _run(sys.executable, '-m', 'pip', 'install', 'build', '-q',
             '--break-system-packages')

    # Stage README (pyproject.toml references README.md in the same dir)
    readme_src = ROOT / 'README.md'
    readme_dst = CLI_DIR / 'README.md'
    shutil.copy(readme_src, readme_dst)

    # Clean stale artifacts
    for pattern in ('build', '*.egg-info'):
        for p in CLI_DIR.glob(pattern):
            shutil.rmtree(p, ignore_errors=True)

    try:
        _run(sys.executable, '-m', 'build', str(CLI_DIR),
             '--outdir', str(DIST), '--wheel', '--sdist')
    finally:
        readme_dst.unlink(missing_ok=True)

    ok('pip package built')


def build_vscode():
    print('\n[ VS Code extension ]')

    npm = shutil.which('npm')
    if not npm:
        err('npm not found — skipping VS Code extension build')
        err('Install Node.js from https://nodejs.org then rerun this script')
        return

    if not (VS_DIR / 'node_modules').exists():
        info('installing npm dependencies…')
        _run(npm, 'install', '--silent', cwd=VS_DIR)

    info('compiling TypeScript…')
    _run(npm, 'run', 'compile', cwd=VS_DIR)

    info('packaging extension…')
    npx = shutil.which('npx') or npm.replace('npm', 'npx')
    _run(npx, 'vsce', 'package', '--out', str(DIST), cwd=VS_DIR)

    ok('VS Code extension built')


def main():
    parser = argparse.ArgumentParser(description='micro-ota build script')
    parser.add_argument('--pip',    action='store_true', help='build pip package only')
    parser.add_argument('--vscode', action='store_true', help='build VS Code extension only')
    args = parser.parse_args()

    both = not args.pip and not args.vscode

    DIST.mkdir(exist_ok=True)
    print(f'\nmicro-ota build  →  {DIST}')
    print('─' * 40)

    if both or args.pip:
        build_pip()

    if both or args.vscode:
        build_vscode()

    print('\n' + '─' * 40)
    artifacts = list(DIST.iterdir())
    if artifacts:
        print('Artifacts:')
        for f in sorted(artifacts):
            size = f.stat().st_size // 1024
            print(f'  {f.name:<45} {size} KB')
    print()


if __name__ == '__main__':
    main()
