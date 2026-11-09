"""Backup format, component table, ids, errors and archive safety checks shared by every provider (see docs/BACKUP-RESTORE-ARCHITECTURE.md)."""
from __future__ import annotations

import re
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from ..core.errors import GisStackError

SCHEMA = 1
ID_RE = re.compile(r"^(pre-)?\d{8}T\d{6}Z-[0-9a-f]{4}$")
KINDS = ("manual", "scheduled", "pre-restore")
MAX_NOTE = 200


class BackupError(GisStackError):
    code = "backup"


class IntegrityError(BackupError):
    code = "integrity"


class RestoreError(BackupError):
    code = "restore"


class BackupNotFound(BackupError):
    code = "not_found"


# component -> how it is captured.  `workload` keys targets.WORKLOADS; `path` is inside the container; `restorable_on` lists the targets
# where the path is a writable volume (configuration bind mounts are read-only on Compose: restoring those is a redeploy).
COMPONENTS: dict[str, dict[str, Any]] = {
    "postgres": {"workload": "postgis", "file": "postgres.dump", "kind": "database", "restorable_on": ("compose", "podman", "k8s"), "optional": False},
    "geoserver": {"workload": "geoserver", "file": "geoserver.tar.gz", "kind": "files", "path": "/opt/geoserver_data", "marker": "global.xml",
                  "restorable_on": ("compose", "podman", "k8s"), "optional": False},
    "caddy": {"workload": "caddy", "file": "caddy.tar.gz", "kind": "files", "path": "/data", "marker": None,
              "restorable_on": ("compose", "podman", "k8s"), "optional": False},
    "qgis": {"workload": "qgis_server", "file": "qgis.tar.gz", "kind": "files", "path": "/etc/qgisserver", "marker": None,
             "restorable_on": ("k8s",), "optional": False},
    "mapproxy_config": {"workload": "mapproxy", "file": "mapproxy-config.tar.gz", "kind": "files", "path": "/etc/mapproxy", "marker": None,
                        "restorable_on": (), "optional": False},
    "mapproxy_cache": {"workload": "mapproxy", "file": "mapproxy-cache.tar.gz", "kind": "files", "path": "/var/lib/mapproxy", "marker": None,
                       "restorable_on": ("compose", "podman", "k8s"), "optional": True},
}
ENCRYPTED_SUFFIX = ".gpg"


def new_id(pre: bool = False) -> str:
    return ("pre-" if pre else "") + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:4]


def valid_id(bid: str) -> str:
    if not isinstance(bid, str) or not ID_RE.match(bid):
        raise BackupError("invalid backup id")
    return bid


def check_components(names: Optional[list[str]]) -> list[str]:
    out = []
    for n in names or []:
        if n not in COMPONENTS:
            raise BackupError(f"unknown component {n!r} (known: {', '.join(COMPONENTS)})")
        out.append(n)
    return out


def stored_name(component: str, encrypted: bool) -> str:
    return COMPONENTS[component]["file"] + (ENCRYPTED_SUFFIX if encrypted else "")


def check_note(note: str) -> str:
    if len(note) > MAX_NOTE or any(ord(c) < 32 for c in note):
        raise BackupError(f"note: at most {MAX_NOTE} characters, no control characters")
    return note


def scan_tar(path: Path, *, max_members: int = 2_000_000) -> dict[str, Any]:
    """Stream through a .tar.gz and refuse anything that could write outside the directory it is extracted into: absolute names, `..`,
    device/fifo members, hard links to names outside the archive, and any member stored *below a symbolic link* of the same archive (the
    classic way to make an extraction write through a link).  A symlink itself is fine - it is only ever created, never followed.
    Also proves the gzip/tar structure is readable end to end.  Returns {members, bytes, top}."""
    members = entries = size = 0
    top: set[str] = set()
    links: set[tuple[str, ...]] = set()
    try:
        with tarfile.open(path, "r|gz") as tf:
            for m in tf:
                members += 1
                if members > max_members:
                    raise IntegrityError("archive has too many members")
                parts = tuple(p for p in Path(m.name).parts if p not in (".", ""))
                if parts:
                    entries += 1
                if m.name.startswith("/") or ".." in parts:
                    raise IntegrityError(f"unsafe member name in archive: {m.name!r}")
                if m.isdev() or m.isfifo():
                    raise IntegrityError(f"device or fifo member in archive: {m.name!r}")
                if any(parts[:i] in links for i in range(1, len(parts))):
                    raise IntegrityError(f"member stored below a symbolic link: {m.name!r}")
                if m.islnk() and (m.linkname.startswith("/") or ".." in Path(m.linkname).parts):
                    raise IntegrityError(f"hard link points outside the archive: {m.name!r} -> {m.linkname!r}")
                if m.issym():
                    links.add(parts)
                size += max(m.size, 0)
                if parts:
                    top.add(parts[0])
    except (tarfile.TarError, EOFError, OSError) as e:
        raise IntegrityError(f"archive is damaged: {type(e).__name__}: {e}") from e
    return {"members": entries, "bytes": size, "top": sorted(top)[:50]}
