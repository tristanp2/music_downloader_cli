#!/usr/bin/env bash
# run_server.sh - launch the music-downloader webserver (uvicorn app:app).
# Portable across Linux, WSL2, and Windows git-bash. Optional $1 = port
# (default 8000). Binds 0.0.0.0 so other LAN machines can reach it - protect
# with X-Auth-Token (set env MUSICDL_SERVER_TOKEN or config/settings.conf).
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
export MUSIC_DOWNLOADER_PORT="$PORT"
echo "[music_downloader] starting webserver on http://0.0.0.0:${PORT}  (Ctrl-C to stop)"
exec "$VENV_PY" -m uvicorn app:app --host 0.0.0.0 --port "$PORT"
