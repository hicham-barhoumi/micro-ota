#!/usr/bin/env python3
"""
uota - micro-ota host CLI

Commands:
  info        [--port PORT] [--baud BAUD]
  init       [--dir DIR] [--force]
  bootstrap  [--port PORT] [--baud BAUD] [--mpy]
  fast        [--host HOST] [--port PORT] [--transport T] [--ble-name N] [--password P] [--version VER]
  full        [--host HOST] [--port PORT] [--transport T] [--ble-name N] [--password P] [--version VER] [--wipe]
  wipe        [--host HOST] [--port PORT] [--transport T] [--ble-name N] [--password P]
  terminal    [--host HOST] [--port PORT] [--transport T] [--ble-name N] [--password P]
  version     [--host HOST] [--port PORT] [--transport T] [--ble-name N] [--password P]
  flash <firmware.bin> [--port PORT] [--baud BAUD] [--chip CHIP] [--erase]
  serve       [--host HOST] [--port PORT] [--version VER]
  bundle      [--out DIR] [--zip] [--version VER]
  remoteio    listen | call <name> [key=val ...] [--transport wifi_tcp|ble] [--ble-name N]
  list        [--transport wifi_tcp|ble] [--port PORT] [--timeout SEC]

All connection params default to values in ota.json when not specified.
"""

import argparse
import errno
import hashlib
import hmac as _hmac_mod
import json
import os
import struct
import sys
import socket
import time
from pathlib import Path

from .manifest import build as build_manifest, to_json as manifest_to_json
from .transports.wifi_tcp import WiFiTCPTransport
from .transports.serial import SerialOTATransport


# -- file templates (loaded from _templates/ at import time) ------------------

_TEMPLATES = Path(__file__).parent / '_templates'
_BOOT_PY  = (_TEMPLATES / 'boot.py').read_text()
_MAIN_PY  = (_TEMPLATES / 'main.py').read_text()
_APP_PY   = (_TEMPLATES / 'app.py').read_text()
_OTA_JSON = (_TEMPLATES / 'ota.json').read_text()


# -- error handling ------------------------------------------------------------

def _friendly(exc, cfg=None):
    msg = str(exc)
    eno = getattr(exc, 'errno', None)

    try:
        import serial
        if isinstance(exc, serial.SerialException):
            if 'No such file' in msg or 'cannot find' in msg.lower():
                port = msg.split("'")[1] if "'" in msg else msg
                return ("Serial port not found: {}\n"
                        "  Check the USB cable or set serialPort in ota.json.").format(port)
            if 'Permission denied' in msg or 'Access is denied' in msg:
                port = msg.split("'")[1] if "'" in msg else '(port)'
                import sys as _sys
                if _sys.platform == 'win32':
                    return ("Permission denied on {}.\n"
                            "  Check that no other program (e.g. Thonny, PuTTY) has the port open.\n"
                            "  You may also need to install the CP210x / CH340 driver.").format(port)
                return ("Permission denied on {}.\n"
                        "  Run: sudo usermod -aG dialout $USER  then log out and back in.\n"
                        "  Or prefix your command with: sg dialout -c \"...\"").format(port)
            return 'Serial error: ' + msg
    except ImportError:
        pass

    if isinstance(exc, (TimeoutError, socket.timeout)):
        # BLE timeouts mention 'BLE' or 'not found' — format as BLE errors, not WiFi
        if any(kw in msg for kw in ('BLE', 'not found', 'scan timed out')):
            return msg or 'BLE operation timed out. Is the device powered on and advertising?'
        host = cfg.get('hostname', '?') if cfg else '?'
        port = cfg.get('port', 2018) if cfg else '?'
        return ("Timed out connecting to {}:{}.\n"
                "  Is the device on WiFi and is the OTA server running?").format(host, port)

    if isinstance(exc, ConnectionRefusedError) or eno == errno.ECONNREFUSED:
        host = cfg.get('hostname', '?') if cfg else '?'
        port = cfg.get('port', 2018) if cfg else '?'
        return ("Connection refused at {}:{}.\n"
                "  The OTA server may not have started yet -- wait a few seconds and retry.\n"
                "  Or run: uota terminal  to check device state.").format(host, port)

    if eno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN):
        return ("Network unreachable. Check that the host and device are on the same network\n"
                "  and that hostname/IP in ota.json is correct.")

    if isinstance(exc, OSError):
        if eno == errno.EIO:
            return 'I/O error on serial port. Device disconnected during transfer?'
        if eno == errno.ENOENT:
            return 'Device or file not found: ' + msg
        if 'connection closed' in msg.lower():
            return ('Device closed the connection unexpectedly.\n'
                    '  It may have reset mid-transfer -- check the serial console.')

    if isinstance(exc, RuntimeError):
        if 'raw REPL' in msg:
            return ("Could not enter MicroPython raw REPL.\n"
                    "  Press the Reset button on the device and retry.\n"
                    "  Detail: " + msg)
        if 'Inline OTA' in msg or 'failed to start' in msg.lower():
            return ("Serial OTA server injection failed.\n"
                    "  Reset the device and retry: uota fast --transport serial\n"
                    "  Detail: " + msg)
        if 'not ready' in msg or 'ready' in msg:
            return 'Device rejected OTA session: ' + msg
        if 'No OTA transport' in msg or 'No supported transport' in msg:
            return ("No transport configured.\n"
                    "  Set 'transports' in ota.json (e.g. [\"wifi_tcp\"] or [\"serial\"]).")
        if 'bleak is required' in msg:
            return msg   # already has pip install hint
        return 'Error: ' + msg

    if isinstance(exc, FileNotFoundError):
        return 'File not found: ' + msg

    if 'BLE' in msg or 'bluetooth' in msg.lower():
        return 'BLE error: ' + msg

    return None


# -- config --------------------------------------------------------------------

def load_config(path='config/ota.json'):
    cfg = {}
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        pass
    secrets_dir = os.path.dirname(path) or '.'
    secrets_path = os.path.join(secrets_dir, '.ota.secrets.json')
    try:
        with open(secrets_path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


# -- transport factory ---------------------------------------------------------

def get_transport(cfg, host_override=None, port_override=None, transport_override=None,
                  ble_name_override=None):
    names = [transport_override] if transport_override else cfg.get('transports', ['ble'])

    for name in names:
        if name == 'wifi_tcp':
            host = host_override or cfg.get('hostname', 'micropython')
            port = port_override or cfg.get('port', 2018)
            return WiFiTCPTransport(host, port)
        if name == 'serial':
            port = _normalise_serial_port(
                host_override or cfg.get('serialPort') or _auto_serial()
            )
            baud = cfg.get('serialBaud', 115200)
            return SerialOTATransport(port, baud)
        if name == 'ble':
            from .transports.ble import BLETransport
            ble_name = ble_name_override or host_override or cfg.get('bleName', 'micro-ota')
            return BLETransport(ble_name)

    raise RuntimeError('No supported transport in config')


def _do_auth(transport, password):
    """Send OTA password and check response — no-op when password is empty."""
    if not password:
        return   # no password configured — skip handshake entirely
    transport.write_line(password)
    resp = transport.read_line().strip()
    if resp == 'denied':
        print('ERROR: OTA authentication failed — wrong password.', file=sys.stderr)
        sys.exit(1)


def _auto_serial():
    from .transports.serial import auto_detect_port
    p = auto_detect_port()
    if p is None:
        print('ERROR: No serial port found. Connect the device or set serialPort in ota.json.')
        sys.exit(1)
    return p


def _normalise_serial_port(port):
    """Prepend /dev/ on Linux/macOS if the user typed a bare name like ttyUSB0."""
    if port and sys.platform != 'win32' and not port.startswith('/'):
        return '/dev/' + port
    return port


# -- OTA push ------------------------------------------------------------------

def send_ota(transport, files, manifest, wipe=False, password=''):
    start = time.time()
    with transport:
        _do_auth(transport, password)
        if wipe:
            # transport is already connected via __enter__; send wipe, then
            # close and reopen so the inline server is re-injected for the OTA
            # session (serial) or a fresh TCP connection is made (WiFi).
            transport.write_line('wipe')
            resp = transport.read_line()
            if resp.strip() != 'ok':
                print('Wipe failed:', resp)
                sys.exit(1)
            print('Device wiped.')
            transport.close()
            time.sleep(1)
            transport.connect()

        transport.write_line('start_ota')
        resp = transport.read_line()
        if resp.strip() != 'ready':
            print('Device not ready:', resp)
            sys.exit(1)

        m_json = manifest_to_json(manifest).encode()
        transport.write_line('manifest {}'.format(len(m_json)))
        transport.write(m_json)
        resp = transport.read_line()
        if resp.strip() != 'ok':
            print('Manifest rejected:', resp)
            transport.write_line('abort')
            sys.exit(1)

        total_files = len(files)
        for i, (rel_path, abs_path) in enumerate(files.items(), 1):
            info   = manifest['files'][rel_path]
            size   = info['size']
            sha256 = info['sha256']

            print('  [{}/{}] {}'.format(i, total_files, rel_path), end='  ', flush=True)
            transport.write_line('file {};{};{}'.format(rel_path, size, sha256))

            sent = 0
            t0   = time.time()
            with open(abs_path, 'rb') as f:
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    transport.write(chunk)
                    sent += len(chunk)

            resp    = transport.read_line().strip()
            elapsed = time.time() - t0
            speed   = (size / 1024) / elapsed if elapsed > 0 else 0
            if resp == 'ok':
                print('{:.1f} KB/s'.format(speed))
            else:
                print('\nFailed:', resp)
                transport.write_line('abort')
                sys.exit(1)

        transport.write_line('end_ota')
        resp = transport.read_line()
        if resp.strip() == 'ok':
            elapsed = time.time() - start
            print('\nOTA done in {:.1f}s  ({} files)'.format(elapsed, total_files))


# -- binary stream OTA ---------------------------------------------------------

def _build_ota_stream(manifest, files, key=''):
    """
    Pack all files into a single binary OTA stream.

    Wire format
    -----------
    [4B]  magic  b'OTAS'
    [2B]  version_len  (big-endian)
    [N]   version      (UTF-8)
    [2B]  file_count   (big-endian)
    -- repeated file_count times, in sorted path order --
    [2B]  path_len     (big-endian)
    [N]   path         (UTF-8, relative, no leading /)
    [4B]  file_size    (big-endian)
    [32B] sha256       (binary)
    [M]   file data
    -----------------------------------------------------
    [32B] HMAC-SHA256 trailer  (covers everything from version_len
          through end of last file; zeros if no key)
    """
    paths   = sorted(manifest['files'].keys())
    version = manifest.get('version', 'unknown').encode('utf-8')

    # body = everything the HMAC covers (version_len onward)
    body = bytearray()
    body += struct.pack('>H', len(version))
    body += version
    body += struct.pack('>H', len(paths))
    for path in paths:
        info   = manifest['files'][path]
        path_b = path.encode('utf-8')
        sha_b  = bytes.fromhex(info['sha256'])
        body  += struct.pack('>H', len(path_b))
        body  += path_b
        body  += struct.pack('>I', info['size'])
        body  += sha_b
        with open(files[path], 'rb') as f:
            body += f.read()

    trailer = (_hmac_mod.new(key.encode(), bytes(body), hashlib.sha256).digest()
               if key else b'\x00' * 32)

    return b'OTAS' + bytes(body) + trailer


def send_stream_ota(transport, files, manifest, key='', wipe=False, password=''):
    """
    Push files using the streaming binary protocol (stream_ota command).

    Sends all files as one continuous payload -- no per-file round-trips.
    Each file is written to staging on the device as its bytes arrive.
    Staging is committed atomically on success; discarded on any error.
    """
    stream = _build_ota_stream(manifest, files, key)
    total  = len(stream)
    nfiles = len(files)
    start  = time.time()
    print('Stream OTA: {} file{}  {:.1f} KB'.format(
        nfiles, 's' if nfiles != 1 else '', total / 1024))

    with transport:
        _do_auth(transport, password)
        if wipe:
            transport.write_line('wipe')
            resp = transport.read_line()
            if resp.strip() != 'ok':
                print('Wipe failed:', resp)
                sys.exit(1)
            print('Device wiped.')
            transport.close()
            time.sleep(1)
            transport.connect()

        transport.write_line('stream_ota {}'.format(total))
        resp = transport.read_line()
        if resp.strip() == 'unknown':
            # Device's ota.py predates stream_ota -- fall back to old protocol.
            print('(device does not support stream_ota, falling back to legacy OTA)')
            transport.close()
            send_ota(transport, files, manifest, wipe=False, password=password)
            return
        if resp.strip() != 'ready':
            print('Device not ready for stream_ota:', resp)
            sys.exit(1)

        # Flow-controlled send: 64-byte windows for serial (ACK-based),
        # large chunks for TCP transports that handle their own buffering.
        _ser   = getattr(transport, '_ser', None)
        WINDOW = 64 if _ser else 4096
        sent   = 0
        t0     = time.time()
        while sent < total:
            end   = min(sent + WINDOW, total)
            transport.write(stream[sent:end])
            sent  = end
            elapsed = max(time.time() - t0, 0.001)
            print('\r  {:>3}%  {:.1f}/{:.1f} KB  {:.1f} KB/s'.format(
                sent * 100 // total,
                sent / 1024, total / 1024,
                (sent / 1024) / elapsed,
            ), end='', flush=True)
            if _ser and sent < total:
                # Wait for device ACK (\x06) before releasing the next window.
                # Device print() calls share UART0 and may arrive here first
                # (e.g. "[OTA] Staged: ..."). Drain until the ACK byte arrives.
                t_ack = time.time() + 5
                while True:
                    b = _ser.read(1)
                    if b == b'\x06':
                        break
                    if not b or time.time() > t_ack:
                        raise OSError('flow ctrl: timed out waiting for ACK')
        print()

        # Device may need extra time to commit many files; bump timeout.
        if _ser:
            _ser.timeout = max(getattr(_ser, 'timeout', 15), 60)

        resp    = transport.read_line().strip()
        elapsed = time.time() - start
        if resp in ('ok', ''):
            # '' = connection closed by device immediately after 'ok' + reset;
            # TCP can deliver the FIN before the data in rare packet-loss cases.
            print('Done in {:.1f}s'.format(elapsed))
        elif resp == 'sig_mismatch':
            print('ERROR: HMAC signature mismatch -- update rejected')
            sys.exit(1)
        elif resp.startswith('sha256_mismatch'):
            print('ERROR: file corruption --', resp)
            sys.exit(1)
        else:
            # Drain additional lines for full traceback visibility
            lines = [resp]
            try:
                import serial as _serial
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    transport._ser.timeout = 0.3
                    extra = transport.read_line().strip()
                    if extra:
                        lines.append(extra)
            except Exception:
                pass
            print('ERROR (device traceback):')
            for ln in lines:
                print(' ', ln)
            sys.exit(1)


# -- mpy compilation helpers --------------------------------------------------

_MPY_CACHE = '.uota_cache.json'


def _read_mpy_cache():
    try:
        with open(_MPY_CACHE) as f:
            return json.load(f).get('mpy_version')
    except Exception:
        return None


def _write_mpy_cache(mpy_version):
    try:
        data = {}
        try:
            with open(_MPY_CACHE) as f:
                data = json.load(f)
        except Exception:
            pass
        data['mpy_version'] = mpy_version
        with open(_MPY_CACHE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _query_device_info(port, baud=115200):
    """Open a raw REPL session, query device info, return dict."""
    from .transports.serial import RawREPL
    with RawREPL(port, baud) as repl:
        raw = repl.exec(
            "import sys,gc\n"
            "try:\n"
            " import os;_u=list(os.uname())\n"
            "except:_u=['','','','','']\n"
            "try:\n"
            " import esp32;_fl=esp32.flash_size()\n"
            "except:_fl=0\n"
            "_mpy=getattr(sys.implementation,'mpy',None)\n"
            "import json\n"
            "print(json.dumps({"
            "'v':sys.version,"
            "'mpy':_mpy,"
            "'plat':sys.platform,"
            "'free':gc.mem_free(),"
            "'used':gc.mem_alloc(),"
            "'flash':_fl,"
            "'sysname':_u[0],"
            "'release':_u[2]"
            "}))\n"
        )
    return json.loads(raw.decode().strip())


def _query_mpy_version(port, baud=115200):
    """
    Query the mpy bytecode version from device.
    Returns int if sys.implementation.mpy is available, else None.
    """
    from .transports.serial import RawREPL
    with RawREPL(port, baud) as repl:
        raw = repl.exec(
            "import sys\n"
            "_v=getattr(sys.implementation,'_mpy',None)\n"
            # _mpy is a packed int: lower 8 bits = mpy format version,
            # upper bits = arch/feature flags. Extract just the version.
            "print((_v & 0xff) if _v is not None else '')\n"
        )
    val = raw.decode().strip()
    return int(val) if val else None


def _mpy_cross_version():
    """
    Parse the mpy format version from  mpy-cross --version  output.
    Returns int (major) on success, None if mpy-cross is absent or unparseable.
    """
    import subprocess, re, shutil
    mpy_cross = shutil.which('mpy-cross')
    if not mpy_cross:
        return None
    r = subprocess.run([mpy_cross, '--version'], capture_output=True)
    out = (r.stdout + r.stderr).decode(errors='replace')
    m = re.search(r'mpy v(\d+)', out)
    return int(m.group(1)) if m else None


def _resolve_mpy_version(cfg, args):
    """
    Return the device's mpy bytecode version for compilation.

    Always reads the cache first.  A live device query (which opens a fresh
    RawREPL / soft-reset) is only done when the cache is empty AND the
    transport is serial — this avoids opening two serial connections in a row
    (one for the query, one for the OTA push), which can confuse the device.

    Run  uota info  to populate the cache explicitly; it also works for WiFi
    deployments where a live query is not possible.
    """
    cached = _read_mpy_cache()
    if cached is not None:
        return cached

    transport_names = (
        [getattr(args, 'transport', None)] if getattr(args, 'transport', None)
        else cfg.get('transports', ['wifi_tcp'])
    )
    if 'serial' in transport_names:
        port = _normalise_serial_port(
            getattr(args, 'host', None) or cfg.get('serialPort') or _auto_serial()
        )
        baud = cfg.get('serialBaud', 115200)
        print('[mpy] No cached version — querying device …')
        ver = _query_mpy_version(port, baud)
        if ver is None:
            ver = _mpy_cross_version()
            if ver is not None:
                print('[mpy] Device mpy version not exposed; using mpy-cross default (v{})'.format(ver))
            else:
                print('[mpy] Cannot determine mpy version — skipping compilation')
                return None
        _write_mpy_cache(ver)
        return ver

    print('[mpy] No cached mpy version — run  uota info --port <port>  first')
    return None


def _compile_files_mpy(files, mpy_version, mpy_patterns, tmp_dir):
    """
    Compile .py files matching mpy_patterns to .mpy with mpy-cross -b <ver>.
    Returns (new_files_dict, compiled_count).
    new_files_dict maps remote_rel_path → local_abs_path.
    """
    import shutil, subprocess, fnmatch
    mpy_cross = shutil.which('mpy-cross')
    if not mpy_cross:
        return files, 0

    new_files = {}
    count = 0
    for rel, abs_p in files.items():
        if rel.endswith('.py') and any(fnmatch.fnmatch(rel, pat) for pat in mpy_patterns):
            mpy_rel = rel[:-3] + '.mpy'
            mpy_out = Path(tmp_dir) / mpy_rel.replace('/', '__')
            r = subprocess.run(
                [mpy_cross, '-b', str(mpy_version), '-o', str(mpy_out), abs_p],
                capture_output=True,
            )
            if r.returncode == 0 and mpy_out.exists():
                new_files[mpy_rel] = str(mpy_out)
                count += 1
                continue
            err = r.stderr.decode(errors='replace').strip()
            print('[mpy] compile failed: {} ({}) — uploading .py'.format(rel, err or '?'))
        new_files[rel] = abs_p
    return new_files, count


def _manifest_from_dict(files_dict, version):
    """Build a manifest dict from {rel_path: abs_path} mapping."""
    from .manifest import _sha256
    mfiles = {}
    for rel, abs_p in files_dict.items():
        mfiles[rel] = {'size': os.path.getsize(abs_p), 'sha256': _sha256(abs_p)}
    return {'version': version, 'files': mfiles}


# -- commands ------------------------------------------------------------------

def _strip_docstrings(node):
    import ast as _ast
    if isinstance(node, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
        if (node.body and
                isinstance(node.body[0], _ast.Expr) and
                isinstance(node.body[0].value, _ast.Constant) and
                isinstance(node.body[0].value.value, str)):
            node.body.pop(0)
            if not node.body:
                node.body.append(_ast.Pass())
    for child in _ast.iter_child_nodes(node):
        _strip_docstrings(child)


def _minify_py(source):
    """Strip comments and docstrings from Python source for device deployment."""
    try:
        import ast as _ast
        tree = _ast.parse(source)
        _strip_docstrings(tree)
        _ast.fix_missing_locations(tree)
        return _ast.unparse(tree) + '\n'
    except Exception:
        return source


def _copy_device_files(src, dst):
    """Recursively copy device files, minifying .py files."""
    import shutil
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name == '__pycache__' or item.suffix in ('.pyc', '.pyo'):
            continue
        dest = dst / item.name
        if item.is_dir():
            _copy_device_files(item, dest)
        elif item.suffix == '.py':
            dest.write_text(_minify_py(item.read_text('utf-8')), 'utf-8')
        else:
            shutil.copy2(item, dest)


def cmd_init(args, cfg):
    """Initialize a new micro-ota project in the target directory."""
    import shutil

    target = Path(getattr(args, 'dir', None) or '.').resolve()
    target.mkdir(parents=True, exist_ok=True)
    force  = getattr(args, 'force', False)

    # 1. Copy device OTA infrastructure files (minified)
    device_src = Path(__file__).parent / '_device'
    device_dst = target / 'lib' / 'uota'
    if device_dst.exists() and not force:
        print('[init] lib/uota/ already exists -- skipping (use --force to overwrite)')
    else:
        if device_dst.exists():
            shutil.rmtree(device_dst)
        _copy_device_files(device_src, device_dst)
        print('[init] Copied OTA device files -> lib/uota/ (minified)')

    # 2. Create config/ with ota.json inside
    config_dir = target / 'config'
    config_dir.mkdir(exist_ok=True)
    ota_json = config_dir / 'ota.json'
    if not ota_json.exists():
        ota_json.write_text(_OTA_JSON)
        print('[init] Created config/ota.json')
    else:
        print('[init] config/ota.json already exists -- skipping')

    # 3. Create app/ directory with stub app.py
    app_dir = target / 'app'
    app_dir.mkdir(exist_ok=True)
    app_py = app_dir / 'app.py'
    if not app_py.exists():
        app_py.write_text(_APP_PY)
        print('[init] Created app/app.py')

    # 4. Create stub main.py
    main_py = target / 'main.py'
    if not main_py.exists():
        main_py.write_text(_MAIN_PY)
        print('[init] Created main.py')

    # 5. Create boot.py
    boot_py = target / 'boot.py'
    if not boot_py.exists() or force:
        boot_py.write_text(_BOOT_PY)
        print('[init] {}boot.py'.format('Overwrote ' if boot_py.exists() and force else 'Created '))

    # 6. Create .ota.secrets.json (gitignored) for sensitive keys
    secrets_file = target / 'config' / '.ota.secrets.json'
    if not secrets_file.exists():
        secrets_file.write_text('{\n    "otaPassword": "",\n    "signingKey":  ""\n}\n')
        print('[init] Created config/.ota.secrets.json (keep this out of git)')

    # 7. Add .ota.secrets.json to .gitignore
    gitignore = target / '.gitignore'
    entry = 'config/.ota.secrets.json\n'
    existing = gitignore.read_text() if gitignore.exists() else ''
    if 'ota.secrets' not in existing:
        with open(gitignore, 'a') as f:
            f.write(('\n' if existing and not existing.endswith('\n') else '') + entry)
        print('[init] Added config/.ota.secrets.json to .gitignore')

    print('\nProject layout:')
    print('  app/                       ← application code       (fast + full OTA)')
    print('  config/ota.json            ← OTA + app config        (committed, synced via OTA)')
    print('  config/.ota.secrets.json   ← passwords + signing key (gitignored, local only)')
    print('  lib/uota/                  ← OTA system files        (managed by bootstrap)')
    print('\nNext steps:')
    print('  1. Edit config/ota.json -- fill in ssid, hostname, transports')
    print('  2. Edit config/.ota.secrets.json -- set otaPassword and signingKey')
    print('  3. Connect your ESP32 via USB')
    print('  4. Run: uota bootstrap   (first-time device setup)')
    print('  5. Run: uota fast        (push app/ + config/ updates)')


def cmd_passwd(args, cfg):
    """Print the SHA256 hash of a password for use as otaPassword on the device."""
    import hashlib
    pw = args.password
    print(hashlib.sha256(pw.encode()).hexdigest())


def cmd_info(args, cfg):
    """Query device info via serial raw REPL."""
    port = _normalise_serial_port(
        args.port or cfg.get('serialPort') or _auto_serial()
    )
    baud = args.baud or cfg.get('serialBaud', 115200)
    print('Connecting to {} @ {} …'.format(port, baud))
    info = _query_device_info(port, baud)

    mpy_ver = info.get('mpy')
    flash_b = info.get('flash', 0)
    free    = info.get('free', 0)
    used    = info.get('used', 0)

    print()
    print('Device info  ({})'.format(port))
    print('─' * 52)
    print('MicroPython  {}'.format(info.get('v', '?')))
    print('Platform     {}'.format(info.get('plat', '?')))
    if info.get('sysname'):
        print('Kernel       {}  {}'.format(info.get('sysname', ''), info.get('release', '')))
    print('Heap         free {:,} B  /  used {:,} B'.format(free, used))
    if flash_b:
        print('Flash        {} MB'.format(flash_b // (1024 * 1024)))
    if mpy_ver is not None:
        print('mpy version  {}  →  mpy-cross -b {}'.format(mpy_ver, mpy_ver))
        _write_mpy_cache(mpy_ver)
        print('\nmpy version cached to {}'.format(_MPY_CACHE))
    else:
        fallback = _mpy_cross_version()
        if fallback is not None:
            print('mpy version  (not in firmware; mpy-cross emits v{} — will use as default)'.format(fallback))
            _write_mpy_cache(fallback)
            print('\nmpy version cached to {} (from mpy-cross)'.format(_MPY_CACHE))
        else:
            print('mpy version  (not determinable — mpy-cross not found or version not parseable)')


def cmd_bootstrap(args, cfg):
    from .bootstrap import run as do_bootstrap
    do_bootstrap(
        port=_normalise_serial_port(args.port or cfg.get('serialPort')),
        baud=args.baud,
        mpy=args.mpy,
    )


def _ota_prepare(cfg, args, patterns, excludes):
    """Build manifest and compile mpy files once. Returns (files, manifest, tmp_dir, key)."""
    import shutil, tempfile, hashlib
    version  = getattr(args, 'version', None) or cfg.get('version', 'unknown')
    manifest = build_manifest(patterns, excludes, version)
    key      = cfg.get('signingKey', '')
    files    = {p: p for p in manifest['files']}

    tmp_dir = None
    mpy_patterns = cfg.get('mpyFiles', [])
    if mpy_patterns and shutil.which('mpy-cross'):
        mpy_version = _resolve_mpy_version(cfg, args)
        if mpy_version is not None:
            tmp_dir = tempfile.mkdtemp()
            files, n = _compile_files_mpy(files, mpy_version, mpy_patterns, tmp_dir)
            if n:
                manifest = _manifest_from_dict(files, version)
                print('[mpy] {}/{} files compiled to .mpy (v{})'.format(
                    n, len(manifest['files']), mpy_version))
            else:
                import shutil as _sh
                _sh.rmtree(tmp_dir, ignore_errors=True)
                tmp_dir = None

    # Transform config/ota.json before pushing to device:
    #   - inject signingKey (device needs it for HMAC verification)
    #   - replace plaintext otaPassword with SHA256 hash (device verifies by hashing)
    cfg_rel = 'config/ota.json'
    if cfg_rel in files:
        plaintext_pw = cfg.get('otaPassword', '')
        if key or plaintext_pw:
            if tmp_dir is None:
                tmp_dir = tempfile.mkdtemp()
            with open(files[cfg_rel]) as f:
                device_cfg = json.load(f)
            device_cfg['signingKey'] = key
            if plaintext_pw:
                device_cfg['otaPassword'] = hashlib.sha256(
                    plaintext_pw.encode()).hexdigest()
            os.makedirs(os.path.join(tmp_dir, 'config'), exist_ok=True)
            tmp_cfg = os.path.join(tmp_dir, 'config', 'ota.json')
            with open(tmp_cfg, 'w') as f:
                json.dump(device_cfg, f, indent=4)
            files[cfg_rel] = tmp_cfg
            manifest = _manifest_from_dict(files, version)

    return files, manifest, tmp_dir, key


def _ota_push(cfg, args, patterns, excludes, wipe=False):
    """Shared implementation for fast and full OTA pushes."""
    import shutil
    files, manifest, tmp_dir, key = _ota_prepare(cfg, args, patterns, excludes)
    password = getattr(args, 'password', None) or cfg.get('otaPassword', '')
    ble_name = getattr(args, 'ble_name', None)
    transport = get_transport(cfg, args.host, args.port, getattr(args, 'transport', None),
                              ble_name_override=ble_name)
    try:
        send_stream_ota(transport, files, manifest, key=key, wipe=wipe, password=password)
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _ota_push_all(cfg, args, patterns, excludes, wipe=False):
    """Scan for all reachable devices and push OTA to each one."""
    import shutil

    transport_filter = getattr(args, 'transport', None)
    password = getattr(args, 'password', None) or cfg.get('otaPassword', '')
    ota_port = cfg.get('port', 2018)
    timeout  = 5.0

    # --- discover targets -------------------------------------------------------
    wifi_targets = []  # list of IP strings
    ble_targets  = []  # list of (name, address) tuples

    if transport_filter in (None, 'wifi_tcp'):
        wifi_targets = _wifi_scan(ota_port, timeout)
    if transport_filter in (None, 'ble'):
        ble_targets = _ble_scan(timeout)

    if not wifi_targets and not ble_targets:
        print('No devices found.')
        return

    print('Targets: {} WiFi, {} BLE'.format(len(wifi_targets), len(ble_targets)))

    # --- prepare files once -----------------------------------------------------
    files, manifest, tmp_dir, key = _ota_prepare(cfg, args, patterns, excludes)

    errors = []

    try:
        for ip in wifi_targets:
            print('\n[WiFi {}]'.format(ip))
            t = WiFiTCPTransport(ip, ota_port)
            try:
                send_stream_ota(t, files, manifest, key=key, wipe=wipe, password=password)
            except Exception as e:
                print('ERROR: {}'.format(e), file=sys.stderr)
                errors.append((ip, e))

        for name, ble_dev in ble_targets:
            print('\n[BLE {}]'.format(name))
            from .transports.ble import BLETransport
            t = BLETransport(name=name, device=ble_dev)
            try:
                send_stream_ota(t, files, manifest, key=key, wipe=wipe, password=password)
            except Exception as e:
                print('ERROR: {}'.format(e), file=sys.stderr)
                errors.append((name, e))
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if errors:
        sys.exit(1)


def cmd_fast(args, cfg):
    patterns = cfg.get('fastOtaFiles', ['main.py'])
    excludes = cfg.get('excludedFiles', [])
    if getattr(args, 'all', False):
        _ota_push_all(cfg, args, patterns, excludes)
    else:
        _ota_push(cfg, args, patterns, excludes)


def cmd_full(args, cfg):
    patterns = cfg.get('fastOtaFiles', []) + cfg.get('fullOtaFiles', [])
    excludes = cfg.get('excludedFiles', [])
    if getattr(args, 'all', False):
        _ota_push_all(cfg, args, patterns, excludes, wipe=args.wipe)
    else:
        _ota_push(cfg, args, patterns, excludes, wipe=args.wipe)


def cmd_terminal(args, cfg):
    password = getattr(args, 'password', None) or cfg.get('otaPassword', '')
    ble_name = getattr(args, 'ble_name', None)
    transport = get_transport(cfg, args.host, args.port, getattr(args, 'transport', None),
                              ble_name_override=ble_name)
    print("Terminal mode. Type 'exit' to quit.")
    print("Commands: ping, version, ls [path], get <path>, rm <path>, reset, wipe")
    try:
        transport.connect()
    except OSError as e:
        print('Connection error:', e)
        return
    try:
        _do_auth(transport, password)
        while True:
            try:
                line = input('$ ').strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line or line.lower() in ('exit', 'quit'):
                break
            try:
                transport.write_line(line)
                if line.startswith('get '):
                    size_line = transport.read_line()
                    try:
                        size = int(size_line.strip())
                        data = transport.read_exact(size)
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                        print()
                    except ValueError:
                        print(size_line)
                elif line.startswith('ls'):
                    # Multi-line response terminated by a blank line.
                    while True:
                        l = transport.read_line()
                        if not l:
                            break
                        print(l)
                else:
                    print(transport.read_line())
            except OSError as e:
                print('Connection error:', e)
                break
    finally:
        transport.close()


def cmd_version(args, cfg):
    password = getattr(args, 'password', None) or cfg.get('otaPassword', '')
    ble_name = getattr(args, 'ble_name', None)
    transport = get_transport(cfg, args.host, args.port, getattr(args, 'transport', None),
                              ble_name_override=ble_name)
    with transport:
        _do_auth(transport, password)
        transport.write_line('version')
        print(transport.read_line())


def cmd_wipe(args, cfg):
    password = getattr(args, 'password', None) or cfg.get('otaPassword', '')
    ble_name = getattr(args, 'ble_name', None)
    transport = get_transport(cfg, args.host, args.port, getattr(args, 'transport', None),
                              ble_name_override=ble_name)
    with transport:
        _do_auth(transport, password)
        transport.write_line('wipe')
        resp = transport.read_line().strip()
        if resp == 'ok':
            print('Device wiped. /lib, /config, and /boot.py preserved.')
        else:
            print('Unexpected response:', resp)


def cmd_flash(args, cfg):
    from .firmware import flash, _find_port
    port = args.port or _find_port(cfg)
    flash(
        firmware_path=args.firmware,
        port=port,
        baud=args.baud,
        chip=args.chip,
        erase=args.erase,
    )


def cmd_serve(args, cfg):
    from .serve import serve
    serve(
        host=args.host,
        port=args.port,
        version=args.version or cfg.get('version'),
    )


def _get_local_subnets():
    """Return [(local_ip_str, IPv4Network)] for every active non-loopback interface."""
    import ipaddress, subprocess
    subnets = []
    try:
        out = subprocess.check_output(
            ['ip', '-4', 'addr', 'show'], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            s = line.strip()
            if s.startswith('inet ') and 'scope' in s:
                cidr = s.split()[1]
                iface = ipaddress.IPv4Interface(cidr)
                if not iface.ip.is_loopback:
                    subnets.append((str(iface.ip), iface.network))
    except Exception:
        pass
    return subnets


def _wifi_scan(ota_port, scan_timeout):
    """Probe every host in local /24 subnets for an open OTA port."""
    import ipaddress
    from concurrent.futures import ThreadPoolExecutor, as_completed

    subnets = _get_local_subnets()
    if not subnets:
        print('WiFi: could not detect local subnets')
        return []

    probe_t = min(0.3, scan_timeout / 3)
    found = []

    for _local_ip, network in subnets:
        # Limit to /24 or smaller to keep scan time reasonable
        if network.prefixlen < 24:
            network = ipaddress.IPv4Network(
                str(network.network_address) + '/24', strict=False)
        hosts = list(network.hosts())
        print('WiFi scan {} ({} hosts, port {}) …'.format(
            network, len(hosts), ota_port))

        def _probe(ip, port=ota_port, t=probe_t):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(t)
                s.connect((str(ip), port))
                s.close()
                return str(ip)
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=64) as ex:
            futs = {ex.submit(_probe, h): h for h in hosts}
            for f in as_completed(futs, timeout=scan_timeout + 1):
                ip = f.result()
                if ip:
                    found.append(ip)

    return sorted(found)


_OTA_SERVICE_UUID = '756f7461-b5a3-f393-e0a9-e50e24dcca9e'


def _ble_scan(scan_timeout):
    """Discover micro-ota devices by OTA service UUID. Returns list of (name, BLEDevice)."""
    import asyncio
    try:
        from bleak import BleakScanner
    except ImportError:
        print('BLE: bleak not installed — run: pip install bleak')
        return []

    print('BLE scan ({:.0f}s) …'.format(scan_timeout))

    async def _run():
        found = []
        async with BleakScanner(service_uuids=[_OTA_SERVICE_UUID]) as scanner:
            await asyncio.sleep(scan_timeout)
            for d in scanner.discovered_devices:
                found.append((d.name or d.address, d))
        return found

    return asyncio.run(_run())


def cmd_list(args, cfg):
    transport = getattr(args, 'transport', None)
    ota_port  = getattr(args, 'port', None) or cfg.get('port', 2018)
    timeout   = getattr(args, 'timeout', 5.0)

    if transport in (None, 'wifi_tcp'):
        ips = _wifi_scan(ota_port, timeout)
        if ips:
            for ip in ips:
                try:
                    hostname = socket.gethostbyaddr(ip)[0]
                except Exception:
                    hostname = ''
                suffix = ('  ' + hostname) if hostname else ''
                print('  WiFi  {}{}'.format(ip, suffix))
        else:
            print('  WiFi  (none found)')

    if transport in (None, 'ble'):
        devices = _ble_scan(timeout)
        if devices:
            for name, ble_dev in sorted(devices, key=lambda x: x[0]):
                print('  BLE   {}  {}'.format(ble_dev.address, name))
        else:
            print('  BLE   (none found)')


def cmd_bundle(args, cfg):
    from .bundle import build
    build(
        out_dir=args.out,
        make_zip=args.zip,
        version=args.version or cfg.get('version'),
    )


def cmd_remoteio(args, cfg):
    transport = args.transport
    if transport is None:
        transports = cfg.get('transports', ['wifi_tcp'])
        transport = 'ble' if transports == ['ble'] else 'wifi_tcp'

    if transport == 'ble':
        from .remoteio import RemoteIOBLEClient, _parse_kwargs
        ble_name = args.ble_name or cfg.get('bleName', 'micro-ota')
        if args.subcmd == 'listen':
            print('Connecting to "%s" via BLE NUS …' % ble_name)
            with RemoteIOBLEClient(ble_name) as rio:
                print('Connected. Streaming device output (Ctrl-C to stop).\n')
                rio.listen()
        else:  # call
            if not args.handler:
                print('call requires a handler name', file=sys.stderr)
                sys.exit(1)
            kwargs = _parse_kwargs(args.kwargs or [])
            with RemoteIOBLEClient(ble_name) as rio:
                result = rio.call(args.handler, **kwargs)
            print(json.dumps(result, indent=2))
    else:
        from .remoteio import main as remoteio_main, _parse_kwargs
        # Build argv for the TCP-only remoteio.main()
        argv = [args.subcmd]
        if args.subcmd == 'call':
            if not args.handler:
                print('call requires a handler name', file=sys.stderr)
                sys.exit(1)
            argv.append(args.handler)
            argv += (args.kwargs or [])
        if args.host:
            argv.append(args.host)
        if args.port:
            argv.append(str(args.port))
        sys.argv = ['uota remoteio'] + argv
        remoteio_main()


# -- CLI entry point -----------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        prog='uota',
        description='micro-ota host tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('-v', '--verbose', action='store_true',
                   help='Show full traceback on errors')
    sub = p.add_subparsers(dest='command', required=True)

    # info
    inf = sub.add_parser('info', help='Query device info via serial (mpy version, heap, flash …)')
    inf.add_argument('--port', help='Serial port (auto-detected if omitted)')
    inf.add_argument('--baud', type=int, default=115200)

    # init
    ini = sub.add_parser('init', help='Initialize a new micro-ota project')
    ini.add_argument('--dir',   default='.', help='Target directory (default: current dir)')
    ini.add_argument('--force', action='store_true', help='Overwrite existing device/ files')

    # bootstrap
    bs = sub.add_parser('bootstrap', help='First-time upload of OTA lib via serial')
    bs.add_argument('--port', help='Serial port (auto-detected if omitted)')
    bs.add_argument('--baud', type=int, default=115200)
    bs.add_argument('--mpy', action='store_true',
                    help='Compile OTA files to .mpy with mpy-cross (faster import, less RAM)')

    # fast
    fa = sub.add_parser('fast', help='Push fastOtaFiles to the device')
    fa.add_argument('--host')
    fa.add_argument('--port',      type=int)
    fa.add_argument('--version')
    fa.add_argument('--all',       action='store_true',
                    help='Scan and push to all reachable devices (WiFi + BLE)')
    fa.add_argument('--transport', choices=['wifi_tcp', 'serial', 'ble'])
    fa.add_argument('--ble-name',  dest='ble_name', metavar='NAME',
                    help='BLE advertisement name (overrides bleName in ota.json)')
    fa.add_argument('--password',  metavar='PW',
                    help='OTA session password (overrides otaPassword in ota.json)')

    # full
    fu = sub.add_parser('full', help='Push all managed files to the device')
    fu.add_argument('--host')
    fu.add_argument('--port', type=int)
    fu.add_argument('--version')
    fu.add_argument('--wipe', action='store_true')
    fu.add_argument('--all',  action='store_true',
                    help='Scan and push to all reachable devices (WiFi + BLE)')
    fu.add_argument('--transport', choices=['wifi_tcp', 'serial', 'ble'])
    fu.add_argument('--ble-name',  dest='ble_name', metavar='NAME',
                    help='BLE advertisement name (overrides bleName in ota.json)')
    fu.add_argument('--password',  metavar='PW',
                    help='OTA session password (overrides otaPassword in ota.json)')

    # wipe
    wi = sub.add_parser('wipe', help='Delete user files on device (preserves /lib, /config, /boot.py)')
    wi.add_argument('--host')
    wi.add_argument('--port',      type=int)
    wi.add_argument('--transport', choices=['wifi_tcp', 'serial', 'ble'])
    wi.add_argument('--ble-name',  dest='ble_name', metavar='NAME')
    wi.add_argument('--password',  metavar='PW')

    # terminal
    te = sub.add_parser('terminal', help='Interactive device terminal')
    te.add_argument('--host')
    te.add_argument('--port', type=int)
    te.add_argument('--transport', choices=['wifi_tcp', 'serial', 'ble'])
    te.add_argument('--ble-name',  dest='ble_name', metavar='NAME',
                    help='BLE advertisement name (overrides bleName in ota.json)')
    te.add_argument('--password',  metavar='PW',
                    help='OTA session password (overrides otaPassword in ota.json)')

    # version
    ve = sub.add_parser('version', help='Read version from device')
    ve.add_argument('--host')
    ve.add_argument('--port', type=int)
    ve.add_argument('--transport', choices=['wifi_tcp', 'serial', 'ble'])
    ve.add_argument('--ble-name',  dest='ble_name', metavar='NAME',
                    help='BLE advertisement name (overrides bleName in ota.json)')
    ve.add_argument('--password',  metavar='PW',
                    help='OTA session password (overrides otaPassword in ota.json)')

    # flash
    fl = sub.add_parser('flash', help='Flash MicroPython firmware via esptool')
    fl.add_argument('firmware')
    fl.add_argument('--port',  default=None)
    fl.add_argument('--baud',  type=int, default=460800)
    fl.add_argument('--chip',  default='auto')
    fl.add_argument('--erase', action='store_true')

    # serve
    sv = sub.add_parser('serve', help='HTTP server for http_pull transport')
    sv.add_argument('--host',    default='0.0.0.0')
    sv.add_argument('--port',    type=int, default=8080)
    sv.add_argument('--version', default=None)

    # bundle
    bu = sub.add_parser('bundle', help='Create a self-contained release bundle')
    bu.add_argument('--out',     default='dist')
    bu.add_argument('--zip',     action='store_true')
    bu.add_argument('--version', default=None)

    # list
    li = sub.add_parser('list', help='Scan for micro-ota devices on WiFi or BLE')
    li.add_argument('--transport', choices=['wifi_tcp', 'ble'], default=None,
                    help='Scan only WiFi or BLE (default: both)')
    li.add_argument('--port', type=int,
                    help='OTA port to probe on WiFi (default: port in ota.json or 2018)')
    li.add_argument('--timeout', type=float, default=5.0,
                    help='Scan duration in seconds (default: 5)')

    # passwd
    pa = sub.add_parser('passwd', help='Print SHA256 hash of a password for device config')
    pa.add_argument('password', help='Plaintext password to hash')

    # remoteio
    rio = sub.add_parser('remoteio', help='RemoteIO side-channel (listen / call)')
    rio.add_argument('subcmd', choices=['listen', 'call'], help='listen or call')
    rio.add_argument('handler', nargs='?', metavar='NAME',
                     help='Handler name (call only)')
    rio.add_argument('kwargs', nargs='*', metavar='key=val',
                     help='Handler arguments as key=value pairs (call only)')
    rio.add_argument('--transport', choices=['wifi_tcp', 'ble'], default=None,
                     help='Transport (default: wifi_tcp, or ble when device is BLE-only)')
    rio.add_argument('--ble-name', dest='ble_name', metavar='NAME',
                     help='BLE advertisement name (overrides bleName in ota.json)')
    rio.add_argument('--host', help='RemoteIO host (WiFi TCP only)')
    rio.add_argument('--port', type=int, help='RemoteIO port (WiFi TCP only)')

    args = p.parse_args()
    cfg  = load_config()

    dispatch = {
        'info':      cmd_info,
        'init':      cmd_init,
        'bootstrap': cmd_bootstrap,
        'fast':      cmd_fast,
        'full':      cmd_full,
        'wipe':      cmd_wipe,
        'terminal':  cmd_terminal,
        'version':   cmd_version,
        'flash':     cmd_flash,
        'serve':     cmd_serve,
        'bundle':    cmd_bundle,
        'remoteio':  cmd_remoteio,
        'list':      cmd_list,
        'passwd':    cmd_passwd,
    }

    try:
        dispatch[args.command](args, cfg)
    except KeyboardInterrupt:
        print('\nInterrupted.', file=sys.stderr)
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        if args.verbose:
            raise
        eff = dict(cfg)
        if getattr(args, 'host', None):
            eff['hostname'] = args.host
        if getattr(args, 'port', None):
            eff['port'] = args.port
        friendly = _friendly(exc, eff)
        if friendly:
            print('ERROR:', friendly, file=sys.stderr)
        else:
            print('ERROR:', exc, file=sys.stderr)
            print('       Run with -v for the full traceback.', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
