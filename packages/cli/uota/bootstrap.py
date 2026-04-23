"""
First-time bootstrap: uploads the OTA library to a blank ESP32 via serial.

Device files are taken from:
  1. <project>/lib/uota/  (created by `uota init`)
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
    ('__init__.py',                  '/lib/uota/__init__.py'),
    ('ota.py',                       '/lib/uota/ota.py'),
    ('boot_guard.py',                '/lib/uota/boot_guard.py'),
    ('remoteio.py',                  '/lib/uota/remoteio.py'),
    ('transports/__init__.py',       '/lib/uota/transports/__init__.py'),
    ('transports/wifi_tcp.py',       '/lib/uota/transports/wifi_tcp.py'),
    ('transports/http_pull.py',      '/lib/uota/transports/http_pull.py'),
    ('transports/serial.py',         '/lib/uota/transports/serial.py'),
    ('transports/ble.py',            '/lib/uota/transports/ble.py'),
]

_BOOT_PY = (_TEMPLATES / 'boot.py').read_text()


def _compile_mpy(src_path, tmp_dir, mpy_version=None):
    """
    Compile src_path to .mpy using mpy-cross.
    mpy_version: int passed as  -b <ver>  to target the device's bytecode version.
    Returns Path to the compiled .mpy, or None on failure.
    """
    mpy_cross = shutil.which('mpy-cross')
    if not mpy_cross:
        return None
    out = Path(tmp_dir) / (Path(src_path).stem + '.mpy')
    cmd = [mpy_cross]
    if mpy_version is not None:
        cmd += ['-b', str(mpy_version)]
    cmd += ['-o', str(out), str(src_path)]
    result = subprocess.run(cmd, capture_output=True)
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
            # Query mpy bytecode version BEFORE suppressing stdout so print()
            # still reaches the raw REPL framing.  Used with mpy-cross -b <ver>
            # to compile bytecode that exactly matches the device firmware.
            mpy_version = None
            if mpy:
                try:
                    raw = repl.exec(
                        "import sys\n"
                        "_v=getattr(sys.implementation,'mpy',None)\n"
                        "print(_v if _v is not None else '')\n"
                    )
                    val = raw.decode().strip()
                    mpy_version = int(val) if val else None
                    if mpy_version is not None:
                        print('[bootstrap] Device mpy version: {} (mpy-cross -b {})'.format(
                            mpy_version, mpy_version))
                    else:
                        print('[bootstrap] mpy version not exposed — mpy-cross will use default')
                except Exception:
                    print('[bootstrap] Could not read mpy version — mpy-cross will use default')

            # Suppress sys.stdout on the device so that any background OTA
            # thread's print() calls produce no output on UART0.  Without this,
            # the thread's output can land between raw REPL framing bytes
            # (e.g. between the two \x04 delimiters) and corrupt the protocol.
            repl.exec(
                "import sys as _s\n"
                "class _N:\n"
                "    def write(self,*a):pass\n"
                "    def flush(self,*a):pass\n"
                "try:_s.stdout=_N()\n"
                "except:pass\n"
                "del _s,_N\n"
            )

            # Clear the boot_guard crash counter.  If previous bootstrap
            # attempts were interrupted before the OTA thread could call
            # mark_clean(), the counter can reach _MAX_CRASHES and trigger
            # a reset on the next boot.  Resetting it here prevents that.
            repl.exec(
                "try:\n"
                " import json\n"
                " _f=open('/ota_boot_state.json','w')\n"
                " json.dump({'crashes':0,'clean':True},_f)\n"
                " _f.close()\n"
                "except:pass\n"
            )
            print('[bootstrap] Boot guard state cleared.')

            # Upload OTA infrastructure files
            for rel, remote in _DEVICE_RELPATHS:
                local = device_dir / rel
                if not local.exists():
                    print('  [skip] {} not found'.format(rel))
                    continue

                if mpy:
                    compiled = _compile_mpy(local, tmp_dir, mpy_version)
                    if compiled:
                        remote_mpy = remote.rsplit('.', 1)[0] + '.mpy'
                        print('  {:<45} → {} (.mpy)'.format(rel, remote_mpy))
                        repl.put_file(str(compiled), remote_mpy, on_progress=_progress(rel))
                        # Remove .py counterpart so MicroPython doesn't find both
                        repl.exec("import os\ntry:\n os.remove({!r})\nexcept:pass\n".format(remote))
                        continue
                    else:
                        print('  [mpy-cross failed] {} — uploading .py'.format(rel))

                print('  {:<45} → {}'.format(rel, remote))
                repl.put_file(str(local), remote, on_progress=_progress(rel))

            # Upload config/ota.json to the device.
            ota_json = Path.cwd() / 'config' / 'ota.json'
            if ota_json.exists():
                repl.exec('import os\ntry:\n os.mkdir("/config")\nexcept:pass\n')
                print('  {:<45} → /config/ota.json'.format('config/ota.json'))
                repl.put_file(str(ota_json), '/config/ota.json', on_progress=_progress('config/ota.json'))
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
