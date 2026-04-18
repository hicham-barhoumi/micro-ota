#!/usr/bin/env bash
# micro-ota serial example — test script
#
# Usage:
#   ./test.sh bootstrap [PORT]   # first-time: install uota library on device
#   ./test.sh push      [PORT]   # push main.py via OTA
#   ./test.sh full      [PORT]   # push all managed files via OTA
#
# PORT defaults to auto-detection (first ESP32-looking USB serial device).
# Examples:
#   ./test.sh bootstrap /dev/ttyUSB0
#   ./test.sh push
#
# After bootstrap the device resets and starts the OTA server.
# Open a serial monitor (e.g. screen /dev/ttyUSB0 115200) in a second
# terminal to watch boot and OTA progress.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CLI_PKG="$REPO_ROOT/packages/cli"

# ── helpers ───────────────────────────────────────────────────────────────────

ok()   { printf '\033[32m✔\033[0m  %s\n' "$*"; }
info() { printf '\033[34m→\033[0m  %s\n' "$*"; }
err()  { printf '\033[31m✘\033[0m  %s\n' "$*" >&2; exit 1; }

# ── ensure uota CLI is available ──────────────────────────────────────────────

if ! python3 -m uota --version &>/dev/null 2>&1; then
    if ! command -v uota &>/dev/null; then
        info "uota not found — installing from local package..."
        pip install -e "$CLI_PKG" --quiet --break-system-packages \
            || pip install -e "$CLI_PKG" --quiet
        ok "uota installed"
    fi
fi

# Prefer running as a module so we always use the repo source, not a stale
# installed version.
UOTA="python3 -m uota"
if ! $UOTA --help &>/dev/null 2>&1; then
    UOTA="uota"
fi

# ── parse args ────────────────────────────────────────────────────────────────

CMD="${1:-}"
PORT="${2:-}"

[ -z "$CMD" ] && {
    echo "Usage: $0 {bootstrap|push|full} [PORT]"
    echo ""
    echo "  bootstrap   First-time install of the uota library on the device."
    echo "              Resets the device when done."
    echo ""
    echo "  push        Stream-push fastOtaFiles (main.py) to the device."
    echo "              Use after bootstrap or any previous push."
    echo ""
    echo "  full        Push all managed files (lib/, *.py, *.json) to the device."
    echo ""
    exit 0
}

PORT_ARG=""
[ -n "$PORT" ] && PORT_ARG="--port $PORT"

# ── run from the example directory so ota.json is picked up ──────────────────

cd "$SCRIPT_DIR"

case "$CMD" in
    bootstrap)
        info "Bootstrapping device${PORT:+ on $PORT}..."
        info "(uploads /lib/uota/ and boot.py then resets the device)"
        $UOTA bootstrap $PORT_ARG
        echo ""
        ok "Bootstrap done — device is resetting."
        echo ""
        echo "   Wait ~5 s for the device to boot, then run:"
        echo "     $0 push${PORT:+ $PORT}"
        ;;

    push)
        info "Pushing main.py${PORT:+ via $PORT}..."
        $UOTA fast --transport serial $PORT_ARG
        echo ""
        ok "Push done — device will reset and run the new main.py."
        ;;

    full)
        info "Full OTA push${PORT:+ via $PORT}..."
        $UOTA full --transport serial $PORT_ARG
        echo ""
        ok "Full push done."
        ;;

    *)
        err "Unknown command: $CMD  (use bootstrap | push | full)"
        ;;
esac
