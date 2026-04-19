#!/usr/bin/env python3
"""
uota - micro-ota host CLI

Commands:
  init       [--dir DIR] [--force]
  bootstrap  [--port PORT] [--baud BAUD]
  fast        [--host HOST] [--port PORT] [--transport T] [--version VER]
  full        [--host HOST] [--port PORT] [--transport T] [--version VER] [--wipe]
  terminal    [--host HOST] [--port PORT] [--transport T]
  version     [--host HOST] [--port PORT] [--transport T]
  flash <firmware.bin> [--port PORT] [--baud BAUD] [--chip CHIP] [--erase]
  serve       [--host HOST] [--port PORT] [--version VER]
  bundle      [--out DIR] [--zip] [--version VER]
  remoteio    listen | call <name> [key=val ...]

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
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


# -- transport factory ---------------------------------------------------------

def get_transport(cfg, host_override=None, port_override=None, transport_override=None):
    names = [transport_override] if transport_override else cfg.get('transports', ['wifi_tcp'])

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
            ble_name = host_override or cfg.get('bleName', 'micro-ota')
            return BLETransport(ble_name)

    raise RuntimeError('No supported transport in config')


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

def send_ota(transport, files, manifest, wipe=False):
    start = time.time()
    with transport:
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


def send_stream_ota(transport, files, manifest, key='', wipe=False):
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
            send_ota(transport, files, manifest, wipe=False)
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
        if resp == 'ok':
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


# -- commands ------------------------------------------------------------------

def cmd_init(args, cfg):
    """Initialize a new micro-ota project in the target directory."""
    import shutil

    target = Path(getattr(args, 'dir', None) or '.').resolve()
    target.mkdir(parents=True, exist_ok=True)
    force  = getattr(args, 'force', False)

    # 1. Copy device OTA infrastructure files
    device_src = Path(__file__).parent / '_device'
    device_dst = target / 'lib' / 'uota'
    if device_dst.exists() and not force:
        print('[init] lib/uota/ already exists -- skipping (use --force to overwrite)')
    else:
        if device_dst.exists():
            shutil.rmtree(device_dst)
        shutil.copytree(device_src, device_dst,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
        print('[init] Copied OTA device files -> lib/uota/')

    # 2. Create config/ with ota.json inside
    config_dir = target / 'config'
    config_dir.mkdir(exist_ok=True)
    ota_json = config_dir / 'ota.json'
    if not ota_json.exists():
        default = {
            "version":      "1.0.0",
            "hostname":     "192.168.1.100",
            "port":         2018,
            "ssid":         "",
            "password":     "",
            "otaKey":       "",
            "serialPort":   "",
            "transports":   ["wifi_tcp"],
            "manifestUrl":  "",
            "pullInterval": 60,
            # config/ is included so device config stays in sync on every push.
            # data/ is excluded — it is runtime state managed by the device.
            "fastOtaFiles": ["app/**", "main.py", "config/**"],
            # Extra files for 'uota full' on top of fast (e.g. shared libs).
            "fullOtaFiles": [],
            "excludedFiles": [
                ".git/**", "lib/uota/**", "*.zip", "dist/**", "data/**"
            ],
        }
        with open(ota_json, 'w') as f:
            json.dump(default, f, indent=4)
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
    if not boot_py.exists():
        boot_py.write_text(_BOOT_PY)
        print('[init] Created boot.py')

    print('\nProject layout:')
    print('  app/           ← application code       (fast + full OTA)')
    print('  config/ota.json← OTA + app config        (fast + full OTA, always in sync)')
    print('  data/          ← runtime data on device  (never touched by OTA or wipe)')
    print('  lib/uota/      ← OTA system files        (managed by bootstrap)')
    print('\nNext steps:')
    print('  1. Edit config/ota.json -- fill in ssid, password, hostname')
    print('  2. Connect your ESP32 via USB')
    print('  3. Run: uota bootstrap   (first-time device setup)')
    print('  4. Run: uota fast        (push app/ + config/ updates)')


def cmd_bootstrap(args, cfg):
    from .bootstrap import run as do_bootstrap
    do_bootstrap(
        port=_normalise_serial_port(args.port or cfg.get('serialPort')),
        baud=args.baud,
        mpy=args.mpy,
    )


def cmd_fast(args, cfg):
    patterns  = cfg.get('fastOtaFiles', ['main.py'])
    excludes  = cfg.get('excludedFiles', [])
    version   = args.version or cfg.get('version', 'unknown')
    manifest  = build_manifest(patterns, excludes, version)
    key       = cfg.get('otaKey', '')
    files     = {p: p for p in manifest['files']}
    transport = get_transport(cfg, args.host, args.port, getattr(args, 'transport', None))
    send_stream_ota(transport, files, manifest, key=key)


def cmd_full(args, cfg):
    patterns  = cfg.get('fastOtaFiles', []) + cfg.get('fullOtaFiles', [])
    excludes  = cfg.get('excludedFiles', [])
    version   = args.version or cfg.get('version', 'unknown')
    manifest  = build_manifest(patterns, excludes, version)
    key       = cfg.get('otaKey', '')
    files     = {p: p for p in manifest['files']}
    transport = get_transport(cfg, args.host, args.port, getattr(args, 'transport', None))
    send_stream_ota(transport, files, manifest, key=key, wipe=args.wipe)


def cmd_terminal(args, cfg):
    transport = get_transport(cfg, args.host, args.port, getattr(args, 'transport', None))
    print("Terminal mode. Type 'exit' to quit.")
    print("Commands: ping, version, ls [path], get <path>, rm <path>, reset, wipe")
    try:
        transport.connect()
    except OSError as e:
        print('Connection error:', e)
        return
    try:
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
    transport = get_transport(cfg, args.host, args.port, getattr(args, 'transport', None))
    with transport:
        transport.write_line('version')
        print(transport.read_line())


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


def cmd_bundle(args, cfg):
    from .bundle import build
    build(
        out_dir=args.out,
        make_zip=args.zip,
        version=args.version or cfg.get('version'),
    )


def cmd_remoteio(args, cfg):
    """Delegate to the remoteio CLI."""
    from .remoteio import main as remoteio_main
    # Reconstruct argv so remoteio.main() sees it
    argv = [args.subcmd] + (args.remoteio_args or [])
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
    fa.add_argument('--transport', choices=['wifi_tcp', 'serial', 'ble'])

    # full
    fu = sub.add_parser('full', help='Push all managed files to the device')
    fu.add_argument('--host')
    fu.add_argument('--port', type=int)
    fu.add_argument('--version')
    fu.add_argument('--wipe', action='store_true')
    fu.add_argument('--transport', choices=['wifi_tcp', 'serial', 'ble'])

    # terminal
    te = sub.add_parser('terminal', help='Interactive device terminal')
    te.add_argument('--host')
    te.add_argument('--port', type=int)
    te.add_argument('--transport', choices=['wifi_tcp', 'serial', 'ble'])

    # version
    ve = sub.add_parser('version', help='Read version from device')
    ve.add_argument('--host')
    ve.add_argument('--port', type=int)
    ve.add_argument('--transport', choices=['wifi_tcp', 'serial', 'ble'])

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

    # remoteio
    rio = sub.add_parser('remoteio', help='RemoteIO side-channel (listen / call)')
    rio.add_argument('subcmd', choices=['listen', 'call'], help='listen or call')
    rio.add_argument('remoteio_args', nargs=argparse.REMAINDER)

    args = p.parse_args()
    cfg  = load_config()

    dispatch = {
        'init':      cmd_init,
        'bootstrap': cmd_bootstrap,
        'fast':      cmd_fast,
        'full':      cmd_full,
        'terminal':  cmd_terminal,
        'version':   cmd_version,
        'flash':     cmd_flash,
        'serve':     cmd_serve,
        'bundle':    cmd_bundle,
        'remoteio':  cmd_remoteio,
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
