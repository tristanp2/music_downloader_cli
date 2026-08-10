#!/usr/bin/env python3
"""
spotify_to_flac.py  --  thin CLI entrypoint for the core pipeline.

All logic lives in core/ so app.py (FastAPI) and sync.py (cron) can call the
same code. This file only:
  1. Boots the Deezer session + deemix settings.
  2. Loops: prompt for a Spotify URL, call core.run_playlist(), print result.
"""
import sys
from pathlib import Path

# ensure <repo>/ is on sys.path so `import core` works when run as a script
REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.config import resolve_output_dir, sync_deezer_arl, ARL, die
from core.deezer import init_deezer
from core.downloader import run_playlist
from core import server_lock


def main():
    if not ARL.is_file():
        die("deezer.arl missing (Deezer HiFi session token).")

    arl_text = ARL.read_text(encoding="utf-8").strip()
    sync_deezer_arl()  # single source of truth -> config/.arl for deemix
    WORK_DIR = resolve_output_dir()
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    # Build a local Deezer session for the fallback case (only used when the
    # web server is NOT running). When the server IS up, cli_dispatch posts to
    # it instead and this session is never touched.
    dz = init_deezer(arl_text)
    settings = __import__("deemix.settings", fromlist=["load"]).load(REPO / "config")

    print("=== Spotify -> FLAC (Deezer) ===")
    print("Paste a Spotify playlist/album URL. 'quit'/'exit'/Enter/Ctrl-C to stop.\n")

    while True:
        try:
            url = input("spotify url> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            break
        if not url:
            print("bye.")
            break
        if url.lower() in ("quit", "exit", "q"):
            print("bye.")
            break

        result = server_lock.cli_dispatch(url, dz=dz, settings=settings, work_dir=WORK_DIR)
        if not result.get("ok"):
            print(f"[skip] {result.get('error', 'unknown error')}")
            continue
        print(f"  downloaded={result['downloaded']}  skipped={result['skipped']}  "
              f"missed={result['missed']}  failed={result['failed']}")


if __name__ == "__main__":
    main()
