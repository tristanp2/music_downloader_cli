#!/usr/bin/env python3
"""
spotify_to_flac.py  --  Spotify playlist -> lossless FLAC.

Primary source:  Deezer (HiFi/FLAC via your ARL token in deezer.arl).
Fallback:       none in-script  --  tracks Deezer can't match are logged to
                missed_tracks.json so you can grab them another way.

Flow per run:
  1. Paste a Spotify playlist/album URL (loop; 'quit'/'exit'/Enter/Ctrl-C to stop).
  2. Parse the playlist via the Spotify Web API (user auth, cached token).
  3. For each track: search Deezer -> download FLAC via deemix (-b flac).
  4. Tracks Deezer can't match are logged to missed_tracks.json.

Credentials (all gitignored, never committed):
  - deezer.arl          : Deezer session token (HiFi account/trial)  --  single source of truth
  - config/.arl         : auto-synced from deezer.arl at startup (deemix portable mode)
  - config/settings.conf : spotify-id / spotify-secret (Spotify dev app, Premium) + output_dir
  - spotify_token.json  : cached Spotify user token (auto-created on first auth)
Secrets are read from those files; nothing is hardcoded here.
"""
import os
import sys
import re
import json
import subprocess
import urllib.parse
import urllib.request
import webbrowser
import http.server
import threading
import time
from pathlib import Path

# ---- paths -----------------------------------------------------------------
REPO = Path(__file__).resolve().parent
DEFAULT_WORK_DIR = Path.home() / "Music" / "music_downloader_outputs"
CONF_SETTINGS = REPO / "config" / "settings.conf"
ARL = REPO / "deezer.arl"
DEEMIX_ARL = REPO / "config" / ".arl"
SPOTIFY_TOKEN_CACHE = REPO / "spotify_token.json"
SPOTIFY_REDIRECT = "http://127.0.0.1:48721/callback"

# ---- helpers ---------------------------------------------------------------
def die(msg):
    print(f"[fatal] {msg}", file=sys.stderr)
    sys.exit(1)

def read_conf(path):
    """Parse a simple 'key = value' conf into a dict."""
    d = {}
    if not path.is_file():
        return d
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip()
    return d

# ---- Spotify Web API (user-auth, cached token) -----------------------------
def _spotify_api_get(token, url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def _exchange_code_for_token(client_id, client_secret, code):
    """Exchange an auth-code for access + refresh tokens via the redirect URI."""
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SPOTIFY_REDIRECT,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def _refresh_token(client_id, client_secret, refresh_token):
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def sync_deezer_arl():
    """Make deezer.arl the single source of truth: copy it into config/.arl
    (deemix portable mode reads that). Keeps you from editing two files."""
    if not ARL.is_file():
        return
    try:
        DEEMIX_ARL.parent.mkdir(parents=True, exist_ok=True)
        DEEMIX_ARL.write_text(ARL.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass  # deemix will just use whatever is there

def get_spotify_token():
    """Return a valid Spotify access token, using a cached/refreshed token when
    possible. On first run (no cache) it performs the auth-code flow: opens your
    browser to approve the app, runs a local server on 127.0.0.1:48721 to catch
    the callback, then exchanges the code for tokens and caches them."""
    conf = read_conf(CONF_SETTINGS)
    client_id = conf.get("spotify-id")
    client_secret = conf.get("spotify-secret")
    if not (client_id and client_secret):
        die("config/settings.conf missing spotify-id / spotify-secret (Spotify dev app).")

    # 1. cached + still valid?
    if SPOTIFY_TOKEN_CACHE.is_file():
        try:
            tok = json.loads(SPOTIFY_TOKEN_CACHE.read_text(encoding="utf-8"))
            if tok.get("expires_at", 0) > time.time() + 60:
                return tok["access_token"]
            # 2. try refresh
            if tok.get("refresh_token"):
                new = _refresh_token(client_id, client_secret, tok["refresh_token"])
                new["refresh_token"] = tok["refresh_token"]  # Spotify may omit it
                new["expires_at"] = int(time.time()) + new.get("expires_in", 3600)
                SPOTIFY_TOKEN_CACHE.write_text(json.dumps(new), encoding="utf-8")
                return new["access_token"]
        except Exception:
            pass  # fall through to full auth

    # 3. full auth-code flow
    scope = "playlist-read-private playlist-read-collaborative"
    auth_url = ("https://accounts.spotify.com/authorize?" +
                urllib.parse.urlencode({
                    "client_id": client_id,
                    "response_type": "code",
                    "redirect_uri": SPOTIFY_REDIRECT,
                    "scope": scope,
                    "show_dialog": "false",
                }))
    received = {}
    from http.server import BaseHTTPRequestHandler
    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(q)
            if "code" in params:
                received["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Spotify auth complete. You can close this tab.")
            else:
                self.send_response(400)
                self.end_headers()
            received["done"] = True
        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 48721), _CallbackHandler)
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    print("[*] Opening Spotify approval page in your browser...")
    print(f"[*] If no browser opened, paste this into your browser:\n    {auth_url}\n")
    webbrowser.open(auth_url)
    print("    Approve the app, then return here.")
    deadline = time.time() + 120
    while not received.get("done") and time.time() < deadline:
        time.sleep(0.5)
    srv.server_close()
    if "code" not in received:
        die("Spotify auth timed out or was denied (no code received on 127.0.0.1:48721).")
    tok = _exchange_code_for_token(client_id, client_secret, received["code"])
    tok["expires_at"] = int(time.time()) + tok.get("expires_in", 3600)
    SPOTIFY_TOKEN_CACHE.write_text(json.dumps(tok), encoding="utf-8")
    return tok["access_token"]

def parse_spotify_playlist(token, playlist_id):
    """Return {name, tracks:[{name,artists}]} from a playlist (paginated).

    Spotify returns the track list in one of two shapes depending on the account:
      - classic:  pl["tracks"]["items"][i]["track"]
      - newer:    pl["items"]["items"][i]["item"]
    We handle both, and extract the track object from either the "track" or "item" key.
    """
    def extract_container(pl):
        # pick the pagination object that actually holds the track rows
        for key in ("tracks", "items"):
            node = pl.get(key)
            if isinstance(node, dict) and isinstance(node.get("items"), list):
                return node
        return None

    def track_of(item):
        # the row may carry the track under "track" or "item"
        t = item.get("track")
        if not isinstance(t, dict):
            t = item.get("item")
        return t if isinstance(t, dict) else None

    pl = _spotify_api_get(token, f"https://api.spotify.com/v1/playlists/{playlist_id}")
    name = pl.get("name", playlist_id)
    tracks = []
    page = extract_container(pl)
    while isinstance(page, dict):
        for item in page.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            t = track_of(item)
            if not t:
                continue
            tn = (t.get("name") or "").strip()
            arts = [(a.get("name") or "").strip() for a in t.get("artists", [])]
            if tn:
                tracks.append({"name": tn, "artists": arts})
        url = page.get("next")
        if not url:
            break
        page = _spotify_api_get(token, url)
    return {"name": name, "tracks": tracks}

def validate_spotify_url(url):
    """Accept only open.spotify.com playlist/album/track URLs."""
    try:
        p = urllib.parse.urlparse(url.strip())
    except Exception:
        return None
    if p.netloc not in ("open.spotify.com", "spotify.com"):
        return None
    m = re.search(r"/(playlist|album|track|show|episode)/([A-Za-z0-9]+)", p.path)
    if not m:
        return None
    return m.group(2)



def safe_folder_name(name):
    """Make a playlist name safe for use as a folder name."""
    if not name:
        return None
    # drop chars illegal on Windows; collapse whitespace
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad else c for c in name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or None

def deezer_search(arl, query):
    """Return the best Deezer track dict for a query, or None."""
    import importlib
    deezer = importlib.import_module("deezer")
    dz = deezer.Deezer()
    if not dz.login_via_arl(arl):
        return None
    try:
        res = dz.api.search(query)
    except Exception:
        return None
    data = res.get("data") or []
    return data[0] if data else None

def deemix_download(url, out_dir):
    """Download a Deezer URL as FLAC via deemix (portable config). Returns True on success."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "deemix", "--portable",
           "-b", "flac", "-p", str(out_dir), url]
    r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=300)
    return "All done" in r.stdout or "Completed download" in r.stdout

def resolve_output_dir():
    """Output base dir precedence: env MUSIC_DOWNLOADER_OUT > config/settings.conf
    `output_dir` > default ~/Music/music_downloader_outputs. `~` expands to home."""
    env = os.environ.get("MUSIC_DOWNLOADER_OUT")
    if env:
        return Path(os.path.expanduser(env)).resolve()
    cfg = read_conf(CONF_SETTINGS)
    if cfg.get("output_dir"):
        return Path(os.path.expanduser(cfg["output_dir"])).resolve()
    return DEFAULT_WORK_DIR.resolve()

def main():
    if not ARL.is_file():
        die("deezer.arl missing (Deezer HiFi session token).")

    arl_text = ARL.read_text(encoding="utf-8").strip()
    sync_deezer_arl()  # single source of truth -> config/.arl for deemix
    WORK_DIR = resolve_output_dir()
    WORK_DIR.mkdir(parents=True, exist_ok=True)

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

        pid = validate_spotify_url(url)
        if not pid:
            print("[skip] not a valid open.spotify.com playlist/album/track URL.")
            continue

        print("[*] authorizing Spotify (cached token if available)...")
        try:
            token = get_spotify_token()
        except SystemExit:
            raise
        except Exception as e:
            print(f"[error] Spotify auth failed: {e}")
            continue

        print("[*] parsing playlist...")
        try:
            parsed = parse_spotify_playlist(token, pid)
            tracks = parsed["tracks"]
            pl_name = parsed["name"]
        except Exception as e:
            print(f"[error] playlist parse failed: {e}")
            continue
        if not tracks:
            print("[warn] no tracks found (private playlist or empty).")
            continue

        print(f"[*] {len(tracks)} tracks. Downloading FLAC from Deezer...\n")
        # name the output folder after the playlist (not the ID)
        folder = safe_folder_name(pl_name) or pid
        out_dir = WORK_DIR / folder
        print(f"[*] output folder: {out_dir}\n")
        missed = []
        for i, t in enumerate(tracks, 1):
            q = f"{' '.join(t['artists'])} {t['name']}"
            print(f"[{i}/{len(tracks)}] {q}")
            hit = deezer_search(arl_text, q)
            if not hit:
                print("    [deezer] no match")
                missed.append(t)
                continue
            dz_url = hit.get("link")
            if not dz_url:
                missed.append(t)
                continue
            # download into the playlist-named subfolder
            if deemix_download(dz_url, out_dir):
                print("    [deezer] FLAC downloaded")
            else:
                print("    [deezer] download failed")
                missed.append(t)

        if missed:
            log = WORK_DIR / "missed_tracks.json"
            prev = json.loads(log.read_text()) if log.is_file() else []
            prev.extend(missed)
            log.write_text(json.dumps(prev, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"\n[!] {len(missed)} tracks unmatched by Deezer (logged to missed_tracks.json):")
            for t in missed:
                print("   -", " ".join(t["artists"]), t["name"])
        print()

if __name__ == "__main__":
    main()
