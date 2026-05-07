# micro-ota

OTA updates for MicroPython — WiFi, USB serial, and BLE — with a one-command workflow and no cloud dependency.

Push code to your ESP32 in seconds, roll back a crashed firmware automatically, stream `print()` output to your terminal in real time, and sign every update with HMAC-SHA256 so only your host can push changes.

---

## Install

**From this repository** (no PyPI account needed):

```bash
pip install https://github.com/hicham-barhoumi/micro-ota/raw/main/releases/micro_ota-1.0.0-py3-none-any.whl
pip install "https://github.com/hicham-barhoumi/micro-ota/raw/main/releases/micro_ota-1.0.0-py3-none-any.whl[ble]"
pip install "https://github.com/hicham-barhoumi/micro-ota/raw/main/releases/micro_ota-1.0.0-py3-none-any.whl[all]"
```

Or clone and install locally:

```bash
git clone https://github.com/hicham-barhoumi/micro-ota.git
pip install micro-ota/releases/micro_ota-1.0.0-py3-none-any.whl
```

**VS Code extension** — download [`releases/micro-ota-1.0.0.vsix`](releases/micro-ota-1.0.0.vsix), then:
- VS Code: `Ctrl+Shift+P` → *Extensions: Install from VSIX…*
- or: `code --install-extension releases/micro-ota-1.0.0.vsix`

---

## Quick start

```bash
mkdir myproject && cd myproject
uota init                   # creates config/ota.json, app/app.py, main.py
```

Edit `config/ota.json` — set `ssid`, `password`, `hostname` (device IP or `hostname.local` via mDNS).

```bash
uota bootstrap              # first-time: uploads OTA library to ESP32 via USB
uota info                   # show device info (MicroPython version, free mem, mpy version)
uota fast                   # push app/ and config/ to device
uota full                   # push all managed files
uota terminal               # interactive device shell
```

See `examples/serial/` for a complete starter project.

> `config/ota.json` contains WiFi credentials — add it to `.gitignore`.

---

## How it works

```
[ PC ]   uota fast              uota remoteio listen / call
            │  TCP :2018               │  TCP :2019  (or BLE NUS)
            ▼                          ▼
[ ESP32 ]  /lib/uota/ota.py  ──  /lib/uota/remoteio.py
                │             /lib/uota/boot_guard.py
                ▼
           /app/app.py  (runs in its own thread)
```

At boot, `boot.py` starts two things:

- **User app thread** — your `app.run()` in a dedicated `_thread`, running concurrently with the OTA server.
- **OTA event loop** (main thread, non-blocking) — polls all configured transports and the RemoteIO server for incoming connections; no additional threads.

The OTA event loop handles:
- **OTA server** (port 2018 or BLE OTA service) — verifies HMAC, applies files atomically, resets.
- **RemoteIO server** (port 2019 when WiFi is active; BLE NUS when BLE-only) — streams `print()` output and dispatches RPC calls.
- **HTTP pull transport** — polls a manifest URL periodically and self-updates when the version changes.
- **Boot guard** — counts consecutive unclean boots so a bad update can never permanently brick the device.

WiFi TCP, USB serial, BLE (Nordic UART Service), and HTTP pull are all supported.

---

## Device filesystem layout

After `uota bootstrap`, the device looks like this:

```
/boot.py                       ← starts app thread + OTA event loop
/main.py                       ← calls app.run()
/app/
    app.py                     ← your application
/config/
    ota.json                   ← config (ssid, hostname, otaKey …) — synced via OTA
/data/                         ← runtime data, never wiped by OTA
/lib/
    uota/
        __init__.py
        ota.py      (or .mpy)  ← OTA server
        boot_guard.py
        remoteio.py
        transports/
            wifi_tcp.py
            serial.py
            ble.py
            http_pull.py
```

`/lib` is on MicroPython's default `sys.path`.

---

## Project structure (your project after `uota init`)

```
myproject/
├── config/
│   └── ota.json            ← configuration (WiFi creds — add to .gitignore)
├── app/
│   └── app.py              ← your application
├── main.py                 ← calls app.run()
└── lib/
    └── uota/               ← OTA infrastructure (copied from package on bootstrap)
        ├── ota.py
        ├── boot_guard.py
        ├── remoteio.py
        └── transports/
```

---

## CLI reference

```
uota <command> [options]
```

| Command | Description |
|---|---|
| `init [--dir DIR] [--force]` | Initialize project — create `config/ota.json`, `app/`, `main.py` |
| `bootstrap [--port PORT] [--baud BAUD] [--mpy]` | First-time upload of OTA library via serial |
| `info [--port PORT] [--baud BAUD]` | Show device info and cache mpy bytecode version |
| `fast [--transport T]` | Push `fastOtaFiles` (app/, config/ by default) |
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

---

## `.mpy` bytecode compilation

When `mpyFiles` is configured in `ota.json` and `mpy-cross` is installed, `uota fast` and `uota full` automatically compile matching `.py` files to `.mpy` bytecode before uploading. This cuts flash usage by ~50% and speeds up import time on the device.

```bash
pip install mpy-cross        # or install a version-matched binary
uota info                    # queries device mpy version, caches to .uota_cache.json
uota full                    # compiles lib/** to .mpy, uploads .mpy files
```

Workflow:
1. `uota info` connects via serial RawREPL, queries `sys.implementation.mpy`, and caches the version in `.uota_cache.json`.
2. `uota fast` / `uota full` read the cached version and compile with `mpy-cross -b <version>`.
3. If the cache is empty or `mpy-cross` is absent, the original `.py` files are uploaded unchanged.

### HTTP pull mpy variant

`uota serve` and `uota bundle` generate a versioned mpy manifest (e.g. `manifest.mpy6.json`) alongside the standard `manifest.json`. The device-side `HttpPullTransport` tries the mpy manifest first and silently falls back:

```
Server:  manifest.json  +  manifest.mpy6.json
                                  │
Device (mpy v6):  tries manifest.mpy6.json first → uses .mpy files
Device (no mpy):  falls back to manifest.json → uses .py files
```

### `--mpy` flag (bootstrap)

```bash
uota bootstrap --mpy
```

Compiles all OTA infrastructure files to `.mpy` before the first-time upload. Requires `mpy-cross`.

---

## Performance

Measured on ESP32 (MicroPython v1.26.1, 240 MHz), comparing a bare boot against the full micro-ota stack.

### Boot time

Time from the first Python instruction in `boot.py` to the moment `app.run()` is called:

| Scenario | Time |
|---|---|
| Without micro-ota | 28 ms |
| With micro-ota — `.mpy` | **280 ms** (`.py`: 709 ms) |
| Overhead | +252 ms |

OTA import breakdown (`.py` / `.mpy`):

| Step | `.py` | `.mpy` |
|---|---|---|
| `import boot_guard` | 44 ms | 26 ms |
| `from ota import OTAUpdater` | 519 ms | 154 ms |
| `boot_guard.boot()` (JSON r/w) | 97 ms | 70 ms |
| `_thread.start_new_thread` | 2 ms | 1 ms |
| `import app` | 47 ms | 29 ms |

The dominant cost is parsing and compiling `ota.py` on every boot. Pre-compiling with `--mpy` cuts that step from 519 ms to 154 ms — **70% faster**. `boot.py` finishes quickly; your app is running in its thread while the OTA event loop initialises WiFi and starts listening in the background.

### RAM

After all OTA modules are loaded and `OTAUpdater` is instantiated (gc-collected):

| | Value |
|---|---|
| Total heap | 129 KB |
| OTA stack footprint | **1.2 KB** (0.9%) |
| App RAM budget | 109 KB (85%) |

The bytecode for `boot_guard` + `ota.py` + all transports retains only ~1.2 KB of heap at runtime — temporary compilation objects are freed immediately by the GC.

### CPU

Tight Python loop throughput (500 ms window):

| Scenario | Iterations | Overhead |
|---|---|---|
| No OTA server | 28,660 | — |
| OTA event loop idle (non-blocking poll) | 28,656 | **< 1%** |

The OTA event loop calls `try_accept()` on each transport (non-blocking `poll(0)`) and immediately yields to the next iteration when no connection is pending. Your app runs in its own thread at full Python speed.

---

## Transports

### WiFi TCP (default)

```json
{ "transports": ["wifi_tcp"], "hostname": "micropython.local", "port": 2018 }
```

```bash
uota fast
uota fast --host mydevice.local   # override hostname
```

### USB Serial

```json
{ "transports": ["serial"] }
```

```bash
uota fast --transport serial
uota info --port /dev/ttyUSB0
```

Enters the raw REPL, injects a self-contained OTA server, speaks the standard protocol over UART. No WiFi needed. Port is auto-detected from connected ESP32 devices.

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

The device advertises as a BLE peripheral. The host scans by name, connects, and speaks the standard OTA protocol over the micro-ota GATT service (UUID prefix `756F74xx`). A separate NUS service (UUID prefix `6E4000xx`) is reserved for RemoteIO so any standard NUS terminal app (nRF UART, LightBlue) can connect simultaneously.

**WiFi + BLE simultaneously** — the ESP32's single 2.4 GHz radio can run both transports at the same time only when PSRAM is present (e.g. ESP32-WROVER, Python heap > 200 KB). On standard ESP32 modules, the OTA server logs a warning and starts only the first transport listed in `transports`.

### HTTP Pull

The device polls a manifest URL on an interval and self-updates when the version changes. No host connection needed at update time.

```json
{
    "transports": ["http_pull"],
    "manifestUrl": "http://myserver.local:8080/manifest.json",
    "pullInterval": 60
}
```

```bash
uota serve          # start HTTP server on port 8080
uota bundle --zip   # or build a static bundle for any web server
```

---

## Security — HMAC-SHA256 manifest signing

Set a shared secret in `config/ota.json` on both the host and the device:

```json
{ "otaKey": "your-secret-key" }
```

The host signs every manifest with HMAC-SHA256 before sending it. The device verifies the signature and aborts with `sig_mismatch` if it is missing or incorrect — before any file is transferred.

Leave `otaKey` empty (the default) to disable signing.

**Signing payload** (deterministic, order-independent):

```
<version>
<path>:<sha256>
<path>:<sha256>
...                 (file paths sorted lexicographically)
```

---

## RemoteIO

A persistent side-channel for two things: streaming all `print()` output from the device to your terminal in real time, and calling named RPC handlers on the device from the host.

RemoteIO starts automatically as part of the OTA event loop — no extra configuration needed. When WiFi is active it listens on TCP port 2019. When the device is BLE-only it accepts connections over the BLE NUS service (UUID `6E400001`).

Requires `LWIP_MAX_SOCKETS >= 2` in the firmware (standard ESP32 builds have this).

### CLI

```bash
# Stream all device print() output live (WiFi TCP)
uota remoteio listen
uota remoteio listen --transport ble    # via BLE NUS

# Call built-in handlers (WiFi TCP by default)
uota remoteio call ping                 # → "pong"
uota remoteio call uptime_ms            # → 35712
uota remoteio call free_mem             # → 98304
uota remoteio call version              # → {"version": "1.0.0"}

# Same calls over BLE NUS (device in BLE-only mode)
uota remoteio call ping --transport ble

# Call a custom handler with arguments
uota remoteio call set_led state=true
uota remoteio call set_led state=false
```

The `listen` command blocks and prints everything the device `print()`s — useful for debugging without opening a serial terminal:

```
[12:04:01] app started
[12:04:02] sensor: 24.3 °C  61% RH
[12:04:07] sensor: 24.4 °C  61% RH
```

### Python API

**WiFi TCP** (device reachable on the network):

```python
from uota.remoteio import RemoteIOClient

with RemoteIOClient('micropython.local') as rio:
    print(rio.call('ping'))                   # 'pong'
    print(rio.call('uptime_ms'))              # 35712
    print(rio.call('free_mem'))               # 98304
    print(rio.call('set_led', state=True))    # 'ok'
```

**BLE NUS** (device in BLE-only mode, `pip install micro-ota[ble]`):

```python
from uota.remoteio import RemoteIOBLEClient

with RemoteIOBLEClient('micro-ota') as rio:  # scans by BLE name
    print(rio.call('ping'))                   # 'pong'
    print(rio.call('free_mem'))               # 98304
```

Use it in scripts for automated testing or monitoring:

```python
from uota.remoteio import RemoteIOClient
import time

with RemoteIOClient('micropython.local') as rio:
    for _ in range(10):
        data = rio.call('sensor_data')
        print(f"temp={data['temp']}  hum={data['humidity']}")
        time.sleep(2)
```

### Registering handlers on the device

```python
# app/app.py
import uota.remoteio as remoteio
from machine import Pin, ADC

led = Pin(2, Pin.OUT)
adc = ADC(Pin(34))

@remoteio.on('set_led')
def _(state=False):
    led.value(int(state))
    return 'ok'

@remoteio.on('sensor_data')
def _():
    raw = adc.read()
    volts = raw * 3.3 / 4095
    return {'raw': raw, 'volts': round(volts, 3)}

@remoteio.on('reset')
def _():
    import machine
    machine.reset()
```

Then from the host:

```bash
uota remoteio call set_led state=true
uota remoteio call sensor_data          # → {"raw": 1820, "volts": 1.468}
```

### Built-in handlers

| Name | Returns |
|---|---|
| `ping` | `"pong"` |
| `version` | installed OTA version string |
| `free_mem` | free heap bytes (int) |
| `uptime_ms` | milliseconds since boot (int) |

---

## `ota.json` reference

Location: `config/ota.json` in your project (device path: `/config/ota.json`).

```json
{
    "version":      "1.0.0",
    "hostname":     "micropython.local",
    "port":         2018,
    "remoteioPort": 2019,
    "ssid":         "MyWiFi",
    "password":     "MyPassword",
    "otaKey":       "",
    "bleName":      "micro-ota",
    "transports":   ["wifi_tcp"],
    "manifestUrl":  "",
    "pullInterval": 60,
    "excludedFiles": [".git/**", "dist/**", ".uota_cache.json"],
    "fastOtaFiles": ["app/**", "main.py", "config/**"],
    "fullOtaFiles": ["**"],
    "mpyFiles":     ["lib/**"]
}
```

| Key | Description |
|---|---|
| `version` | Version string embedded in the manifest after each OTA |
| `hostname` | Device IP or `.local` hostname for WiFi OTA and RemoteIO (e.g. `micropython.local` resolves via mDNS on physical hosts) |
| `port` | OTA server port (default `2018`) |
| `remoteioPort` | RemoteIO server port (default `2019`) |
| `ssid` / `password` | WiFi credentials (stored on device) |
| `otaKey` | HMAC-SHA256 signing key — empty disables signing |
| `bleName` | BLE advertisement name (max 20 chars) |
| `transports` | Active transports on the device |
| `manifestUrl` | HTTP pull manifest URL |
| `pullInterval` | HTTP pull poll interval in seconds |
| `excludedFiles` | Glob patterns excluded from all OTA uploads |
| `fastOtaFiles` | Files pushed by `uota fast` |
| `fullOtaFiles` | Additional files pushed by `uota full` |
| `mpyFiles` | Glob patterns compiled to `.mpy` before upload (requires `mpy-cross`) |

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
2. On single-partition firmware, prints a warning but does **not** force-reset — the device continues booting so you can still connect via serial to recover.
3. `mark_clean()` also calls `esp32.Partition.mark_app_valid_cancel_rollback()` when available.

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
| `wipe` | `ok` | Delete user files, keep `/lib`, `/data`, `/config` |
| `start_ota` | `ready` | Begin OTA session |

### OTA session

```
manifest <size>\n<json>              → ok / sig_mismatch
file <name>;<size>;<sha256>\n<bin>   → ok / sha256_mismatch
...
end_ota                              → ok  (atomic commit + reset)
abort                                → aborted  (staging discarded)
```

Files are staged in `/ota_stage/`. On `end_ota`: old files not in the new manifest are deleted (protected paths like `/lib`, `/config`, `/data` are never touched), staged files are moved atomically, version is written, device resets.

If `otaKey` is set, the device verifies the manifest signature before accepting any files.

---

## VS Code extension

Located in `packages/vscode/`. Activates automatically when `config/ota.json` is present in the workspace.

### Build

```bash
# Linux / macOS
bash scripts/build.sh

# Windows (or any platform)
python scripts/build.py
```

Both scripts produce the same artifacts in `dist/`. The Python script also accepts `--pip` or `--vscode` to build one at a time.

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
| `micro-ota: Device Info` | `uota info` |
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

`tests/test_all_transports.py` is a hardware-in-the-loop suite that covers every transport × operation combination — ping, version, ls, get, rm, stream_ota (fast), start_ota (full), wipe, reset, and RemoteIO — on WiFi TCP, BLE OTA, BLE NUS RemoteIO, and USB serial.

```bash
# Full suite (ESP32 on /dev/ttyUSB0, WiFi hotspot active, BLE dongle present)
python3 tests/test_all_transports.py

# Skip individual transports
SKIP_SERIAL=1  python3 tests/test_all_transports.py   # WiFi + BLE only
SKIP_WIFI=1    python3 tests/test_all_transports.py   # BLE + serial only
SKIP_BLE=1     python3 tests/test_all_transports.py   # WiFi + serial only

# Override auto-detected device IP (useful when mDNS is unavailable)
WIFI_HOST=192.168.1.42 python3 tests/test_all_transports.py

# Override serial port
SERIAL_PORT=/dev/ttyUSB1 python3 tests/test_all_transports.py
```

The suite handles transport switching automatically (serial raw-REPL injection is always available regardless of which wireless transport the device is currently running) and resets the BlueZ adapter between BLE phases to clear stale state.

---

## Package structure (development)

```
micro-ota/
├── examples/
│   └── serial/             ← complete starter project (serial + WiFi)
│       ├── config/
│       │   └── ota.json    ← fill in your ssid/password/hostname
│       ├── app/
│       │   └── app.py
│       ├── main.py
│       └── lib/uota/       ← synced copy of _device/ files (gitignored)
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
│       └─  src/extension.ts
├── scripts/
│   └── build.sh / build.py ← builds pip wheel + VS Code .vsix into dist/
└── tests/
```
