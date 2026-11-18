#!/usr/bin/env bash
# AtmosHunter — Launcher
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
LOG="$SCRIPT_DIR/.launcher.log"
DEST_DIR="${HOME}/Music/DolbyAtmos"

mkdir -p "$DEST_DIR"

exec >> "$LOG" 2>&1
echo "=== $(date) === Iniciando AtmosHunter v2.8 ==="

if [ ! -f "$VENV/bin/python3" ]; then
  python3 -m venv "$VENV" --system-site-packages
  "$VENV/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
fi

cd "$SCRIPT_DIR"
exec "$VENV/bin/python3" "$SCRIPT_DIR/main.py"
