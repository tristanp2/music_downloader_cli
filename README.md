# music_downloader_cli

Read a Spotify playlist and download each track as lossless **FLAC** via
[Deezer](https://deezer.com) (HiFi/FLAC tier required for true lossless).
Spotify is used **only** to read the playlist; Deezer (via `deemix`) does the
actual FLAC download.

## Requirements

- Python 3.11+
- A Deezer account with the **HiFi** tier (free tier gives 128 kbps MP3 only;
  HiFi gives CD-quality FLAC). A free trial qualifies.
- A Spotify app (developer dashboard) with `client_id` / `client_secret`, plus
  a Spotify Premium account — used to read playlist metadata via the Web API.
- `deezer-py`, `deemix`, `fastapi`, `uvicorn`, `rich` (installed in the venv).

## Install

```bash
git clone <repo-url>
cd music_downloader_cli

# create an isolated environment
python -m venv .venv

# activate it
#   macOS / Linux / WSL2:
source .venv/bin/activate
#   Windows (cmd/PowerShell):
.venv\Scripts\activate

# install dependencies
pip install -r requirements.txt
```

After activating, `python` resolves to the venv interpreter, which has all the
third-party packages.

## Configure secrets (all gitignored, never committed)

1. **Deezer token** — log into Deezer in your browser, then copy the `arl`
   cookie value into `deezer.arl` (one line). On each run the server copies it
   into `config/.arl` for deemix, so `deezer.arl` is the only Deezer file you
   edit.
2. **Spotify app creds** — put your Spotify developer app's `client_id` and
   `client_secret` into `config/settings.conf`:
   ```ini
   spotify-id = <your client id>
   spotify-secret = <your client secret>
   ```
   The first run opens your browser to approve the app; the resulting token is
   cached in `spotify_token.json` and refreshed automatically.
3. **Output directory** — add to `config/settings.conf`:
   ```ini
   output_dir = <your output dir>
   ```
   The playlist name is appended as a subfolder (see Output below).
4. **Users** — `config/settings.conf` lists who can download:
   ```ini
   users = <user1,user2,...>
   ```
   Each user gets their own subfolder under `output_dir`. Defaults to
   `user1` if omitted.
5. **Network bind** — `config/settings.conf` `bind_host` controls which
   interface the server listens on:
   ```ini
   bind_host = <host>        # all interfaces (LAN + localhost)
   ```
6. **Server token (optional)** — if set, `POST /download` requires an
   `X-Auth-Token` header matching it. GETs (the UI, job list) stay open for
   localhost/LAN use.
   ```ini
   server_token = <shared secret>
   ```

A `config/settings.template.conf` is provided — copy it to
`config/settings.conf` and fill in your values.

## Run the server

```bash
# portable launcher (picks the venv automatically; arg = port, default 8000)
./run_server.sh            # macOS / Linux / WSL2
run_server.cmd             # Windows

# or directly with the venv interpreter
.venv/Scripts/python.exe app.py        # Windows
.venv/bin/python app.py                # macOS / Linux / WSL2
```

## Output

- Downloads land in `<output_dir>/<user>/<playlist folder>/`. The folder is
  named after the playlist (not its Spotify ID). `users = user1,user2` gives
  `.../user1/...` and `.../user2/...`.
- The output **base** directory resolves in this order:
  1. `MUSIC_DOWNLOADER_OUT` environment variable (highest priority)
  2. `output_dir` in `config/settings.conf`
  3. Default if neither is set: `~/Music/music_downloader_outputs`
- **Filesystem-as-registry**: each playlist folder holds a
  `playlist.meta.json` recording the Spotify URL, name, and per-track status.
  The server reads this to list known playlists and to know what to re-check on
  sync — so the disk contents *are* the state; there's no separate database.
- Tracks Deezer can't match are written as partial/incomplete and reported in
  the job result; a completed job shows the missed count in its card.

