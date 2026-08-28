@echo off
REM Launcher shim for sockseek (Soulseek P2P fallback).
REM Forwards only --config + --pref-format + args. Do NOT pass -o/--output:
REM doing so silently defeats sockseek.conf's output-dir.
"%~dp0sockseek.exe" --config "%~dp0sockseek.conf" --pref-format "flac,wav" %*
