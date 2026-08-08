@echo off
rem run.cmd - launch music_downloader on Windows.
rem Resolves its own directory so it works from anywhere. Forwards all args
rem to the project venv interpreter. Requires the venv to exist
rem (python -m venv .venv && pip install -r requirements.txt).

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

"%VENV_PY%" "%DIR%spotify_to_flac.py" %*
