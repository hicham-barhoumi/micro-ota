"""
First-time bootstrap: uploads the OTA library to a blank ESP32 via serial.

Device files are taken from:
  1. <project>/lib/uota/  (if present — created by `uota init`)
  2. The bundled _device/ folder inside this package (fallback)

Files uploaded:
  lib/uota/{ota.py,boot_guard.py,remoteio.py,transports/…},
  config/ota.json (from project), boot.py (from _templates/)
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_BUNDLED   = Path(__file__).parent / '_device'
_TEMPLATES = Path(__file__).parent / '_templates'

_DEVICE_RELPATHS = [
    ('__init__.py',               '/lib/uota/__init__.py'),
    ('ota.py',                    '/lib/uota/ota.py'),
    ('boot_guard.py',             '/lib/uota/boot_guard.py'),
    ('remoteio.py',               '/lib/uota/remoteio.py'),
    ('transports/__init__.py',    '/lib/uota/transports/__init__.py'),
    ('transports/wifi_tcp.py',    '/lib/uota/transports/wifi_tcp.py'),
]

_BOOT_PY = (_TEMPLATES / 'boot.py').read_text()


def _compile_mpy(src_path, tmp_dir):
    """
    Compile src_path to .mpy using mpy-cross.
    Returns Path to the compiled .mpy, or None if mpy-cross is unavailable
    or compilation fails.
    """
    mpy_cross = shutil.which('mpy-cross')
    if not mpy_cross:
        return None
    out = Path(tmp_dir) / (Path(src_path).stem + '.mpy')
    result = subprocess.run(
        [mpy_cross, '-o', str(out), str(src_path)],
        capture_output=True,
    )
    if result.returncode == 0 and out.exists():
        return out
    return None


def run(port, baud=115200, device_dir=None, mpy=False):
    """
    Bootstrap the device on *port*.

    device_dir — directory containing OTA device files.
                 Defaults to <cwd>/device if it exists, otherwise the
                 bundled _device/ folder from the package.
    mpy        — compile infrastructure files to .mpy with mpy-cross before
                 uploading (faster import, less RAM). Requires mpy-cross on PATH
                 and version-matched to device firmware.
    """
    from .transports.serial import RawREPL, auto_detect_port

    # Resolve device source directory
    if device_dir is None:
        cwd_device = Path.cwd() / 'lib' / 'uota'
        device_dir = cwd_device if cwd_device.is_dir() else _BUNDLED

    device_dir = Path(device_dir)
    print('[bootstrap] Using device files from:', device_dir)

    if port is None:
        port = auto_detect_port()
        if port is None:
            print('ERROR: No serial port found. Connect the device and retry,')
            print('       or specify --port explicitly.')
            sys.exit(1)
        print('Auto-detected port:', port)

    print('Bootstrapping {} @ {} baud …'.format(port, baud))

    if mpy:
        mpy_cross = shutil.which('mpy-cross')
        if mpy_cross:
            print('[bootstrap] mpy-cross found at', mpy_cross)
        else:
            print('[bootstrap] WARNING: mpy-cross not found on PATH — uploading .py files instead')
            mpy = False

    tmp_dir = tempfile.mkdtemp() if mpy else None
    try:
        with RawREPL(port, baud) as repl:
            # Upload OTA infrastructure files
            for rel, remote in _DEVICE_RELPATHS:
                local = device_dir / rel
                if not local.exists():
                    print('  [skip] {} not found'.format(rel))
                    continue

                if mpy:
                    compiled = _compile_mpy(local, tmp_dir)
                    if compiled:
                        remote_mpy = remote.rsplit('.', 1)[0] + '.mpy'
                        print('  {:<45} → {} (.mpy)'.format(rel, remote_mpy))
                        repl.put_file(str(compiled), remote_mpy, on_progress=_progress(rel))
                        continue
                    else:
                        print('  [mpy-cross failed] {} — uploading .py'.format(rel))

                print('  {:<45} → {}'.format(rel, remote))
                repl.put_file(str(local), remote, on_progress=_progress(rel))

            # Upload config/ota.json from the project (canonical location).
            # Fall back to root ota.json for backward compatibility.
            ota_json = Path.cwd() / 'config' / 'ota.json'
            if not ota_json.exists():
                ota_json = Path.cwd() / 'ota.json'
            if ota_json.exists():
                repl.exec_('import os\ntry:\n os.mkdir("/config")\nexcept:pass\n')
                label  = str(ota_json.relative_to(Path.cwd()))
                remote = '/config/ota.json'
                print('  {:<45} → {}'.format(label, remote))
                repl.put_file(str(ota_json), remote, on_progress=_progress(label))
            else:
                print('  [skip] config/ota.json not found in current directory')

            # Write generated boot.py
            print('  {:<45} → /boot.py'.format('(generated boot.py)'))
            repl.write_text('/boot.py', _BOOT_PY)

            print('\nBootstrap complete. Resetting device …')
            repl.soft_reset()
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    print('Done. Device is now OTA-capable.')
    print('Next step: run  uota fast  to upload your application.')


def _progress(name):
    last = [-1]
    def cb(sent, total):
        pct = int(sent / total * 100) if total else 100
        if pct != last[0]:
            last[0] = pct
            bar = '#' * (pct // 5) + '-' * (20 - pct // 5)
            print('\r    [{}] {:3d}%'.format(bar, pct), end='', flush=True)
        if sent == total:
            print()
    return cb
