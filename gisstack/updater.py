"""Update check: is there a newer GitHub release, what changed, and fetch / install it.

Only talks to api.github.com and the release assets of this repository, and only when the user asks
(nothing is checked in the background).  Downloads are verified against the release's SHA256SUMS."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator

from . import __version__

REPO = "GIS-GEORGIA/gis-stack"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_PREFIX = f"https://github.com/{REPO}/releases/download/"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
MAX_ASSET_BYTES = 200 * 1024 * 1024


class UpdateError(RuntimeError):
    pass


def parse_version(s: str) -> tuple[int, ...]:
    """'v0.1.10' -> (0, 1, 10).  Anything that is not digits-and-dots after an optional 'v' is rejected."""
    m = re.match(r"^v?(\d+(?:\.\d+){0,3})$", str(s).strip())
    if not m:
        raise UpdateError(f"unrecognised version: {s!r}")
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(latest: str, current: str = __version__) -> bool:
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _get(url: str, opener: Callable[..., Any] | None = None, timeout: int = 10, accept: str = "application/vnd.github+json") -> Any:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"gisstack/{__version__}"})
    try:
        return (opener or urllib.request.urlopen)(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("ჯერ არცერთი გამოშვება (release) არ არსებობს") from e
        if e.code in (403, 429):
            raise UpdateError("GitHub-მა დროებით შეზღუდა მოთხოვნები (rate limit); სცადე მოგვიანებით") from e
        raise UpdateError(f"GitHub: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError(f"ვერ დავუკავშირდი GitHub-ს: {getattr(e, 'reason', e)}") from e


def install_kind() -> str:
    """How this copy was installed: venv (install.sh), package (deb/rpm), source (git checkout) or unknown."""
    pkg = Path(__file__).resolve().parent
    if (pkg.parent / ".git").exists():
        return "source"
    if str(pkg).startswith("/usr/lib/gisstack"):
        return "package"
    if sys.prefix != sys.base_prefix and (Path(sys.prefix) / "bin" / "pip").exists():
        return "venv"
    return "unknown"


def fetch_latest(opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Latest published (non-draft, non-prerelease) release of the repository."""
    with _get(API_LATEST, opener) as r:
        data = json.loads(r.read().decode("utf-8"))
    tag = str(data.get("tag_name", ""))
    parse_version(tag)       # raises if the tag is not a plain version
    return {
        "version": ".".join(str(x) for x in parse_version(tag)),
        "tag": tag,
        "name": data.get("name") or tag,
        "notes": (data.get("body") or "").strip(),
        "url": data.get("html_url") or RELEASES_PAGE,
        "published": (data.get("published_at") or "")[:10],
        "assets": [{"name": a["name"], "url": a["browser_download_url"], "size": a.get("size", 0)}
                   for a in data.get("assets", []) if str(a.get("browser_download_url", "")).startswith(ASSET_PREFIX)],
    }


def _pick(assets: list[dict[str, Any]], pattern: str) -> dict[str, Any] | None:
    return next((a for a in assets if re.search(pattern, a["name"])), None)


def _os_family() -> str:
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return ""
    ids = " ".join(re.findall(r"^(?:ID|ID_LIKE)=(.*)$", text, re.M)).replace('"', "")
    if re.search(r"debian|ubuntu", ids):
        return "deb"
    if re.search(r"rhel|fedora|centos|rocky|alma", ids):
        return "rpm"
    return ""


def check(opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Compare with the latest release.  Never raises for 'no newer version'; raises UpdateError on network errors."""
    latest = fetch_latest(opener)
    kind = install_kind()
    assets = latest["assets"]
    sdist = _pick(assets, r"\.tar\.gz$")
    pkg = _pick(assets, r"\.deb$" if _os_family() == "deb" else r"\.rpm$") if _os_family() else None
    return {
        "current": __version__, "latest": latest["version"], "newer": is_newer(latest["version"]),
        "name": latest["name"], "notes": latest["notes"], "url": latest["url"], "published": latest["published"],
        "install_kind": kind, "assets": assets,
        "sdist": sdist, "package": pkg, "sums": _pick(assets, r"^SHA256SUMS$"),
        # in-place upgrade is only offered where it is safe: our own virtualenv from install.sh
        "can_auto": kind == "venv" and sdist is not None and _pick(assets, r"^SHA256SUMS$") is not None,
    }


def _expected_hash(sums_text: str, name: str) -> str:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    raise UpdateError(f"{name}: SHA256SUMS-ში ჰეში არ არის — ფაილს არ ვენდობით")


def download(asset: dict[str, Any], sums_asset: dict[str, Any] | None, dest_dir: str | Path,
             opener: Callable[..., Any] | None = None) -> Path:
    """Fetch a release asset into dest_dir and verify its SHA-256.  Returns the file path."""
    for a in (asset, sums_asset):
        if a is not None and not str(a["url"]).startswith(ASSET_PREFIX):
            raise UpdateError("არასანდო ჩამოტვირთვის მისამართი")
    if sums_asset is None:
        raise UpdateError("SHA256SUMS არ არის ამ გამოშვებაში — ჩამოტვირთვა გაუქმდა")
    with _get(sums_asset["url"], opener, accept="application/octet-stream") as r:
        want = _expected_hash(r.read(100_000).decode("utf-8", "replace"), asset["name"])
    name = Path(asset["name"]).name        # never trust a path from the network
    if name != asset["name"] or not re.fullmatch(r"[A-Za-z0-9._+-]+", name):
        raise UpdateError("არასწორი ფაილის სახელი")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / name
    h, size = hashlib.sha256(), 0
    with _get(asset["url"], opener, timeout=60, accept="application/octet-stream") as r, open(out, "wb") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ASSET_BYTES:
                out.unlink(missing_ok=True)
                raise UpdateError("ფაილი ზედმეტად დიდია")
            h.update(chunk)
            fh.write(chunk)
    if h.hexdigest() != want:
        out.unlink(missing_ok=True)
        raise UpdateError(f"{name}: SHA-256 არ ემთხვევა — ფაილი წაშლილია")
    return out


def package_hint(path: str | Path) -> str:
    p = str(path)
    if p.endswith(".deb"):
        return f"sudo apt install {p}"
    if p.endswith(".rpm"):
        return f"sudo dnf install {p}"
    return p


def apply(info: dict[str, Any], work_dir: str | Path, opener: Callable[..., Any] | None = None) -> Iterator[str]:
    """Download + verify the new release and upgrade this virtualenv in place.  Yields log lines.

    Only for installations made by install.sh (install_kind == 'venv'); everything else gets a hint."""
    if not info["newer"]:
        yield f"უკვე უახლესი ვერსიაა ({info['current']})"
        return
    if not info["can_auto"]:
        raise UpdateError({
            "package": "პაკეტით დაყენებულია — ჩამოტვირთე და დააყენე ახალი პაკეტი (gisstack update --download DIR)",
            "source": "git checkout-ია — გაუშვი: git pull",
        }.get(info["install_kind"], "ავტომატური განახლება ამ დაყენებისთვის მიუწვდომელია — ჩამოტვირთე ხელით"))
    yield f"ჩამოტვირთვა: {info['sdist']['name']}"
    path = download(info["sdist"], info["sums"], work_dir, opener)
    yield "SHA-256 ✔"
    yield f"$ {Path(sys.executable).name} -m pip install --upgrade {path.name}"
    proc = subprocess.Popen([sys.executable, "-m", "pip", "install", "--upgrade", str(path)], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace")
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line.rstrip("\n")
    if proc.wait() != 0:
        raise UpdateError("pip-მა განახლება ვერ შეასრულა")
    yield f"✔ განახლდა: {info['current']} → {info['latest']}. თუ GUI გიმუშავებს, გადატვირთე (gisstack web / desktop)."
