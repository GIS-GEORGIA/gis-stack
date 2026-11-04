"""Optional system dependencies, installed only after the user agrees (today: Tkinter for the desktop GUI)."""
from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
from typing import Callable

from . import i18n, settings


def _t(text: str) -> str:
    return i18n.tr(text, settings.resolve_lang())


def tk_available() -> bool:
    importlib.invalidate_caches()
    try:
        importlib.import_module("tkinter")
        return True
    except ImportError:
        return False


def _family() -> str:
    try:
        text = open("/etc/os-release", encoding="utf-8").read()
    except OSError:
        return ""
    ids = " ".join(re.findall(r"^(?:ID|ID_LIKE)=(.*)$", text, re.M)).replace('"', "").lower()
    if re.search(r"debian|ubuntu", ids):
        return "deb"
    if re.search(r"rhel|fedora|centos|rocky|alma", ids):
        return "rpm"
    if re.search(r"suse", ids):
        return "suse"
    if re.search(r"arch", ids):
        return "arch"
    return ""


def tk_package() -> tuple[str, list[str]] | None:
    """(package name, install command without sudo) for this distribution, or None if unknown."""
    fam = _family()
    return {
        "deb": ("python3-tk", ["apt-get", "install", "-y", "python3-tk"]),
        "rpm": ("python3-tkinter", ["dnf", "install", "-y", "python3-tkinter"]),
        "suse": ("python3-tk", ["zypper", "--non-interactive", "install", "python3-tk"]),
        "arch": ("tk", ["pacman", "-S", "--noconfirm", "tk"]),
    }.get(fam)


def ensure_tk(ask: Callable[[str], str] = input, run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
              interactive: bool | None = None, out: Callable[[str], None] = print) -> bool:
    """True when Tkinter can be imported.  If it is missing, say what is needed and install it only after a yes."""
    if tk_available():
        return True
    pkg = tk_package()
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if pkg is None:
        out(_t("ბიბლიოთეკა Tkinter არ არის დაყენებული (Desktop GUI-ს სჭირდება). დააყენე შენი სისტემის პაკეტით (Python-ის tkinter) და გაუშვი თავიდან."))
        return False
    name, cmd = pkg
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    full = cmd if is_root else ["sudo"] + cmd
    shown = " ".join(full)
    out(_t("Desktop GUI-ს სჭირდება Tkinter (პაკეტი") + f" {name}). " + _t("ბრძანება:") + f" {shown}")
    if not is_root and not shutil.which("sudo"):
        out(_t("sudo ვერ მოიძებნა — გაუშვი ბრძანება root-ით და შემდეგ გაუშვი gisstack desktop."))
        return False
    if not interactive:
        out(_t("დაყენება არ ხდება: მოთხოვნა ტერმინალში არ არის. გაუშვი ზემოთ მოცემული ბრძანება."))
        return False
    try:
        answer = ask(_t("დავაყენო ახლავე? [y/N] ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in ("y", "yes", "კი", "დიახ"):
        out(_t("გაუქმდა. Desktop GUI-ის გარეშე გამოიყენე ვებ GUI: gisstack web"))
        return False
    r = run(full)
    if r.returncode != 0:
        out(_t("დაყენება ვერ მოხერხდა."))
        return False
    if not tk_available():
        out(_t("პაკეტი დაყენდა, მაგრამ Python მას ვერ ხედავს (ვირტუალური გარემო system-site-packages-ის გარეშე?). გაუშვი თავიდან ./install.sh --tk"))
        return False
    out(_t("✔ Tkinter დაყენებულია"))
    return True
