#!/usr/bin/env bash
# Load nvm if present (needed when Node was installed via nvm)
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
# Build script for micro-ota
#
# Produces in dist/:
#   micro_ota-<version>-py3-none-any.whl   (pip-installable wheel)
#   micro_ota-<version>.tar.gz             (pip-installable sdist)
#   micro-ota-<version>.vsix               (VS Code extension)
#
# Requirements:
#   pip install build          (Python build frontend)
#   npm + @vscode/vsce         (VS Code extension packaging)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"

# ── helpers ───────────────────────────────────────────────────────────────────

ok()   { printf '  \033[32m✔\033[0m  %s\n' "$*"; }
info() { printf '  \033[34m→\033[0m  %s\n' "$*"; }
err()  { printf '  \033[31m✘\033[0m  %s\n' "$*" >&2; }

# ── setup ─────────────────────────────────────────────────────────────────────

mkdir -p "$DIST"
echo ""
echo "micro-ota build"
echo "Output: $DIST"
echo "────────────────────────────────────────"

# ── sync device lib to examples ──────────────────────────────────────────────

DEVICE_SRC="$ROOT/packages/cli/uota/_device"
EXAMPLES_LIB="$ROOT/examples/serial/lib/uota"
if [ -d "$EXAMPLES_LIB" ]; then
    rsync -a --include='*.py' --exclude='__pycache__/' "$DEVICE_SRC/" "$EXAMPLES_LIB/"
fi

# ── pip package ───────────────────────────────────────────────────────────────

echo ""
echo "[ 1/2 ] pip package"

# Ensure the build frontend is available
if ! python3 -m build --version &>/dev/null; then
    info "installing build frontend (pip install build)..."
    python3 -m pip install build -q --break-system-packages
fi

# Clean stale build artifacts before building
rm -rf "$ROOT/packages/cli/build" "$ROOT/packages/cli"/*.egg-info

# pyproject.toml references ../../README.md — stage it locally for the build
cp "$ROOT/README.md" "$ROOT/packages/cli/README.md"

python3 -m build "$ROOT/packages/cli" \
    --outdir "$DIST" \
    --wheel \
    --sdist \
    2>&1 | grep -E "(Successfully|copying|running|creating|writing|adding)" || true

# Remove staged README
rm -f "$ROOT/packages/cli/README.md"

ok "pip package built"

# ── VS Code extension ─────────────────────────────────────────────────────────

echo ""
echo "[ 2/2 ] VS Code extension"

VSCODE_DIR="$ROOT/packages/vscode"

if ! command -v npm &>/dev/null; then
    err "npm not found — skipping VS Code extension build"
    err "Install Node.js from https://nodejs.org then rerun this script"
else
    # Install npm deps if needed
    if [ ! -d "$VSCODE_DIR/node_modules" ]; then
        info "installing npm dependencies..."
        npm install --prefix "$VSCODE_DIR" --silent
    fi

    # Compile TypeScript
    info "compiling TypeScript..."
    npm run compile --prefix "$VSCODE_DIR" 2>&1 | tail -5

    # Package the extension (output directly into dist/)
    info "packaging extension..."
    cd "$VSCODE_DIR"
    npx vsce package --out "$DIST" 2>&1 | grep -E "(Packaged|WARNING|ERROR|DONE)" || true
    cd "$ROOT"

    ok "VS Code extension built"
fi

# ── summary ───────────────────────────────────────────────────────────────────

echo ""
echo "────────────────────────────────────────"
echo "Artifacts:"
ls -1 "$DIST" | while read -r f; do
    size=$(du -sh "$DIST/$f" 2>/dev/null | cut -f1)
    printf '  %-45s %s\n' "$f" "$size"
done
echo ""
