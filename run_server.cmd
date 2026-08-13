@echo off
rem run_server.cmd - launch the music-downloader webserver on Windows.
rem Resolves its own directory, uses the project venv, and starts app.py.
rem Optional first arg = port (default 8000). The bind interface is read from
rem config/settings.conf `bind_host` (or env MUSIC_DOWNLOADER_BIND_HOST) inside app.py,
rem not hardcoded here - so LAN-only vs all-interfaces is controlled by config.
rem Requires the venv to exist:
rem   python -m venv .venv
rem   .venv\Scripts\activate
rem   pip install -r requirements.txt

set "DIR=%~dp0"
set "VENV_PY=%DIR%.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [music_downloader] venv not found at .venv\Scripts\python.exe
    echo Set it up once:
    echo   python -m venv .venv
    echo   .venv\Scripts\activate
    echo   pip install -r requirements.txt
    exit /b 1
)

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"
set "MUSIC_DOWNLOADER_PORT=%PORT%"

echo [music_downloader] starting webserver (port %PORT%, bind host from config/settings.conf bind_host)  (Ctrl-C to stop)
"%VENV_PY%" app.py
