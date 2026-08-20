#!/usr/bin/env python3
"""Additive daily backup of the music-downloader output dir to a second drive.

Stdlib-only and portable (no project venv deps). Copies every file from the
source tree (default: the server's music_downloader_outputs) into a mirrored
tree on the backup drive.

ADDITIVE ONLY: a file on the backup that already matches the source (same
size + mtime, within a 2s tolerance) is skipped; a file missing or differing
on the backup is copied. NOTHING is ever deleted on the backup drive -- files
removed from the source simply accumulate on the backup. Run once a day
(e.g. via the Hermes cronjob that points at this script).

Source resolution precedence (mirrors core.config.resolve_output_dir):
  --source  >  env MUSIC_DOWNLOADER_OUT  >  settings.conf output_dir
  >  ~/Music/music_downloader_outputs

Backup destination precedence:
  --dest  >  env MUSIC_DOWNLOADER_BACKUP_DIR  >  settings.conf backup_dir
  >  D:/Downloads/music_backup
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = REPO_ROOT / "config" / "settings.conf"
DEFAULT_SOURCE_DIR = Path.home() / "Music" / "music_downloader_outputs"
DEFAULT_BACKUP_DIR = Path("D:/Downloads/music_backup")

# Filesystem mtime values can round across a copy; treat anything within this
# many seconds as "already copied" so re-runs don't redundantly re-copy.
MTIME_TOLERANCE_SECONDS = 2


def read_conf_file(settings_path: Path) -> dict[str, str]:
    """Parse the simple 'key = value' settings.conf (mirrors core.config)."""
    settings: dict[str, str] = {}
    if not settings_path.is_file():
        return settings

    for raw_line in settings_path.read_text(encoding="utf-8").splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue

        key, value = stripped_line.split("=", 1)
        settings[key.strip()] = value.strip()

    return settings


def resolve_source_dir(parsed_args: argparse.Namespace) -> Path:
    """Where to copy FROM, resolved in the same order the server uses."""
    if parsed_args.source:
        return Path(os.path.expanduser(parsed_args.source)).resolve()

    env_override = os.environ.get("MUSIC_DOWNLOADER_OUT")
    if env_override:
        return Path(os.path.expanduser(env_override)).resolve()

    configured_value = read_conf_file(SETTINGS_PATH).get("output_dir")
    if configured_value:
        return Path(os.path.expanduser(configured_value)).resolve()

    return DEFAULT_SOURCE_DIR.resolve()


def resolve_backup_dir(parsed_args: argparse.Namespace) -> Path:
    """Where to copy TO, resolved with the same override shape as the source."""
    if parsed_args.dest:
        return Path(os.path.expanduser(parsed_args.dest)).resolve()

    env_override = os.environ.get("MUSIC_DOWNLOADER_BACKUP_DIR")
    if env_override:
        return Path(os.path.expanduser(env_override)).resolve()

    configured_value = read_conf_file(SETTINGS_PATH).get("backup_dir")
    if configured_value:
        return Path(os.path.expanduser(configured_value)).resolve()

    return DEFAULT_BACKUP_DIR.resolve()


def file_needs_backup(source_file: Path, backup_file: Path) -> bool:
    """True when the file should be copied: missing on backup, or size/mtime differ."""
    if not backup_file.is_file():
        return True

    source_stat = source_file.stat()
    backup_stat = backup_file.stat()

    if source_stat.st_size != backup_stat.st_size:
        return True

    if abs(source_stat.st_mtime - backup_stat.st_mtime) > MTIME_TOLERANCE_SECONDS:
        return True

    return False


def run_backup(source_dir: Path, backup_dir: Path, dry_run: bool) -> tuple[int, int, int]:
    """Copy changed/new files from source to backup. Returns (copied, skipped, errors)."""
    if not dry_run:
        backup_dir.mkdir(parents=True, exist_ok=True)

    files_copied = files_skipped = files_errored = 0

    for current_dir, _subdirs, filenames in os.walk(source_dir):
        relative_dir = os.path.relpath(current_dir, source_dir)

        for filename in filenames:
            source_file = Path(current_dir) / filename
            backup_file = backup_dir / relative_dir / filename

            try:
                if file_needs_backup(source_file, backup_file):
                    if not dry_run:
                        backup_file.parent.mkdir(parents=True, exist_ok=True)
                        # copy2 preserves mtime so the next run sees a match.
                        shutil.copy2(source_file, backup_file)
                    files_copied += 1
                    if dry_run:
                        print(f"[backup] would copy: {backup_file}")
                else:
                    files_skipped += 1
            except Exception as copy_error:  # keep going; tally + report at end
                files_errored += 1
                print(f"[backup] ERROR copying {source_file}: {copy_error}", file=sys.stderr)

    return files_copied, files_skipped, files_errored


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Additive daily backup of music-downloader outputs to a 2nd drive.")
    parser.add_argument("--source", help="override source dir")
    parser.add_argument("--dest", help="override backup dir")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would copy, change nothing")
    return parser.parse_args()


def main() -> None:
    parsed_args = parse_arguments()

    source_dir = resolve_source_dir(parsed_args)
    backup_dir = resolve_backup_dir(parsed_args)

    if not source_dir.is_dir():
        print(f"[backup] ERROR: source dir not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"[backup] source: {source_dir}")
    print(f"[backup] dest:   {backup_dir}" + ("  (DRY RUN)" if parsed_args.dry_run else ""))

    files_copied, files_skipped, files_errored = run_backup(
        source_dir, backup_dir, parsed_args.dry_run)

    print(f"[backup] done: {files_copied} copied, "
          f"{files_skipped} skipped, {files_errored} errors")

    sys.exit(1 if files_errored else 0)


if __name__ == "__main__":
    main()
