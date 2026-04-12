"""
Firmware flash utility for micro-ota.

Wraps esptool to flash MicroPython firmware (.bin) to ESP32 devices.
"""

import os
import sys
import time

_FLASH_ADDR = {
    'esp32':   '0x1000',
    'esp32s2': '0x0',
    'esp32s3': '0x0',
    'esp32c3': '0x0',
    'esp32c6': '0x0',
    'esp32h2': '0x0',
    'auto':    '0x0',
}


def flash(firmware_path, port, baud=460800, chip='auto', erase=False):
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
        base_args = ['--chip', chip, '--port', port, '--baud', str(baud),
                     '--before', 'default_reset', '--after', 'hard_reset']

    addr = _FLASH_ADDR.get(chip, '0x0')
    print('\nFlashing…')
    esptool.main(base_args + ['write_flash', '-z', addr, firmware_path])
    print('\nFlash complete. Device is resetting.')


def verify(firmware_path, port, chip='auto'):
    _check_esptool()
    import esptool
    size = os.path.getsize(firmware_path)
    addr = _FLASH_ADDR.get(chip, '0x0')
    print('Verifying {} bytes from {}…'.format(size, addr))
    esptool.main(['--chip', chip, '--port', port,
                  'verify_flash', addr, firmware_path])
    print('Verify OK.')


def chip_id(port):
    _check_esptool()
    import esptool
    esptool.main(['--port', port, 'chip_id'])


def erase_chip(port, chip='auto'):
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


def _check_esptool():
    try:
        import esptool
    except ImportError:
        print('ERROR: esptool is not installed.')
        print('Install with:  pip install esptool  or  pip install micro-ota[flash]')
        sys.exit(1)


def _find_port(cfg=None):
    if cfg:
        p = cfg.get('serialPort', '')
        if p:
            return p
    from .transports.serial import auto_detect_port
    p = auto_detect_port()
    if p is None:
        print('ERROR: No serial port found. Connect the device or set serialPort in ota.json.')
        sys.exit(1)
    return p
