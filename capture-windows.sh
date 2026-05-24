#!/usr/bin/env bash
# capture-windows.sh
# Drop this in your repo root alongside capture-windows.py.
# Installs deps if needed, then runs the capture script.
#
# Usage: ./capture-windows.sh -- python myapp.py
#        ./capture-windows.sh --threshold 8 -- ./my_gui_app
#        ./capture-windows.sh --retake -- npx electron .

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/capture-windows.py"

# ── Dependency check ──────────────────────────────────────────────────────────

check_python_deps() {
    python3 -c "import PIL, imagehash" 2>/dev/null && return
    echo "[capture] Installing Python deps: Pillow imagehash"
    pip install --quiet Pillow imagehash
}

check_system_deps_linux() {
    local missing=()
    command -v xdotool  >/dev/null 2>&1 || missing+=("xdotool")
    command -v import   >/dev/null 2>&1 || missing+=("imagemagick")
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "[capture] Missing system tools: ${missing[*]}"
        echo "[capture] Install with: sudo apt install ${missing[*]}"
        echo ""
    fi
}

OS="$(uname -s)"
check_python_deps

if [[ "$OS" == "Linux" ]]; then
    check_system_deps_linux
elif [[ "$OS" == "Darwin" ]]; then
    python3 -c "import Quartz" 2>/dev/null || {
        echo "[capture] Installing pyobjc-framework-Quartz for macOS window detection…"
        pip install --quiet pyobjc-framework-Quartz
    }
fi

# ── Run ───────────────────────────────────────────────────────────────────────

exec python3 "$PY_SCRIPT" "$@"