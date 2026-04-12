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
    print('Firmware flash is coming in Phase 5.')
    print('For now, use esptool directly:')
    print('  esptool.py --port {} write_flash 0x0 {}'.format(
        args.port or cfg.get('serialPort', '/dev/ttyUSB0'),
        args.firmware,
    ))


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

    # flash  (Phase 5 stub)
    fl = sub.add_parser('flash', help='Flash MicroPython firmware (Phase 5)')
    fl.add_argument('firmware', help='Path to .bin firmware file')
    fl.add_argument('--port')
    fl.add_argument('--baud', type=int, default=460800)

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

    {
        'bootstrap': cmd_bootstrap,
        'fast':      cmd_fast,
        'full':      cmd_full,
        'terminal':  cmd_terminal,
        'version':   cmd_version,
        'flash':     cmd_flash,
        'serve':     cmd_serve,
        'bundle':    cmd_bundle,
    }[args.command](args, cfg)


if __name__ == '__main__':
    main()
