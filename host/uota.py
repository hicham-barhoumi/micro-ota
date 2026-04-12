#!/usr/bin/env python3
"""
uota – micro-ota host CLI

Usage:
  uota bootstrap  [--port PORT] [--baud BAUD]
  uota fast        [--host HOST] [--port PORT] [--version VER]
  uota full        [--host HOST] [--port PORT] [--version VER] [--wipe]
  uota terminal    [--host HOST] [--port PORT]
  uota version     [--host HOST] [--port PORT]
  uota flash <firmware.bin> [--port PORT] [--baud BAUD]   (Phase 3)

All connection params default to values in ota.json when not specified.
"""

import argparse
import errno
import json
import os
import sys
import socket
import time

# Make sure we can import host.* when running from project root
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host.manifest import build as build_manifest, to_json as manifest_to_json
from host.transports.wifi_tcp import WiFiTCPTransport
from host.transports.serial import SerialOTATransport


# ── error handling ────────────────────────────────────────────────────────────

def _friendly(exc, cfg=None):
    """
    Map a low-level exception to a one-line human-readable message.
    Returns None for genuinely unexpected errors so the caller can fall back
    to printing the raw exception text.
    """
    msg = str(exc)
    eno = getattr(exc, 'errno', None)

    # Serial-specific errors (import lazily; pyserial may not be installed)
    try:
        import serial
        if isinstance(exc, serial.SerialException):
            if 'No such file' in msg or 'cannot find' in msg.lower():
                port = msg.split("'")[1] if "'" in msg else msg
                return ("Serial port not found: {}\n"
                        "  Check the USB cable or set serialPort in ota.json.").format(port)
            if 'Permission denied' in msg or 'Access is denied' in msg:
                port = msg.split("'")[1] if "'" in msg else '(port)'
                return ("Permission denied on {}.\n"
                        "  Run: sudo usermod -aG dialout $USER  then log out and back in.\n"
                        "  Or prefix your command with: sg dialout -c \"...\"").format(port)
            return 'Serial error: ' + msg
    except ImportError:
        pass

    # Network timeouts
    if isinstance(exc, (TimeoutError, socket.timeout)):
        host = cfg.get('hostname', '?') if cfg else '?'
        port = cfg.get('port', 2018) if cfg else '?'
        return ("Timed out connecting to {}:{}.\n"
                "  Is the device on WiFi and is the OTA server running?").format(host, port)

    # Connection refused (device reachable but port not listening)
    if isinstance(exc, ConnectionRefusedError) or eno == errno.ECONNREFUSED:
        host = cfg.get('hostname', '?') if cfg else '?'
        port = cfg.get('port', 2018) if cfg else '?'
        return ("Connection refused at {}:{}.\n"
                "  The OTA server may not have started yet — wait a few seconds and retry.\n"
                "  Or run: uota terminal  to check device state.").format(host, port)

    # Host unreachable / no route
    if eno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN):
        return ("Network unreachable. Check that the host and device are on the same network\n"
                "  and that hostname/IP in ota.json is correct.")

    if isinstance(exc, OSError):
        # Serial port vanished mid-transfer
        if eno == errno.EIO:
            return 'I/O error on serial port. Device disconnected during transfer?'
        if eno == errno.ENOENT:
            return 'Device or file not found: ' + msg
        if 'connection closed' in msg.lower():
            return ('Device closed the connection unexpectedly.\n'
                    '  It may have reset mid-transfer — check the serial console.')

    if isinstance(exc, RuntimeError):
        # Raw REPL entry failures
        if 'raw REPL' in msg:
            return ("Could not enter MicroPython raw REPL.\n"
                    "  Press the Reset button on the device and retry.\n"
                    "  Detail: " + msg)
        # Inline OTA server injection
        if 'Inline OTA' in msg or 'failed to start' in msg.lower():
            return ("Serial OTA server injection failed.\n"
                    "  Reset the device and retry: uota fast --transport serial\n"
                    "  Detail: " + msg)
        # Device protocol disagreements
        if 'not ready' in msg or 'ready' in msg:
            return 'Device rejected OTA session: ' + msg
        if 'No OTA transport' in msg:
            return ("No transport configured.\n"
                    "  Set 'transports' in ota.json (e.g. [\"wifi_tcp\"] or [\"serial\"]).")
        # esptool / firmware errors bubble through RuntimeError too
        return 'Error: ' + msg

    if isinstance(exc, FileNotFoundError):
        return 'File not found: ' + msg

    return None   # unexpected — caller will show raw exc + verbose hint


# ── config ────────────────────────────────────────────────────────────────────

def load_config(path='ota.json'):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


# ── transport factory ─────────────────────────────────────────────────────────

def get_transport(cfg, host_override=None, port_override=None, transport_override=None):
    """Return the first usable host transport from config (or override)."""
    names = [transport_override] if transport_override else cfg.get('transports', ['wifi_tcp'])

    for name in names:
        if name == 'wifi_tcp':
            host = host_override or cfg.get('hostname', 'micropython')
            port = port_override or cfg.get('port', 2018)
            return WiFiTCPTransport(host, port)
        if name == 'serial':
            port = host_override or cfg.get('serialPort') or auto_detect_serial()
            baud = cfg.get('serialBaud', 115200)
            return SerialOTATransport(port, baud)

    raise RuntimeError('No supported transport in config')


def auto_detect_serial():
    from host.transports.serial import auto_detect_port
    p = auto_detect_port()
    if p is None:
        print('ERROR: No serial port found. Connect the device or set serialPort in ota.json.')
        sys.exit(1)
    return p


# ── OTA push ──────────────────────────────────────────────────────────────────

def send_ota(transport, files, manifest, wipe=False):
    """Push files to the device. files is a dict {rel_path: abs_path}."""
    start = time.time()

    with transport:
        # Optional wipe first (separate connection – device closes after each cmd)
        if wipe:
            transport.connect()
            transport.write_line('wipe')
            resp = transport.read_line()
            if resp.strip() != 'ok':
                print('Wipe failed:', resp)
                sys.exit(1)
            print('Device wiped.')
            transport.close()
            time.sleep(1)
            transport.connect()

        # Start OTA session
        transport.write_line('start_ota')
        resp = transport.read_line()
        if resp.strip() != 'ready':
            print('Device not ready:', resp)
            sys.exit(1)

        # Send manifest
        m_json = manifest_to_json(manifest).encode()
        transport.write_line('manifest {}'.format(len(m_json)))
        transport.write(m_json)
        resp = transport.read_line()
        if resp.strip() != 'ok':
            print('Manifest rejected:', resp)
            transport.write_line('abort')
            sys.exit(1)

        # Send files
        total_files = len(files)
        for i, (rel_path, abs_path) in enumerate(files.items(), 1):
            info = manifest['files'][rel_path]
            size   = info['size']
            sha256 = info['sha256']

            print('  [{}/{}] {}'.format(i, total_files, rel_path), end='  ', flush=True)
            transport.write_line('file {};{};{}'.format(rel_path, size, sha256))

            sent = 0
            t0 = time.time()
            with open(abs_path, 'rb') as f:
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    transport.write(chunk)
                    sent += len(chunk)

            resp = transport.read_line().strip()
            elapsed = time.time() - t0
            speed = (size / 1024) / elapsed if elapsed > 0 else 0
            if resp == 'ok':
                print('{:.1f} KB/s'.format(speed))
            else:
                print('\nFailed:', resp)
                transport.write_line('abort')
                sys.exit(1)

        # Commit
        transport.write_line('end_ota')
        resp = transport.read_line()
        if resp.strip() == 'ok':
            elapsed = time.time() - start
            print('\nOTA done in {:.1f}s  ({} files)'.format(elapsed, total_files))
        # Device will reset — connection may drop before we read 'ok', that's fine


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_bootstrap(args, cfg):
    from host.bootstrap import run as do_bootstrap
    do_bootstrap(
        port=args.port or cfg.get('serialPort'),
        baud=args.baud,
        project_root=_ROOT,
    )


def cmd_fast(args, cfg):
    patterns  = cfg.get('fastOtaFiles', ['main.py'])
    excludes  = cfg.get('excludedFiles', [])
    version   = args.version or cfg.get('version', 'unknown')
    manifest  = build_manifest(patterns, excludes, version)
    files     = {p: p for p in manifest['files']}
    transport = get_transport(cfg, args.host, args.port, getattr(args, 'transport', None))
    print('Fast OTA: {} file(s)'.format(len(files)))
    send_ota(transport, files, manifest)


def cmd_full(args, cfg):
    patterns  = cfg.get('fastOtaFiles', []) + cfg.get('fullOtaFiles', [])
    excludes  = cfg.get('excludedFiles', [])
    version   = args.version or cfg.get('version', 'unknown')
    manifest  = build_manifest(patterns, excludes, version)
    files     = {p: p for p in manifest['files']}
    transport = get_transport(cfg, args.host, args.port, getattr(args, 'transport', None))
    print('Full OTA: {} file(s){}'.format(len(files), '  [wipe first]' if args.wipe else ''))
    send_ota(transport, files, manifest, wipe=args.wipe)


def cmd_terminal(args, cfg):
    transport = get_transport(cfg, args.host, args.port, getattr(args, 'transport', None))
    print("Terminal mode. Type 'exit' to quit.")
    print("Commands: ping, version, ls [path], get <path>, rm <path>, reset, wipe")
    while True:
        try:
            line = input('$ ').strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line or line.lower() in ('exit', 'quit'):
            break
        try:
            with transport:
                transport.write_line(line)
                # Some commands return binary (get), handle separately
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
                else:
                    resp = transport.read_line()
                    print(resp)
        except OSError as e:
            print('Connection error:', e)


def cmd_version(args, cfg):
    transport = get_transport(cfg, args.host, args.port, getattr(args, 'transport', None))
    with transport:
        transport.write_line('version')
        print(transport.read_line())


def cmd_flash(args, cfg):
    from host.firmware import flash, _find_port
    port = args.port or _find_port(cfg)
    flash(
        firmware_path=args.firmware,
        port=port,
        baud=args.baud,
        chip=args.chip,
        erase=args.erase,
    )


def cmd_serve(args, cfg):
    from host.serve import serve
    serve(
        host=args.host,
        port=args.port,
        version=args.version or cfg.get('version'),
    )


def cmd_bundle(args, cfg):
    from host.bundle import build
    build(
        out_dir=args.out,
        make_zip=args.zip,
        version=args.version or cfg.get('version'),
    )


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        prog='uota',
        description='micro-ota host tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('-v', '--verbose', action='store_true',
                   help='Show full traceback on errors')
    sub = p.add_subparsers(dest='command', required=True)

    # bootstrap
    bs = sub.add_parser('bootstrap', help='First-time upload of OTA lib via serial')
    bs.add_argument('--port', help='Serial port (auto-detected if omitted)')
    bs.add_argument('--baud', type=int, default=115200)

    # fast
    fa = sub.add_parser('fast', help='Push fastOtaFiles to the device')
    fa.add_argument('--host',      help='Device hostname or IP')
    fa.add_argument('--port',      type=int, help='TCP port (WiFi) or serial port path')
    fa.add_argument('--version',   help='Version string to embed in manifest')
    fa.add_argument('--transport', choices=['wifi_tcp', 'serial'],
                    help='Force transport (default: first in ota.json transports list)')

    # full
    fu = sub.add_parser('full', help='Push all managed files to the device')
    fu.add_argument('--host')
    fu.add_argument('--port', type=int)
    fu.add_argument('--version')
    fu.add_argument('--wipe', action='store_true', help='Wipe device before upload')
    fu.add_argument('--transport', choices=['wifi_tcp', 'serial'])

    # terminal
    te = sub.add_parser('terminal', help='Interactive device terminal over WiFi')
    te.add_argument('--host')
    te.add_argument('--port', type=int)
    te.add_argument('--transport', choices=['wifi_tcp', 'serial'])

    # version
    ve = sub.add_parser('version', help='Read version from device')
    ve.add_argument('--host')
    ve.add_argument('--port', type=int)
    ve.add_argument('--transport', choices=['wifi_tcp', 'serial'])

    # flash
    fl = sub.add_parser('flash', help='Flash MicroPython firmware via esptool')
    fl.add_argument('firmware', help='Path to .bin firmware file')
    fl.add_argument('--port',  default=None, help='Serial port (auto-detected if omitted)')
    fl.add_argument('--baud',  type=int, default=460800, help='Flash baud rate (default 460800)')
    fl.add_argument('--chip',  default='auto',
                    help='Chip: auto (default), esp32, esp32s2, esp32s3, esp32c3')
    fl.add_argument('--erase', action='store_true',
                    help='Full chip erase before flashing')

    # serve — HTTP file server for http_pull transport
    sv = sub.add_parser('serve', help='Serve managed files over HTTP for http_pull transport')
    sv.add_argument('--host',    default='0.0.0.0', help='Bind address (default 0.0.0.0)')
    sv.add_argument('--port',    type=int, default=8080, help='TCP port (default 8080)')
    sv.add_argument('--version', default=None, help='Version string override')

    # bundle — create a self-contained release directory / ZIP
    bu = sub.add_parser('bundle', help='Create a self-contained release bundle')
    bu.add_argument('--out',     default='dist', help='Output directory (default dist)')
    bu.add_argument('--zip',     action='store_true', help='Also create dist.zip')
    bu.add_argument('--version', default=None, help='Version string override')

    args = p.parse_args()
    cfg  = load_config()

    dispatch = {
        'bootstrap': cmd_bootstrap,
        'fast':      cmd_fast,
        'full':      cmd_full,
        'terminal':  cmd_terminal,
        'version':   cmd_version,
        'flash':     cmd_flash,
        'serve':     cmd_serve,
        'bundle':    cmd_bundle,
    }

    try:
        dispatch[args.command](args, cfg)
    except KeyboardInterrupt:
        print('\nInterrupted.', file=sys.stderr)
        sys.exit(130)
    except SystemExit:
        raise   # already handled (e.g. argparse, explicit sys.exit)
    except Exception as exc:
        if args.verbose:
            raise
        # Build an effective config that reflects the actual host/port used
        # (args overrides take precedence over ota.json values)
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
