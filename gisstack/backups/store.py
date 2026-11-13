"""The local backup store: layout, atomic sealing, manifest + checksums, verification, retention and the restore state file.

One directory per backup below `<root>/<id>/`; everything owner-only.  A backup becomes visible only when `seal` renames its `.partial`
directory, after the manifest and SHA256SUMS were written and the structure was re-checked."""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from .. import fsutil
from ..core import paths
from . import model
from .model import BackupError, BackupNotFound, IntegrityError

CHUNK = 1024 * 1024
MANIFEST = "manifest.json"
SUMS = "SHA256SUMS"
LOG = "operation.log"
STATE = "restore-state.json"


def default_root(project: str, kind: str) -> Path:
    return paths.home() / "stack-backups" / f"{project}-{kind}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class Store:
    def __init__(self, root: Optional[Path] = None, *, project: str = "", kind: str = "") -> None:
        self.root = Path(root) if root else default_root(project, kind)

    # ------------------------------------------------------------------ paths
    def ensure(self) -> Path:
        return fsutil.private_dir(self.root)

    def path(self, bid: str) -> Path:
        return self.root / model.valid_id(bid)

    def partial(self, bid: str) -> Path:
        return self.root / (model.valid_id(bid) + ".partial")

    def begin(self, bid: str) -> Path:
        self.ensure()
        p = self.partial(bid)
        if p.exists() or self.path(bid).exists():
            raise BackupError(f"backup {bid} already exists")
        p.mkdir(mode=0o700)
        os.chmod(p, 0o700)
        return p

    # ------------------------------------------------------------------ sealing
    def seal(self, bid: str, manifest: dict[str, Any]) -> Path:
        """Write manifest + SHA256SUMS for the files in the partial directory, check them against disk, and rename into place."""
        p = self.partial(bid)
        names = sorted(f["name"] for c in manifest["components"].values() for f in c["files"])
        for n in names:
            if not (p / n).is_file():
                raise IntegrityError(f"missing file {n}")
        for n in names:
            os.chmod(p / n, 0o600)                                    # whatever created the payload, a sealed backup is owner-only
        fsutil.write_private(p / MANIFEST, json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        lines = [f"{sha256_file(p / n)}  {n}" for n in [*names, MANIFEST]]
        fsutil.write_private(p / SUMS, "\n".join(lines) + "\n")
        problems = self.check_dir(p)["problems"]
        if problems:
            raise IntegrityError("; ".join(problems))
        final = self.path(bid)
        os.replace(p, final)
        return final

    def fail(self, bid: str, reason: str) -> None:
        """Keep what a failed backup produced for inspection, but never as a backup (`<id>.FAILED`)."""
        p = self.partial(bid)
        if p.exists():
            dest = self.root / (bid + ".FAILED")
            shutil.rmtree(dest, ignore_errors=True)
            with_reason = p / "FAILED.txt"
            fsutil.write_private(with_reason, reason[:500] + "\n")
            os.replace(p, dest)

    # ------------------------------------------------------------------ reading
    def read(self, bid: str) -> dict[str, Any]:
        mf = self.path(bid) / MANIFEST
        if not mf.is_file():
            raise BackupNotFound(f"no backup {bid}")
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise IntegrityError(f"manifest of {bid} is damaged: {e}") from e
        if not isinstance(m, dict) or m.get("schema") != model.SCHEMA or not isinstance(m.get("components"), dict):
            raise IntegrityError(f"manifest of {bid}: unsupported schema {m.get('schema') if isinstance(m, dict) else '?'}")
        return m

    def list(self) -> list[dict[str, Any]]:
        """Newest first.  Entries that are not intact backups (.partial / .FAILED / damaged manifest) are listed with their status, never as ok."""
        if not self.root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir():
                continue
            name = d.name
            if name.endswith(".partial") or name.endswith(".FAILED"):
                bid, status = name.rsplit(".", 1)
                rows.append({"id": bid, "status": status.lower(), "kind": "", "created": "", "bytes": 0, "components": [], "encrypted": False, "note": ""})
                continue
            if not model.ID_RE.match(name):
                continue
            try:
                m = self.read(name)
            except BackupError:
                rows.append({"id": name, "status": "damaged", "kind": "", "created": "", "bytes": 0, "components": [], "encrypted": False, "note": ""})
                continue
            rows.append({"id": name, "status": "ok", "kind": m.get("kind", ""), "created": m.get("created", ""), "components": sorted(m["components"]),
                         "bytes": sum(f["bytes"] for c in m["components"].values() for f in c["files"]), "encrypted": bool(m.get("encrypted")),
                         "note": m.get("note", "")})
        return sorted(rows, key=lambda r: r["id"].removeprefix("pre-"), reverse=True)

    # ------------------------------------------------------------------ verification (no key needed: checksums cover the stored bytes)
    def check_dir(self, d: Path, *, structure: bool = False) -> dict[str, Any]:
        problems: list[str] = []
        checked = 0
        sums_file, mf = d / SUMS, d / MANIFEST
        if not mf.is_file():
            return {"ok": False, "problems": ["manifest.json is missing"], "checked": 0}
        if not sums_file.is_file():
            return {"ok": False, "problems": ["SHA256SUMS is missing"], "checked": 0}
        sums: dict[str, str] = {}
        for line in sums_file.read_text(encoding="utf-8").splitlines():
            digest, _, name = line.partition("  ")
            if len(digest) != 64 or not name or "/" in name or name.startswith("."):
                problems.append(f"SHA256SUMS: bad line {line[:60]!r}")
                continue
            sums[name] = digest
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
            files = {f["name"]: f for c in manifest["components"].values() for f in c["files"]}
        except (OSError, ValueError, KeyError, TypeError) as e:
            return {"ok": False, "problems": [f"manifest.json is unreadable: {e}"], "checked": 0}
        expected = set(files) | {MANIFEST}
        present = {p.name for p in d.iterdir() if p.is_file()} - {SUMS, LOG, "FAILED.txt"}
        for n in sorted(expected - present):
            problems.append(f"missing file: {n}")
        for n in sorted(present - expected):
            problems.append(f"unexpected file: {n}")
        if set(sums) != expected:
            problems.append("SHA256SUMS does not list exactly the files of the manifest")
        for n in sorted(expected & present):
            checked += 1
            actual = sha256_file(d / n)
            if sums.get(n) != actual:
                problems.append(f"checksum mismatch: {n}")
            if n != MANIFEST and files[n]["sha256"] != actual:
                problems.append(f"manifest checksum mismatch: {n}")
            if n != MANIFEST and files[n]["bytes"] != (d / n).stat().st_size:
                problems.append(f"size mismatch: {n}")
        if structure and not problems and not manifest.get("encrypted"):
            for n in sorted(files):
                if n.endswith(".tar.gz"):
                    try:
                        model.scan_tar(d / n)
                    except IntegrityError as e:
                        problems.append(f"{n}: {e}")
        return {"ok": not problems, "problems": problems, "checked": checked}

    def verify(self, bid: str, *, structure: bool = True) -> dict[str, Any]:
        d = self.path(bid)
        if not d.is_dir():
            raise BackupNotFound(f"no backup {bid}")
        rep = self.check_dir(d, structure=structure)
        rep["id"] = bid
        return rep

    # ------------------------------------------------------------------ deletion and retention
    def delete(self, bid: str) -> None:
        """Remove one backup directory (explicit action only).  The path is resolved and must be a direct child of the store."""
        for cand in (self.path(bid), self.partial(bid), self.root / (bid + ".FAILED")):
            if cand.is_dir():
                if cand.resolve().parent != self.root.resolve():
                    raise BackupError("refusing to delete outside the backup store")
                shutil.rmtree(cand)
                return
        raise BackupNotFound(f"no backup {bid}")

    def prune(self, *, keep: int = 14, days: Optional[int] = None, keep_pre: int = 5, dry_run: bool = False, now: Optional[float] = None) -> dict[str, Any]:
        """Retention: keep the newest `keep` regular backups and `keep_pre` pre-restore backups, plus (if `days`) everything newer than that.
        The newest *intact* backup is never removed, whatever the policy says.  Leftover .partial/.FAILED directories older than a day are removed."""
        now = now or time.time()
        rows = [r for r in self.list() if r["status"] == "ok"]
        regular = [r for r in rows if r["kind"] != "pre-restore"]
        pre = [r for r in rows if r["kind"] == "pre-restore"]
        doomed: list[str] = []

        def old(r: dict[str, Any]) -> bool:
            if days is None:
                return True
            try:
                t = calendar.timegm(time.strptime(r["id"].removeprefix("pre-")[:16], "%Y%m%dT%H%M%SZ"))
            except ValueError:
                return False
            return now - t > days * 86400
        for group, n in ((regular, keep), (pre, keep_pre)):
            doomed += [r["id"] for r in group[n:] if old(r)]
        if regular and regular[0]["id"] in doomed:
            doomed.remove(regular[0]["id"])                        # never the newest intact backup
        stale: list[str] = []
        for r in self.list():
            if r["status"] in ("partial", "failed"):
                d = self.root / (r["id"] + (".partial" if r["status"] == "partial" else ".FAILED"))
                if d.is_dir() and now - d.stat().st_mtime > 86400:
                    stale.append(r["id"])
        if not dry_run:
            for bid in doomed + stale:
                self.delete(bid)
        return {"removed": doomed, "removed_leftovers": stale, "kept": [r["id"] for r in rows if r["id"] not in doomed], "dry_run": dry_run}

    # ------------------------------------------------------------------ restore state (crash recovery)
    def state_path(self) -> Path:
        return self.root / STATE

    def read_state(self) -> Optional[dict[str, Any]]:
        p = self.state_path()
        if not p.is_file():
            return None
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"phase": "unreadable"}
        return s if isinstance(s, dict) else None

    def write_state(self, state: dict[str, Any]) -> None:
        self.ensure()
        fsutil.write_private(self.state_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")

    def clear_state(self) -> None:
        self.state_path().unlink(missing_ok=True)
