"""
Firmware flash utility for micro-ota.

Wraps esptool to flash MicroPython firmware (.bin) to ESP32 devices.
Works with esptool ≥ 3.x (Python API via esptool.main()).

Usage:
    python3 host/uota.py flash firmware.bin [--port /dev/ttyUSB0] [--baud 460800]
    python3 host/uota.py flash firmware.bin --erase   (full chip erase first)
    python3 host/firmware.py firmware.bin [options]

Flash addresses by chip (auto-detected when --chip auto):
    esp32        → 0x1000
    esp32s2/s3   → 0x0
    esp32c3/c6   → 0x0
"""

import os
import sys
import time


# Flash address by chip family.  esptool 'auto' chip detection will pick the
# right one automatically; these are fallbacks for explicit chip selection.
_FLASH_ADDR = {
    'esp32':   '0x1000',
    'esp32s2': '0x0',
    'esp32s3': '0x0',
    'esp32c3': '0x0',
    'esp32c6': '0x0',
    'esp32h2': '0x0',
    'auto':    '0x0',   # esptool handles auto-detection correctly
}


def flash(firmware_path, port, baud=460800, chip='auto', erase=False):
    """
    Flash *firmware_path* to the device on *port*.

    firmware_path — path to .bin firmware file
    port          — serial port (e.g. /dev/ttyUSB0, COM3)
    baud          — flash baud rate (default 460800; lower if unreliable)
    chip          — ESP chip type: 'auto' (default), 'esp32', 'esp32s3', …
    erase         — full chip erase before flashing (slower but clean slate)
    """
    _check_esptool()

    firmware_path = os.path.abspath(firmware_path)
    if not os.path.isfile(firmware_path):
        raise FileNotFoundError('Firmware file not found: ' + firmware_path)

    size_kb = os.path.getsize(firmware_path) // 1024
    print('Firmware : {} ({} KB)'.format(os.path.basename(firmware_path), size_kb))
    print('Port     : {}  baud={}'.format(port, baud))
    print('Chip     : {}'.format(chip))

    import esptool

    base_args = ['--chip', chip, '--port', port, '--baud', str(baud),
                 '--before', 'default_reset', '--after', 'hard_reset']

    if erase:
        print('\nErasing chip (this takes ~15s)…')
        esptool.main(base_args + ['erase_flash'])
        time.sleep(2)
        # Re-open after erase (device resets)
        base_args = ['--chip', chip, '--port', port, '--baud', str(baud),
                     '--before', 'default_reset', '--after', 'hard_reset']

    addr = _FLASH_ADDR.get(chip, '0x0')
    print('\nFlashing…')
    esptool.main(base_args + ['write_flash', '-z', addr, firmware_path])
    print('\nFlash complete. Device is resetting.')


def verify(firmware_path, port, chip='auto'):
    """Read back and verify the flashed firmware matches the file."""
    _check_esptool()
    import esptool
    size = os.path.getsize(firmware_path)
    addr = _FLASH_ADDR.get(chip, '0x0')
    print('Verifying {} bytes from {}…'.format(size, addr))
    esptool.main(['--chip', chip, '--port', port,
                  'verify_flash', addr, firmware_path])
    print('Verify OK.')


def chip_id(port):
    """Print chip info (MAC, flash size, chip type) without flashing."""
    _check_esptool()
    import esptool
    esptool.main(['--port', port, 'chip_id'])


def erase_chip(port, chip='auto'):
    """Full chip erase — destroys all flash contents including the filesystem."""
    _check_esptool()
    import esptool
    print('WARNING: this will erase ALL flash contents on', port)
    answer = input('Type YES to continue: ')
    if answer.strip() != 'YES':
        print('Aborted.')
        return
    esptool.main(['--chip', chip, '--port', port,
                  '--before', 'default_reset', '--after', 'hard_reset',
                  'erase_flash'])
    print('Erase complete.')


# ── helpers ───────────────────────────────────────────────────────────────────

def _check_esptool():
    try:
        import esptool
    except ImportError:
        print('ERROR: esptool is not installed.')
        print('Install with:  pip install esptool')
        sys.exit(1)


def _find_port(cfg=None):
    """Return the serial port from cfg, auto-detect, or raise."""
    if cfg:
        p = cfg.get('serialPort', '')
        if p:
            return p
    from host.transports.serial import auto_detect_port
    p = auto_detect_port()
    if p is None:
        print('ERROR: No serial port found. Connect the device or set serialPort in ota.json.')
        sys.exit(1)
    return p


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description='micro-ota firmware flasher')
    p.add_argument('firmware', help='Path to .bin firmware file')
    p.add_argument('--port',  default=None, help='Serial port (auto-detected if omitted)')
    p.add_argument('--baud',  type=int, default=460800, help='Baud rate (default 460800)')
    p.add_argument('--chip',  default='auto',
                   help='Chip type: auto (default), esp32, esp32s2, esp32s3, esp32c3')
    p.add_argument('--erase', action='store_true',
                   help='Full chip erase before flashing')
    args = p.parse_args()

    port = args.port or _find_port()
    flash(args.firmware, port, baud=args.baud, chip=args.chip, erase=args.erase)


if __name__ == '__main__':
    main()
