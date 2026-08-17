#!/usr/bin/env python3
"""
cli.py  --  thin CLI entrypoint for the core pipeline.

All logic lives in core/ so app.py (FastAPI) and the CLI can call the same
code. The web server also exposes POST /sync for scheduled re-downloads. This
file only:
  1. Boots the Deezer session + deemix settings (ARL lives at config/.arl).
  2. Loops: prompt for a Spotify URL, call core.run_playlist(), print result.
"""
import sys
import argparse
from pathlib import Path

# ensure <repo>/ is on sys.path so `import core` works when run as a script
REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.config import resolve_output_dir, ARL, die, read_users
from core.deezer import init_deezer
from core.downloader import run_playlist
from core import server_lock


def main():
    ap = argparse.ArgumentParser(description="Spotify -> FLAC downloader")
    ap.add_argument("--user", required=False, default=None,
                    help="User name for playlist directory (default: first in config users list)")
    args = ap.parse_args()
    users = read_users()
    user = args.user if args.user else users[0]

    if not ARL.is_file():
        die("config/.arl missing (Deezer HiFi session token).")

    arl_text = ARL.read_text(encoding="utf-8").strip()
    WORK_DIR = resolve_output_dir() / user
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    # Build a local Deezer session for the fallback case (only used when the
    # web server is NOT running). When the server IS up, cli_dispatch posts to
    # it instead and this session is never touched.
    dz = init_deezer(arl_text)
    settings = __import__("deemix.settings", fromlist=["load"]).load(REPO / "config")

    print(f"=== Spotify -> FLAC (Deezer)  [user: {user}] ===")
    srv_port = server_lock.server_is_up()
    if srv_port is not None:
        print(f"[mode] routing through web server on port {srv_port} "
              f"(job queued; watch progress in the web UI)")
    else:
        print("[mode] local (no server running -- using core lib directly)")
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

        result = server_lock.cli_dispatch(url, dz=dz, settings=settings, work_dir=WORK_DIR, user=user)
        if not result.get("ok"):
            # server mode returns an error dict (e.g. job POST failed) without a
            # [skip] tag; local mode returns run_playlist's error dict too.
            print(f"[skip] {result.get('error', 'unknown error')}")
            continue
        if result.get("routed") == "server":
            # server accepted the job; real progress lives in the web UI
            # (GET /jobs/{job_id}). The CLI just confirms dispatch.
            print(f"  [server:{result.get('port')}] job {result.get('job_id')} queued "
                  f"-- watch progress in the web UI")
        else:
            print(f"  downloaded={result['downloaded']}  skipped={result['skipped']}  "
                  f"missed={result['missed']}  failed={result['failed']}")


if __name__ == "__main__":
    main()
