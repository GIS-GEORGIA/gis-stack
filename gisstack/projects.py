"""Saved projects: ~/.gisstack/projects/<name>.json, plus per-component saving.

A project file is a plain gisstack config (usable with `gisstack ... -c FILE`).  Saving one component
only rewrites the sections that belong to it, so the other programs' settings stay as they were."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from . import config, fsutil
from .core import paths
from .core.errors import ProjectError

# component id -> top-level config sections it owns
SECTIONS: dict[str, list[str]] = {
    "postgis": ["postgres"],
    "tomcat": ["tomcat"],
    "geoserver": ["geoserver"],
    "qgis_server": ["qgis"],
    "mapproxy": ["mapproxy"],
    "caddy": ["domain", "email", "tls"],
    "webmap": ["webmap"],
}
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


def projects_dir() -> Path:
    d = paths.projects_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(name: str) -> Path:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ProjectError("პროექტის სახელი: მხოლოდ a-z, 0-9, _ (პატარა ასოებით დაწყებული)")
    return projects_dir() / f"{name}.json"


def list_projects() -> list[str]:
    return sorted(p.stem for p in projects_dir().glob("*.json") if _NAME_RE.match(p.stem))


def load(name: str) -> dict[str, Any]:
    """The saved settings only (what the user chose to store)."""
    p = _path(name)
    if not p.is_file():
        raise ProjectError(f"პროექტი ვერ მოიძებნა: {name}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ProjectError(f"პროექტის ფაილი დაზიანებულია: {e}") from e
    if not isinstance(data, dict):
        raise ProjectError("პროექტის ფაილი დაზიანებულია")
    return data


def load_full(name: str) -> dict[str, Any]:
    """Saved settings laid over the defaults - what a GUI form shows."""
    return config.merge(config.default_config(), load(name))


def _write(name: str, data: dict[str, Any]) -> Path:
    # may contain passwords the user typed: owner-only from the first byte, replaced atomically
    return fsutil.write_private(_path(name), json.dumps(data, indent=2, ensure_ascii=False))


def _validate(data: dict[str, Any]) -> None:
    try:
        config.resolve(data, generate_secrets=False)
    except config.ConfigError as e:
        raise ProjectError(str(e)) from e


def save(cfg: dict[str, Any]) -> Path:
    """Save a whole project (cfg is the GUI form's config; cfg['project'] names the file)."""
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    _validate(data)
    return _write(data["project"], data)


def save_component(cfg: dict[str, Any], component: str) -> Path:
    """Store just one program's settings (and its on/off tick) into the project file."""
    if component not in SECTIONS:
        raise ProjectError(f"უცნობი კომპონენტი: {component}")
    name = cfg.get("project")
    if not isinstance(name, str):
        raise ProjectError("project: required")
    try:
        data = load(name)
    except ProjectError as e:
        if "ვერ მოიძებნა" not in str(e):
            raise
        data = {}
    data["project"] = name
    for sec in SECTIONS[component]:
        if sec in cfg:
            data[sec] = copy.deepcopy(cfg[sec])
    if component in (cfg.get("components") or {}):
        data.setdefault("components", {})[component] = bool(cfg["components"][component])
    _validate(data)
    return _write(name, data)
