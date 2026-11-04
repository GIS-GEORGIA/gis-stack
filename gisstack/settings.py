"""User settings shared by the web GUI, the desktop GUI and the CLI: ~/.gisstack/settings.json."""
from __future__ import annotations

import json
import os
from typing import Any

from . import deploy, fsutil

LANGS = ("auto", "ka", "en")
THEMES = ("auto", "light", "dark")
REPO_URL = "https://github.com/GIS-GEORGIA/gis-stack"
DEFAULTS: dict[str, str] = {"lang": "auto", "theme": "auto"}


def _path():
    return deploy.home_dir() / "settings.json"


def load() -> dict[str, str]:
    out = dict(DEFAULTS)
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if data.get("lang") in LANGS:
                out["lang"] = data["lang"]
            if data.get("theme") in THEMES:
                out["theme"] = data["theme"]
    except (OSError, ValueError):
        pass
    return out


def save(lang: str | None = None, theme: str | None = None) -> dict[str, str]:
    cur = load()
    if lang is not None:
        if lang not in LANGS:
            raise ValueError(f"lang must be one of {', '.join(LANGS)}")
        cur["lang"] = lang
    if theme is not None:
        if theme not in THEMES:
            raise ValueError(f"theme must be one of {', '.join(THEMES)}")
        cur["theme"] = theme
    fsutil.write_private(_path(), json.dumps(cur, indent=2))
    return cur


def resolve_lang(value: str | None = None) -> str:
    """'ka' or 'en'.  auto = GISSTACK_LANG, else the system locale (ka_* -> ka), else English."""
    v = value or os.environ.get("GISSTACK_LANG") or load()["lang"]
    if v in ("ka", "en"):
        return v
    loc = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or "").lower()
    return "ka" if loc.startswith("ka") else "en"


def view() -> dict[str, Any]:
    s = load()
    return {**s, "lang_resolved": resolve_lang(s["lang"]), "repo": REPO_URL}
