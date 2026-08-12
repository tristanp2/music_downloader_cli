"""Spotify Web API helpers: auth (cached + refresh + interactive),
playlist parsing, and URL validation.

CLI path: get_spotify_token() does the full interactive auth-code flow.
Web path: get_spotify_token_silent() uses only cache + refresh (no browser).
"""
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
import http.server
import threading


SPOTIFY_SCOPE = "playlist-read-private playlist-read-collaborative"


from .config import read_conf, CONF_SETTINGS, SPOTIFY_TOKEN_CACHE, SPOTIFY_REDIRECT, die
from .track import Track


# ---------------------------------------------------------------------------
# low-level HTTP helpers
# ---------------------------------------------------------------------------

def _spotify_api_get(token, url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))





def _exchange_code_for_token(client_id, client_secret, code):
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


# ---------------------------------------------------------------------------
# token getters
# ---------------------------------------------------------------------------

def get_spotify_token():
    """Return a valid Spotify access token (interactive: may open a browser).

    Uses cached token when possible, refreshes when expired, falls back to a
    full auth-code flow (opens browser, listens on 127.0.0.1:48721).
    """
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
    return _do_auth_code_flow(client_id, client_secret)


def get_spotify_token_silent():
    """Return a valid Spotify access token WITHOUT user interaction.

    Uses cache first, then refresh. If both fail (no cache / refresh invalid /
    needs full re-auth), returns None. Suitable for the web server / cron where
    there is no browser available.
    """
    conf = read_conf(CONF_SETTINGS)
    client_id = conf.get("spotify-id")
    client_secret = conf.get("spotify-secret")
    if not (client_id and client_secret):
        return None

    if SPOTIFY_TOKEN_CACHE.is_file():
        try:
            tok = json.loads(SPOTIFY_TOKEN_CACHE.read_text(encoding="utf-8"))
            if tok.get("expires_at", 0) > time.time() + 60:
                return tok["access_token"]
            if tok.get("refresh_token"):
                new = _refresh_token(client_id, client_secret, tok["refresh_token"])
                new["refresh_token"] = tok["refresh_token"]
                new["expires_at"] = int(time.time()) + new.get("expires_in", 3600)
                SPOTIFY_TOKEN_CACHE.write_text(json.dumps(new), encoding="utf-8")
                return new["access_token"]
        except Exception:
            pass
    return None


def _do_auth_code_flow(client_id, client_secret):
    scope = SPOTIFY_SCOPE
    auth_url = ("https://accounts.spotify.com/authorize?" +
                urllib.parse.urlencode({
                    "client_id": client_id,
                    "response_type": "code",
                    "redirect_uri": SPOTIFY_REDIRECT,
                    "scope": scope,
                    "show_dialog": "false",
                }))
    received = {}

    class _CallbackHandler(http.server.BaseHTTPRequestHandler):
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


# ---------------------------------------------------------------------------
# playlist parsing
# ---------------------------------------------------------------------------

def parse_spotify_playlist(token, playlist_id):
    """Return {name, tracks:[{name, artists, position, spotify_uri}]}.

    Handles both the classic Spotify response shape
    (pl["tracks"]["items"][i]["track"]) and the newer shape
    (pl["items"][i]["item"]). Pagination is followed automatically.
    """
    def extract_container(pl):
        for key in ("tracks", "items"):
            node = pl.get(key)
            if isinstance(node, dict) and isinstance(node.get("items"), list):
                return node
        return None

    def track_of(item):
        t = item.get("track")
        if not isinstance(t, dict):
            t = item.get("item")
        if not isinstance(t, dict):
            return None
        tid = t.get("id")
        if tid:
            t["spotify_uri"] = f"spotify:track:{tid}"
        else:
            t["spotify_uri"] = t.get("uri")  # may be None for local/episode rows
        return t

    pl = _spotify_api_get(token, f"https://api.spotify.com/v1/playlists/{playlist_id}")
    name = pl.get("name", playlist_id)
    tracks: list[Track] = []
    position = 0
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
                position += 1
                tracks.append(Track(
                    position=position,
                    name=tn,
                    artists=arts,
                    spotify_uri=t.get("spotify_uri"),
                ))
        url = page.get("next")
        if not url:
            break
        page = _spotify_api_get(token, url)
    return {"name": name, "tracks": tracks}


def validate_spotify_url(url):
    """Accept only open.spotify.com playlist/album/track URLs. Returns the ID
    or None."""
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
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad else c for c in name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or None
