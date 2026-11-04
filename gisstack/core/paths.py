"""Every location gis-stack reads or writes, in one place (audit A-09).  `GISSTACK_HOME` relocates all of them."""
from __future__ import annotations

import os
from pathlib import Path

from .. import fsutil


def home() -> Path:
    """~/.gisstack (or $GISSTACK_HOME): holds secrets, tokens, backups, so it is owner-only (0700)."""
    return fsutil.private_dir(Path(os.environ.get("GISSTACK_HOME") or (Path.home() / ".gisstack")))


def _sub(name: str) -> Path:
    return home() / name


def secrets_file(project: str) -> Path:
    return _sub("secrets") / f"{project}.json"


def projects_dir() -> Path:
    return _sub("projects")


def bundles_dir() -> Path:
    return _sub("bundles")


def backups_dir() -> Path:
    return _sub("backups")


def settings_file() -> Path:
    return _sub("settings.json")


def token_file() -> Path:
    return _sub("web.token")


def audit_log() -> Path:
    return _sub("audit.log")


def journal_file() -> Path:
    return _sub("journal.jsonl")


def tokens_file() -> Path:
    return _sub("tokens.json")


def log_file() -> Path:
    return _sub("gisstack.log")


def lock_dir() -> Path:
    return _sub("locks")


def monitor_db(project: str, host: str) -> Path:
    """History of one monitored machine: ~/.gisstack/monitor/<project>-<host>.sqlite"""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in f"{project}-{host}")
    return fsutil.private_dir(_sub("monitor")) / f"{safe}.sqlite"
