# micro-ota

Simple, fast, reliable Over-The-Air updates for MicroPython. Push code to your ESP32 over WiFi, USB serial, or BLE; talk to it in real time over a persistent side-channel; roll back crashed firmware automatically; sign manifests with HMAC-SHA256 so only authorised pushes are accepted.

---

## Install

```bash
pip install micro-ota                   # WiFi + serial only
pip install micro-ota[ble]              # + BLE transport (bleak)
pip install micro-ota[flash]            # + firmware flashing (esptool)
pip install micro-ota[all]              # everything
```

---

## Quick start

```bash
mkdir myproject && cd myproject
uota init                   # creates ota.json, device/, main.py, boot.py
```

Edit `ota.json` — fill in `ssid`, `password`, `hostname` (device IP).

```bash
uota bootstrap              # first-time: uploads OTA library to ESP32 via USB
uota fast                   # push main.py over WiFi
uota full                   # push all managed files
uota terminal               # interactive device shell
```

`uota init` copies the OTA infrastructure files into `device/` in your project so you can inspect or customise them before bootstrap.

See `examples/basic/` for a complete starter project.

---

## How it works

```
[ PC ]   uota fast              uota remoteio listen
            │  TCP :2018               │  TCP :2019
            ▼                          ▼
[ ESP32 ]  /lib/ota.py  ───────  /lib/remoteio.py
                │        /lib/boot_guard.py
                ▼
           main.py  (your app)
```

Three background threads start at boot:

- **OTA server** (port 2018) — accepts file pushes, verifies HMAC signature, applies atomically, resets.
- **RemoteIO server** (port 2019) — forwards `print()` output to the host and handles named RPC calls.
- **Boot guard** — counts consecutive crashes so a bad update cannot brick the device.

WiFi, USB serial, BLE, and HTTP pull are all supported as transports.

---

## Device filesystem layout

After `uota bootstrap`, the device filesystem looks like this:

```
/boot.py                     ← starts OTA + RemoteIO threads
/ota.json                    ← config (ssid, hostname, otaKey, transports …)
/lib/
    ota.py  (or .mpy)        ← OTA server
    boot_guard.py
    remoteio.py
    transports/
        wifi_tcp.py
        serial.py
        ble.py
        http_pull.py
/main.py                     ← your application
```

`/lib` is on MicroPython's default `sys.path`, so `import ota`, `import boot_guard` etc. work without any path changes.

---

## Project structure (your project after `uota init`)

```
myproject/
├── ota.json            ← configuration (keep out of git — has credentials)
├── main.py             ← your application
├── boot.py             ← auto-generated: starts OTA + RemoteIO threads
└── device/             ← OTA infrastructure files (copied from package)
    ├── ota.py
    ├── boot_guard.py
    ├── remoteio.py
    └── transports/
        ├── wifi_tcp.py
        ├── serial.py
        ├── ble.py
        └── http_pull.py
```

---

## CLI reference

```
uota <command> [options]
```

| Command | Description |
|---|---|
| `init [--dir DIR] [--force]` | Initialize project — copy device files + create `ota.json` |
| `bootstrap [--port PORT] [--baud BAUD] [--mpy]` | First-time upload of OTA library via serial |
| `fast [--transport T]` | Push `fastOtaFiles` (default: `main.py`) |
| `full [--transport T] [--wipe]` | Push all managed files |
| `terminal [--transport T]` | Interactive device shell |
| `version [--transport T]` | Read installed version from device |
| `flash <file.bin> [--chip CHIP] [--erase]` | Flash MicroPython firmware via esptool |
| `serve [--host H] [--port P]` | HTTP file server for `http_pull` transport |
| `bundle [--out DIR] [--zip]` | Build a self-contained release bundle |
| `remoteio listen` | Stream device `print()` output to terminal |
| `remoteio call <name> [key=val ...]` | Call a named handler on the device |

### Transport options

All `fast`, `full`, `version`, `terminal` commands accept:

| Option | Default | Description |
|---|---|---|
| `--host HOST` | `ota.json hostname` | Device IP or hostname |
| `--port PORT` | `ota.json port` | TCP port |
| `--transport wifi_tcp\|serial\|ble` | first in `ota.json transports` | Transport |
| `--version VER` | `ota.json version` | Version string to embed |

### `--mpy` flag (bootstrap)

```bash
uota bootstrap --mpy
```

Compiles all OTA infrastructure files to `.mpy` bytecode with `mpy-cross` before uploading. Faster import time and lower RAM usage on the device. Requires `mpy-cross` to be installed and version-matched to the device's MicroPython firmware. Falls back to `.py` gracefully if `mpy-cross` is not found.

---

## Transports

### WiFi TCP (default)

```json
{ "transports": ["wifi_tcp"], "hostname": "192.168.1.100", "port": 2018 }
```

```bash
uota fast
uota fast --host 192.168.1.200
```

### USB Serial

```json
{ "transports": ["serial"], "serialPort": "/dev/ttyUSB0" }
```

```bash
uota fast --transport serial
```

Enters the raw REPL, injects an inline OTA server, speaks the standard protocol over UART. No WiFi needed. Port is auto-detected from connected ESP32 devices.

### BLE (Nordic UART Service)

```bash
pip install micro-ota[ble]
```

```json
{ "transports": ["ble"], "bleName": "micro-ota" }
```

```bash
uota fast --transport ble
```

The device advertises as a BLE peripheral. The host scans by name, connects, and speaks the standard protocol over NUS characteristics.

### HTTP Pull

The device polls a manifest URL on an interval and self-updates when the version changes. No host connection needed at update time.

```json
{
    "transports": ["http_pull"],
    "manifestUrl": "http://192.168.1.50:8080/manifest.json",
    "pullInterval": 60
}
```

```bash
uota serve          # start HTTP server on port 8080
uota bundle --zip   # or build a static bundle for any web server
```

---

## Security — HMAC-SHA256 manifest signing

Set a shared secret in `ota.json` on both the host and the device:

```json
{ "otaKey": "your-secret-key" }
```

The host signs every manifest with HMAC-SHA256 before sending it. The device verifies the signature before accepting any OTA push. A missing or incorrect signature causes the device to respond `sig_mismatch` and abort.

Leave `otaKey` empty (the default) to disable signing — fully backward compatible.

**Signing payload** (deterministic, order-independent):

```
<version>
<path>:<sha256>
<path>:<sha256>
...                 (file paths sorted lexicographically)
```

---

## RemoteIO

A persistent side-channel on port 2019 for streaming `print()` output and calling named handlers on the device.

### CLI

```bash
uota remoteio listen                    # stream all device print() output
uota remoteio call ping
uota remoteio call free_mem
uota remoteio call version
uota remoteio call echo msg=hello
```

### Python API

```python
from uota.remoteio import RemoteIOClient

with RemoteIOClient('192.168.1.100') as rio:
    print(rio.call('ping'))         # 'pong'
    print(rio.call('free_mem'))     # 98304
    print(rio.call('uptime_ms'))    # 12345
```

### Registering handlers on the device

```python
# main.py
import remoteio

@remoteio.on('sensor_data')
def _():
    return {'temp': read_temp(), 'humidity': read_hum()}

@remoteio.on('set_led')
def _(state=False):
    led.value(state)
    return 'ok'
```

### Built-in handlers

| Name | Returns |
|---|---|
| `ping` | `'pong'` |
| `version` | `{"version": "x.y.z"}` |
| `free_mem` | free heap bytes |
| `uptime_ms` | ms since boot |

---

## `ota.json` reference

```json
{
    "version":      "1.0.0",
    "hostname":     "192.168.1.100",
    "port":         2018,
    "remoteioPort": 2019,
    "ssid":         "MyWiFi",
    "password":     "MyPassword",
    "otaKey":       "",
    "bleName":      "micro-ota",
    "serialPort":   "",
    "transports":   ["wifi_tcp"],
    "manifestUrl":  "",
    "pullInterval": 60,
    "excludedFiles": [".git/**", "device/**", "*.zip", "dist/**"],
    "fastOtaFiles": ["main.py"],
    "fullOtaFiles": ["*.py", "lib/**", "*.json"]
}
```

| Key | Description |
|---|---|
| `version` | Version string embedded in the manifest after each OTA |
| `hostname` | Device IP for WiFi OTA and RemoteIO |
| `port` | OTA server port (default `2018`) |
| `remoteioPort` | RemoteIO server port (default `2019`) |
| `ssid` / `password` | WiFi credentials (stored on device) |
| `otaKey` | HMAC-SHA256 signing key — empty disables signing |
| `bleName` | BLE advertisement name (max 20 chars) |
| `serialPort` | Serial port (auto-detected if empty) |
| `transports` | Active transports on the device |
| `manifestUrl` | HTTP pull manifest URL |
| `pullInterval` | HTTP pull poll interval in seconds |
| `excludedFiles` | Glob patterns excluded from all OTA uploads |
| `fastOtaFiles` | Files pushed by `uota fast` |
| `fullOtaFiles` | Files pushed by `uota full` |

> `ota.json` contains WiFi credentials — add it to `.gitignore`. Use `examples/basic/ota.json` as a template.

---

## Firmware flash

```bash
pip install micro-ota[flash]

# Basic (auto-detects port and chip)
uota flash esp32-20240602-v1.23.0.bin

# Full options
uota flash firmware.bin \
    --port /dev/ttyUSB0 \
    --baud 460800 \
    --chip esp32 \
    --erase          # full chip erase first
```

Flash addresses are set automatically: ESP32 → `0x1000`; S2/S3/C3/C6/H2 → `0x0`.

After flashing, run `uota bootstrap` to re-upload the OTA library.

---

## Boot guard

`boot_guard.py` tracks consecutive unclean boots in `/ota_boot_state.json`. The OTA server calls `mark_clean()` once running, resetting the counter.

On **3 consecutive crashes**:
1. On ESP32 with dual-partition firmware, switches to the previous firmware partition and reboots (automatic rollback).
2. `mark_clean()` also calls `esp32.Partition.mark_app_valid_cancel_rollback()` to confirm stability.

To recover a bricked device:
```bash
uota bootstrap      # re-upload OTA library via USB
uota flash fw.bin   # or reflash MicroPython firmware
```

---

## OTA protocol

| Command | Response | Description |
|---|---|---|
| `ping` | `pong` | Liveness check |
| `version` | `{"version":"x.y.z"}` | Read installed version |
| `ls [path]` | filenames, one per line | List directory |
| `get <path>` | `<size>\n<binary>` | Download a file |
| `rm <path>` | `ok` / `error: ...` | Delete a file |
| `reset` | `ok` then resets | Soft reset |
| `wipe` | `ok` | Delete user files, keep `/lib` |
| `start_ota` | `ready` | Begin OTA session |

### OTA session

```
manifest <size>\n<json>              → ok / sig_mismatch
file <name>;<size>;<sha256>\n<bin>   → ok / sha256_mismatch
...
end_ota                              → ok  (atomic commit + reset)
abort                                → aborted  (staging discarded)
```

Files are staged in `/ota_stage/`. On `end_ota`: old files not in the new manifest are deleted, staged files are moved atomically, version is written, device resets.

If `otaKey` is set on the device and the manifest signature is missing or incorrect, the device responds `sig_mismatch` and the session is aborted before any file is transferred.

---

## VS Code extension

Located in `packages/vscode/`. Activates automatically when `ota.json` is present in the workspace.

### Build

```bash
bash scripts/build.sh       # builds pip package + VS Code extension into dist/
```

Or build the extension separately:

```bash
cd packages/vscode
npm install
npm run compile
npx vsce package            # produces micro-ota-1.0.0.vsix in current dir
```

### Install

```
Extensions → ⋯ → Install from VSIX…
```

### Commands (Command Palette: `Ctrl+Shift+P`)

| Command | Description |
|---|---|
| `micro-ota: Initialize Project` | `uota init` |
| `micro-ota: Bootstrap Device (Serial)` | `uota bootstrap` |
| `micro-ota: Fast OTA Push` | `uota fast` |
| `micro-ota: Full OTA Push` | `uota full` (prompts for --wipe) |
| `micro-ota: Open Device Terminal` | `uota terminal` |
| `micro-ota: Read Device Version` | `uota version` |
| `micro-ota: Flash Firmware (.bin)` | `uota flash` (file picker) |
| `micro-ota: Start HTTP OTA Server` | `uota serve` |
| `micro-ota: Build Release Bundle` | `uota bundle --zip` |
| `micro-ota: RemoteIO Listen` | `uota remoteio listen` |

### Settings

| Setting | Default | Description |
|---|---|---|
| `micro-ota.uotaPath` | `uota` | Path to uota executable |
| `micro-ota.transport` | `wifi_tcp` | Default transport for fast/full/terminal |

---

## Tests

```bash
# All unit tests (no hardware required)
python3 -m unittest discover -s tests -p 'test_*.py'

# Individual suites
python3 -m unittest tests/test_manifest.py          # manifest builder (9)
python3 -m unittest tests/test_protocol.py          # OTA protocol with mock device (9)
python3 -m unittest tests/test_serial_transport.py  # serial transport (11)
python3 -m unittest tests/test_http_pull.py         # HTTP pull + bundle + serve (15)
python3 -m unittest tests/test_firmware.py          # firmware flash + boot_guard (14)
python3 -m unittest tests/test_security.py          # HMAC signing + verification (16)
python3 -m unittest tests/test_ble_transport.py     # BLE transport — requires bleak (25)

# Hardware-in-the-loop (ESP32 on /dev/ttyUSB0)
sg dialout -c "python3 tests/test_hardware.py"
sg dialout -c "SKIP_SERIAL=1 python3 tests/test_hardware.py"  # WiFi only
sg dialout -c "SKIP_WIFI=1   python3 tests/test_hardware.py"  # serial only
```

---

## Package structure (development)

```
micro-ota/
├── examples/
│   └── basic/              ← complete starter project template
│       ├── ota.json
│       ├── main.py
│       └── boot.py
├── packages/
│   ├── cli/                ← pip package source
│   │   ├── pyproject.toml
│   │   └── uota/
│   │       ├── cli.py      ← entry point (uota command)
│   │       ├── manifest.py ← build + sign + verify manifests
│   │       ├── bootstrap.py
│   │       ├── firmware.py
│   │       ├── serve.py
│   │       ├── bundle.py
│   │       ├── remoteio.py
│   │       ├── transports/ ← host-side transports
│   │       │   ├── wifi_tcp.py
│   │       │   ├── serial.py
│   │       │   └── ble.py
│   │       └── _device/    ← bundled MicroPython files (uploaded by bootstrap)
│   │           ├── ota.py
│   │           ├── boot_guard.py
│   │           ├── remoteio.py
│   │           └── transports/
│   │               ├── wifi_tcp.py
│   │               ├── serial.py
│   │               ├── ble.py
│   │               └── http_pull.py
│   └── vscode/             ← VS Code extension source
│       ├── package.json
│       ├── tsconfig.json
│       └── src/extension.ts
├── scripts/
│   └── build.sh            ← builds pip wheel + VS Code .vsix into dist/
└── tests/
```
