#!/usr/bin/env bash
# run.sh - launch the music-downloader CLI (cli.py).
# Portable across Linux, WSL2, and Windows git-bash. Resolves its own dir and
# the project venv (bin/python on Linux/WSL2, Scripts/python.exe on Windows
# git-bash). No cygpath: MSYS auto-translates /c/... for the .exe on Windows.
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
exec "$VENV_PY" "$DIR/cli.py" "$@"
