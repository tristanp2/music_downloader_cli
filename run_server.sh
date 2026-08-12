#!/usr/bin/env bash
# run_server.sh - launch the music-downloader webserver (app.py).
# Portable across Linux, WSL2, and Windows git-bash. Optional $1 = port
# (default 8000). The bind interface is read from config/settings.conf
# `bind_host` (or env MUSICDL_BIND_HOST) inside app.py, not hardcoded here.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$DIR/.venv/Scripts/python.exe" ]; then
    VENV_PY="$DIR/.venv/Scripts/python.exe"
else
    VENV_PY="$DIR/.venv/bin/python"
fi
if [ ! -x "$VENV_PY" ]; then
    echo "[music_downloader] venv not found at $DIR/.venv"
    echo "Set it up once:"
    echo "  python -m venv .venv"
    echo "  source .venv/bin/activate   (or: .venv/Scripts/activate on Windows)"
    echo "  pip install -r requirements.txt"
    exit 1
fi
PORT="${1:-8000}"
export MUSICDL_PORT="$PORT"
echo "[music_downloader] starting webserver (port $PORT, bind host from config/settings.conf bind_host)  (Ctrl-C to stop)"
exec "$VENV_PY" app.py
