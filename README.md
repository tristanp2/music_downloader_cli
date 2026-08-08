# music_downloader_cli

Read a Spotify playlist and download each track as lossless **FLAC** via
[Deezer](https://deezer.com) (HiFi/FLAC tier required for true lossless).

Spotify is used **only** to read the playlist. Deezer does the actual FLAC
download (via `deemix`). No second source  --  tracks Deezer can't match are
logged to `missed_tracks.json` for you to handle manually.

## Requirements

- Python 3.11+
- A Deezer account with the **HiFi** tier (free tier gives 128 kbps MP3 only;
  HiFi gives CD-quality FLAC). A free trial qualifies.
- A Spotify app (developer dashboard) with `client_id` / `client_secret`, plus
  a Spotify Premium account  --  used to read playlist metadata via the Web API.
- `deezer` and `deemix` Python packages (installed in the venv below).

## Install

```bash
git clone <repo-url>
cd music_downloader_cli

# create an isolated environment
python -m venv .venv

# activate it (so `python` / `pip` mean the venv)
#   macOS / Linux:
source .venv/bin/activate
#   Windows (cmd/PowerShell):
.venv\Scripts\activate

# install dependencies
pip install -r requirements.txt
```

After activating, `python` resolves to the venv interpreter, which has
`deezer` and `deemix`. That's the only reason for the venv  --  it isolates
these third-party packages from your system Python.

## Configure secrets (all gitignored, never committed)

1. **Deezer token**  --  log into Deezer in your browser, then copy the `arl`
   cookie value into `deezer.arl` (one line). The script auto-syncs it into
   `config/.arl` for deemix on each run, so this is the only Deezer file you
   edit.
2. **Spotify app creds**  --  put your Spotify developer app's
   `client_id` and `client_secret` into `config/settings.conf`
   (same file as the `output_dir` setting):
   ```ini
   spotify-id = <your client id>
   spotify-secret = <your client secret>
   ```
   The first run opens your browser to approve the app; the resulting token is
   cached in `spotify_token.json` and refreshed automatically.

## Run

```bash
# with the venv activated:
python spotify_to_flac.py

# or, without activating, call the venv interpreter directly:
#   macOS / Linux:  .venv/bin/python spotify_to_flac.py
#   Windows:        .venv\Scripts\python.exe spotify_to_flac.py
```

The script runs an interactive loop. Paste a Spotify playlist/album URL when
prompted; it downloads every track as FLAC, then waits for the next URL.

- Type `quit`, `exit`, or `q` (or Enter / Ctrl-C) to stop.
- A non-Spotify URL is rejected with a message; the loop continues.

## Output

- Downloads land in `<base>/<playlist name>/` (the folder is named after the
  playlist, not its ID).
- The output **base** directory is resolved in this order:
  1. `MUSIC_DOWNLOADER_OUT` environment variable (highest priority):
     ```bash
     MUSIC_DOWNLOADER_OUT=/data/music python spotify_to_flac.py
     ```
  2. `output_dir` in `config/settings.conf` (edit the template there):
     ```
     output_dir = D:/music/flac
     ```
  3. Default if neither is set: `~/Music/music_downloader_outputs`
- `config/settings.conf` supports `~` for home and absolute paths. It is
  gitignored (machine-specific), so each machine sets its own path.
- Tracks Deezer can't match are written to `missed_tracks.json` in the output
  directory.

## Notes

- Deezer HiFi = CD-quality 16/44.1 FLAC. This is lossless; it is not 24-bit
  Hi-Res.
- The Deezer `arl` token expires when Deezer logs you out. Re-grab the cookie
  and update `deezer.arl` to resume downloads.
- `config/config.json` holds deemix's download settings (filename template,
  etc.)  --  edit there if you want different file naming.

## .gitignore

Excludes `.venv/`, `music_downloader_outputs/`, and all secrets (`deezer.arl`,
`spotify_token.json`, `config/settings.conf`, `config/.arl`). Nothing
sensitive is committed.
