"""Backup providers: one shared implementation of create / verify / restore, three thin environment adapters.

The only things that differ between Docker Compose, Podman and Kubernetes are how a command is run inside a workload (`targets.Place`),
how a workload is restarted, and how to tell that it is running - those are the three small methods each subclass provides.  Everything
that must not drift apart (format, verification, staging, reversible swap, recovery) lives in `BackupProvider`.

Safety rules implemented here (see docs/BACKUP-RESTORE-ARCHITECTURE.md): nothing is sealed unless re-read and structurally checked;
nothing is replaced unless its replacement was staged and validated; every swap is reversible and recorded in a state file first; the
previous data is kept; secrets never reach a manifest, log line or archive."""
from __future__ import annotations

import contextlib
import copy
import os
import re
import shlex
import subprocess  # nosec B404 - runs the operator's own docker/kubectl/ssh with argument lists, never a shell string from input
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Generator, Iterator, Optional

from .. import __version__, backup_crypto, deployments, journal, ops, runner, transport
from ..core.errors import DeployError
from ..targets import Place
from . import model
from .model import COMPONENTS, BackupError, IntegrityError, RestoreError
from .store import LOG, Store, sha256_file

CHUNK = 1024 * 1024
DB_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
OPS_ID = {"postgres": "postgis", "geoserver": "geoserver", "caddy": "caddy", "qgis": "qgis_server", "mapproxy_config": "mapproxy", "mapproxy_cache": "mapproxy"}
CFG_ENABLED = {"postgres": "postgis", "geoserver": "geoserver", "caddy": "caddy", "qgis": "qgis_server", "mapproxy_config": "mapproxy", "mapproxy_cache": "mapproxy"}
DEADLINE = 6 * 3600
SENSITIVE = {"geoserver": "GeoServer's own credential hashes", "caddy": "TLS private keys", "qgis": "QGIS projects can contain database credentials"}


def pg_major(version: str) -> int:
    m = re.match(r"(\d+)", version or "")
    return int(m.group(1)) if m else 0


class BackupProvider:
    """Shared implementation.  Subclasses set `kind` and implement `place`, `restart`, `family`."""

    kind = ""
    family = ""                                       # backups are interchangeable inside a family (compose == podman)

    def __init__(self, cfg: dict[str, Any], mode: str = "local", ssh: Optional[dict[str, Any]] = None, *, store: Optional[Store] = None,
                 encryption: Optional[backup_crypto.Spec] = None, passphrase: Optional[str] = None, deadline: float = DEADLINE) -> None:
        self.cfg, self.mode, self.ssh = cfg, mode, ssh or {}
        self.store = store or Store(project=str(cfg.get("project", "stack")), kind=self.kind)
        self.encryption = encryption.validate() if encryption else None
        self.passphrase = passphrase                    # to read an encrypted backup (restore); never written anywhere
        self.deadline = deadline
        self.mask = runner.redactor(runner.secrets_of(cfg) + ([passphrase] if passphrase else []) + ([encryption.passphrase] if encryption and encryption.passphrase else []))
        self.paths = {n: c.get("path", "") for n, c in COMPONENTS.items()}      # overridable (tests stand in a local directory for a volume)
        self.result: dict[str, Any] = {}
        self._log: Optional[Path] = None

    # ------------------------------------------------------------------ adapter (the only provider-specific part)
    def place(self, component: str) -> Place:
        raise NotImplementedError

    def restart(self, component: str) -> None:
        raise NotImplementedError

    def host_script(self, component: str, body: str) -> str:
        """A bash script for the host side (docker/kubectl) with the workload's variables defined."""
        return self.place(component).prelude() + body

    # ------------------------------------------------------------------ running things inside workloads
    def _argv(self, component: str, user: str, cmd: list[str]) -> list[str]:
        script = self.place(component).prelude() + "gs_in " + shlex.quote(user) + " " + " ".join(shlex.quote(c) for c in cmd)
        return transport.argv(self.mode, self.ssh, script, root=False)

    def _fail(self, what: str, rc: int, err: str) -> BackupError:
        tail = self.mask((err or "").strip().splitlines()[-1] if (err or "").strip() else "no output")[:300]
        return BackupError(f"{what} (exit {rc}): {tail}")

    def _watchdog(self, proc: subprocess.Popen[bytes]) -> threading.Timer:
        t = threading.Timer(self.deadline, lambda: proc.kill())
        t.daemon = True
        t.start()
        return t

    def capture(self, component: str, user: str, cmd: list[str], dest: Path) -> tuple[int, str]:
        """Run `cmd` inside the workload and stream its stdout into `dest` (never into memory), hashing on the way.  Returns (bytes, sha256)."""
        import hashlib
        h = hashlib.sha256()
        size = 0
        proc = subprocess.Popen(self._argv(component, user, cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # nosec B603
        timer = self._watchdog(proc)
        err_buf: list[bytes] = []
        threading.Thread(target=lambda: err_buf.append(proc.stderr.read() if proc.stderr else b""), daemon=True).start()
        try:
            old = os.umask(0o077)
            try:
                with open(dest, "wb") as out:
                    stdout = proc.stdout
                    if stdout is None:
                        raise BackupError("no output stream")
                    for chunk in iter(lambda: stdout.read(CHUNK), b""):
                        out.write(chunk)
                        h.update(chunk)
                        size += len(chunk)
            finally:
                os.umask(old)
            rc = proc.wait()
        except BaseException:
            proc.kill()
            raise
        finally:
            timer.cancel()
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        time.sleep(0.01)
        if rc != 0:
            dest.unlink(missing_ok=True)
            raise self._fail(f"{component}: capture failed", rc, b"".join(err_buf).decode("utf-8", "replace"))
        return size, h.hexdigest()

    def feed(self, component: str, user: str, cmd: list[str], src: Path) -> str:
        """Run `cmd` inside the workload with the file as stdin.  Returns its stdout (text)."""
        with open(src, "rb") as fh:
            p = subprocess.run(self._argv(component, user, cmd), stdin=fh, capture_output=True, timeout=self.deadline)  # nosec B603
        if p.returncode != 0:
            raise self._fail(f"{component}: command failed", p.returncode, p.stderr.decode("utf-8", "replace"))
        return p.stdout.decode("utf-8", "replace")

    def sh(self, component: str, user: str, cmd: list[str], *, timeout: int = 600, check: bool = True) -> tuple[int, str, str]:
        p = subprocess.run(self._argv(component, user, cmd), capture_output=True, timeout=timeout)  # nosec B603
        out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        if check and p.returncode != 0:
            raise self._fail(f"{component}: command failed", p.returncode, err or out)
        return p.returncode, out, err

    def host(self, component: str, body: str, *, timeout: int = 600) -> str:
        p = subprocess.run(transport.argv(self.mode, self.ssh, self.host_script(component, body), root=False), capture_output=True, text=True, timeout=timeout)  # nosec B603
        if p.returncode != 0:
            raise self._fail(f"{component}: host command failed", p.returncode, p.stderr or p.stdout)
        return p.stdout

    def running(self, component: str) -> bool:
        try:
            return self.sh(component, "", ["true"], timeout=45, check=False)[0] == 0
        except subprocess.TimeoutExpired:                  # e.g. `kubectl exec` waiting for a pod that does not exist
            return False

    # ------------------------------------------------------------------ small helpers
    @property
    def db(self) -> str:
        db = str(self.cfg["postgres"]["db"])
        if not DB_NAME.match(db):
            raise BackupError("the configured database name cannot be handled safely")
        return db

    @property
    def pguser(self) -> str:
        return str(self.cfg["postgres"]["user"])

    def psql(self, sql: str, *, db: str = "postgres", timeout: int = 300) -> str:
        return self.sh("postgres", "postgres", ["psql", "-U", self.pguser, "-d", db, "-v", "ON_ERROR_STOP=1", "-Atqc", sql], timeout=timeout)[1].strip()

    def emit(self, msg: str) -> str:
        line = self.mask(msg)
        if self._log is not None:
            with contextlib.suppress(OSError):
                fd = os.open(self._log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)      # owner-only from the first byte, whatever the umask
                try:
                    os.write(fd, (time.strftime("%H:%M:%S ") + line + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
        return line

    def default_components(self, with_cache: bool = False) -> list[str]:
        enabled = self.cfg.get("components", {})
        out = [n for n, c in COMPONENTS.items() if enabled.get(CFG_ENABLED[n]) and (not c["optional"] or with_cache)]
        if not out:
            raise BackupError("no component of this stack can be backed up")
        return out

    def _sibling(self) -> BackupProvider:
        """A provider with the same settings for a nested operation (the automatic pre-restore backup) and its own result/log."""
        sub = copy.copy(self)
        sub.result, sub._log = {}, None
        return sub

    def _target_name(self) -> str:
        return str(self.cfg.get("deploy_target", "compose"))

    # ------------------------------------------------------------------ create
    def create(self, components: Optional[list[str]] = None, *, kind: str = "manual", note: str = "", with_cache: bool = False) -> Iterator[str]:
        """Back the stack up.  Yields log lines; the manifest is left in `self.result`.  Raises (nothing sealed) if any step fails."""
        if kind not in model.KINDS:
            raise BackupError("bad backup kind")
        note = model.check_note(note)
        comps = model.check_components(components) or self.default_components(with_cache)
        bid = model.new_id(pre=kind == "pre-restore")
        self.store.begin(bid)
        part = self.store.partial(bid)
        self._log = part / LOG
        host = transport.host_label(self.mode, self.ssh)
        try:
            with journal.Operation("stack-backup:create", redact=self.mask, target=self._target_name(), mode=self.mode, host=host, project=self.cfg.get("project", ""), backup=bid):
                yield self.emit(f"== backup {bid} ({self.kind}): {', '.join(comps)}" + (" [encrypted]" if self.encryption else ""))
                entry: dict[str, Any] = {}
                for name in comps:
                    if not self.running(name):
                        raise BackupError(f"{name}: the workload is not running - nothing was backed up")
                for name in comps:
                    yield self.emit(f"-- {name}")
                    entry[name] = self._backup_component(name, part)
                    yield self.emit(f"   {name}: {sum(f['bytes'] for f in entry[name]['files'])} bytes, verified")
                sensitive = [n for n in comps if n in SENSITIVE]
                if sensitive and not self.encryption:
                    yield self.emit("!! this backup is NOT encrypted and holds sensitive data (" + "; ".join(SENSITIVE[n] for n in sensitive)
                                    + ") - keep the folder private or encrypt (--encrypt-to / --passphrase-file)")
                manifest = {"schema": model.SCHEMA, "id": bid, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, "note": note,
                            "gisstack_version": __version__, "project": self.cfg.get("project", ""), "target": self.kind, "family": self.family, "host": host,
                            "encrypted": bool(self.encryption), "encryption": self._enc_meta(), "components": entry, "versions": self._versions(comps)}
                self.store.seal(bid, manifest)
                self._log = self.store.path(bid) / LOG
                yield self.emit(f"== sealed {bid}: checksums and structure verified")
                self.result = manifest
        except BaseException as e:
            self.store.fail(bid, f"{type(e).__name__}: {self.mask(str(e))}")
            raise

    def _enc_meta(self) -> dict[str, Any]:
        if not self.encryption:
            return {}
        return {"method": "gpg-passphrase" if self.encryption.passphrase is not None else "gpg-recipients", "recipients": list(self.encryption.recipients)}

    def _versions(self, comps: list[str]) -> dict[str, str]:
        v: dict[str, str] = {}
        if "postgres" in comps:
            v["postgres"] = self.psql("show server_version", db=self.db)
            with contextlib.suppress(BackupError):
                v["postgis"] = self.psql("select postgis_version()", db=self.db)
        return v

    def _backup_component(self, name: str, part: Path) -> dict[str, Any]:
        spec = COMPONENTS[name]
        plain = part / spec["file"]
        if spec["kind"] == "database":
            cmd = ["pg_dump", "-Fc", "-Z", "6", "-U", self.pguser, "-d", self.db]
            size, digest = self.capture(name, "postgres", cmd, plain)
            meta = self._verify_dump(name, plain)
        else:
            path = self.paths[name]
            self.sh(name, "root", ["test", "-d", path])
            size, digest = self.capture(name, "root", ["tar", "czf", "-", "--numeric-owner", "-C", path, "."], plain)
            meta = self._verify_tar(plain)
            meta["path"] = path
        if sha256_file(plain) != digest or plain.stat().st_size != size:         # what reached the disk is what was streamed
            raise IntegrityError(f"{name}: the written file differs from the stream")
        files = []
        if self.encryption:
            enc = part / (spec["file"] + model.ENCRYPTED_SUFFIX)
            backup_crypto.encrypt(plain, enc, self.encryption)
            files.append({"name": enc.name, "bytes": enc.stat().st_size, "sha256": sha256_file(enc), "plain_bytes": size})
        else:
            files.append({"name": plain.name, "bytes": size, "sha256": digest})
        return {"files": files, "meta": meta}

    def _verify_dump(self, name: str, dump: Path) -> dict[str, Any]:
        out = self.feed(name, "postgres", ["pg_restore", "--list"], dump)
        entries = [ln for ln in out.splitlines() if ln and not ln.startswith(";")]
        if not entries:
            raise IntegrityError(f"{name}: the dump contains no objects")
        return {"database": self.db, "toc_entries": len(entries)}

    def _verify_tar(self, tar: Path) -> dict[str, Any]:
        info = model.scan_tar(tar)
        if info["members"] == 0:
            raise IntegrityError(f"{tar.name}: the archive is empty (nothing to back up)")
        return {"members": info["members"], "bytes": info["bytes"]}

    # ------------------------------------------------------------------ list / inspect / verify / delete / prune
    def list_backups(self) -> list[dict[str, Any]]:
        return self.store.list()

    def inspect(self, bid: str) -> dict[str, Any]:
        m = dict(self.store.read(bid))
        m["integrity"] = self.store.verify(bid, structure=False)
        log = self.store.path(bid) / LOG
        try:
            m["log"] = log.read_text(encoding="utf-8", errors="replace").splitlines()[-60:] if log.is_file() else []      # written through the redactor: no secrets
        except OSError:
            m["log"] = []
        return m

    def verify(self, bid: str) -> dict[str, Any]:
        """Checksums of every stored file, the manifest, file set, and (unencrypted) archive structure."""
        return self.store.verify(bid, structure=True)

    def delete(self, bid: str) -> None:
        self.store.delete(bid)

    def prune(self, **kw: Any) -> dict[str, Any]:
        return self.store.prune(**kw)

    # ------------------------------------------------------------------ restore
    def restorable(self, manifest: dict[str, Any], wanted: Optional[list[str]]) -> list[str]:
        names = model.check_components(wanted) if wanted else [n for n in manifest["components"] if self.kind in COMPONENTS[n]["restorable_on"] or
                                                                (self.family == "compose" and any(k in COMPONENTS[n]["restorable_on"] for k in ("compose", "podman")))]
        out = []
        for n in names:
            if n not in manifest["components"]:
                raise RestoreError(f"the backup does not contain {n}")
            if not (self.kind in COMPONENTS[n]["restorable_on"] or (self.family == "compose" and "compose" in COMPONENTS[n]["restorable_on"])):
                raise RestoreError(f"{n} cannot be restored in place on {self.kind} (it is configuration: redeploy instead)")
            out.append(n)
        if not out:
            raise RestoreError("nothing in this backup can be restored in place")
        return out

    def check_restore(self, bid: str, wanted: Optional[list[str]] = None) -> dict[str, Any]:
        """Pre-restore validation without changing anything: integrity, compatibility, workloads running, PostgreSQL version."""
        m = self.store.read(bid)
        rep = self.store.verify(bid, structure=True)
        problems = list(rep["problems"])
        if m.get("family") != self.family:
            problems.append(f"the backup was made on {m.get('target')}, this stack is {self.kind}: not compatible")
        comps: list[str] = []
        try:
            comps = self.restorable(m, wanted)
        except RestoreError as e:
            problems.append(str(e))
        if m.get("encrypted") and not self.passphrase and not any(c for c in [os.environ.get(backup_crypto.ENV_PASSPHRASE)]):
            problems.append("the backup is encrypted: a passphrase (or the private key via gpg-agent) is needed")
        for n in comps:
            if not self.running(n):
                problems.append(f"{n}: the workload is not running")
        if "postgres" in comps and not problems:
            have = pg_major(self.psql("show server_version", db=self.db))
            want = pg_major(m.get("versions", {}).get("postgres", ""))
            if want and have < want:
                problems.append(f"PostgreSQL {have} cannot load a dump made by PostgreSQL {want}")
        return {"ok": not problems, "problems": problems, "components": comps, "backup": bid, "created": m.get("created"), "kind": m.get("kind")}

    def restore(self, bid: str, components: Optional[list[str]] = None, *, confirm: bool = False) -> Iterator[str]:
        if confirm is not True:
            raise RestoreError("restore replaces live data: confirm must be true")
        pending = self.store.read_state()
        if pending:
            raise RestoreError("an interrupted restore is still recorded: run recover first")
        host = transport.host_label(self.mode, self.ssh)
        with journal.Operation("stack-backup:restore", redact=self.mask, target=self._target_name(), mode=self.mode, host=host, project=self.cfg.get("project", ""), backup=bid):
            yield self.emit(f"== restore {bid} into {self.kind}")
            rep = self.check_restore(bid, components)
            if not rep["ok"]:
                raise RestoreError("restore refused, nothing was changed: " + "; ".join(rep["problems"]))
            comps: list[str] = rep["components"]
            yield self.emit(f"   validated: {', '.join(comps)}")
            m = self.store.read(bid)
            workdir = Path(tempfile.mkdtemp(prefix="gsrestore"))
            os.chmod(workdir, 0o700)
            try:
                try:
                    payloads = self._payloads(bid, m, comps, workdir)
                    for n in comps:                           # the bytes about to be extracted are checked again (decrypted archives, and a file swapped after verify)
                        if COMPONENTS[n]["kind"] == "files":
                            model.scan_tar(payloads[n])
                except (DeployError, IntegrityError) as e:    # wrong key / damaged ciphertext / unsafe archive: refused before anything was touched
                    raise RestoreError(f"restore refused, nothing was changed: {self.mask(str(e))[:300]}") from e
                protect = [c for c in comps if c != "mapproxy_cache"]
                pre_id = ""
                if protect:
                    yield self.emit("-- pre-restore backup of what will be replaced")
                    sub = self._sibling()
                    yield from sub.create(protect, kind="pre-restore", note=f"before restore of {bid}")
                    pre_id = sub.result["id"]
                short = "r" + re.sub(r"[^0-9a-z]", "", bid.lower())[:24]
                state: dict[str, Any] = {"phase": "swapping", "backup": bid, "pre_backup": pre_id, "short": short, "done": [], "started": int(time.time())}
                self.store.write_state(state)
                undo: list[Callable[[], None]] = []
                retained: dict[str, str] = {}
                try:
                    for n in comps:
                        yield self.emit(f"-- {n}: stage, validate, swap")
                        if COMPONENTS[n]["kind"] == "database":
                            retained[n] = yield from self._restore_db(payloads[n], short, state, undo)
                        else:
                            retained[n] = yield from self._restore_files(n, payloads[n], short, state, undo)
                    restarted = sorted({n for n in comps if COMPONENTS[n]["kind"] == "files"})
                    for n in restarted:
                        yield self.emit(f"-- restarting {n}")
                        self.restart(n)
                    yield self.emit("-- post-restore health verification")
                    ids = sorted({OPS_ID[n] for n in comps})
                    health = deployments.verify(self.cfg, lambda: list(ops.status(self.cfg, self.mode, self.ssh, self._target_name(), ids)["rows"]))
                    if health["state"] != "healthy":
                        raise RestoreError(f"health verification: {health['state']} {[r['id'] for r in health['rows'] if not r['ok']]}")
                    if "postgres" in comps:
                        self.psql("select 1", db=self.db)
                except GeneratorExit:                       # the consumer stopped reading (job cancelled): put everything back, no more output possible
                    if not self._run_undo(undo):
                        self.store.clear_state()
                    raise
                except BaseException as e:
                    yield self.emit(f"!! restore failed: {type(e).__name__}: {self.mask(str(e))[:300]} - restoring the previous data")
                    failed_undo = self._run_undo(undo)
                    for n in sorted({n for n in comps if COMPONENTS[n]["kind"] == "files"}):
                        with contextlib.suppress(Exception):
                            self.restart(n)
                    if failed_undo:
                        state["phase"] = "undo-failed"
                        state["errors"] = failed_undo
                        self.store.write_state(state)
                        raise RestoreError("restore failed AND the automatic undo failed for: " + "; ".join(failed_undo) + ". The previous data is still on disk; see `recover`.") from e
                    self.store.clear_state()
                    raise RestoreError(f"restore failed, the previous data was put back and nothing was lost: {self.mask(str(e))[:300]}") from e
                self.store.clear_state()
                self.result = {"restored": comps, "backup": bid, "pre_restore_backup": pre_id, "retained_previous_data": retained}
                for n, where in sorted(retained.items()):
                    yield self.emit(f"   previous {n} data kept at: {where}")
                yield self.emit(f"== restore {bid} finished and verified healthy; the previous data above was kept (remove it yourself when you are satisfied)")
            finally:
                fsutil_rmtree(workdir)

    def _payloads(self, bid: str, m: dict[str, Any], comps: list[str], workdir: Path) -> dict[str, Path]:
        """Plain files to restore from (decrypted into a private temp dir when the backup is encrypted)."""
        out: dict[str, Path] = {}
        for n in comps:
            f = m["components"][n]["files"][0]
            src = self.store.path(bid) / f["name"]
            if m.get("encrypted"):
                out[n] = backup_crypto.decrypt(src, workdir / COMPONENTS[n]["file"], self.passphrase or os.environ.get(backup_crypto.ENV_PASSPHRASE))
            else:
                out[n] = src
        return out

    # --- PostgreSQL: scratch database, validation, reversible rename swap
    def _restore_db(self, dump: Path, short: str, state: dict[str, Any], undo: list[Callable[[], None]]) -> Generator[str, None, str]:
        db, scratch, pre = self.db, f"{self.db}_{short}", f"{self.db}_pre_{short}"
        for ident in (scratch, pre):
            if len(ident) > 62:
                raise RestoreError("database name too long to build safe restore names")
        self.psql(f'DROP DATABASE IF EXISTS "{scratch}"')                      # a leftover of *our own* earlier attempt, never user data
        self.psql(f'CREATE DATABASE "{scratch}"')
        try:
            self.feed("postgres", "postgres", ["pg_restore", "-U", self.pguser, "-d", scratch, "--exit-on-error", "--single-transaction", "--no-password"], dump)
            self.psql("select 1", db=scratch)
            self.psql("select count(*) from pg_class", db=scratch)
            if self.psql("select count(*) from pg_extension where extname='postgis'", db=scratch) == "1":
                self.psql("select postgis_version()", db=scratch)
        except BaseException:
            with contextlib.suppress(BackupError):
                self.psql(f'DROP DATABASE IF EXISTS "{scratch}"')
            raise
        yield self.emit(f"   {db}: restored into {scratch} and validated")
        state["postgres"] = {"db": db, "scratch": scratch, "pre": pre, "swapped": False}
        self.store.write_state(state)

        def undo_db() -> None:
            st = state["postgres"]
            if st["swapped"]:
                self.psql(f'ALTER DATABASE "{st["db"]}" ALLOW_CONNECTIONS false')
                self._kick(st["db"])
                self.psql(f'ALTER DATABASE "{st["db"]}" RENAME TO "{st["scratch"]}"')
                self.psql(f'ALTER DATABASE "{st["pre"]}" RENAME TO "{st["db"]}"')
                st["swapped"] = False
                self.store.write_state(state)
            self.psql(f'ALTER DATABASE "{st["db"]}" ALLOW_CONNECTIONS true')
            self.psql(f'DROP DATABASE IF EXISTS "{st["scratch"]}"')
        undo.append(undo_db)
        self.psql(f'ALTER DATABASE "{db}" ALLOW_CONNECTIONS false')             # nobody reconnects between "kick" and "rename"
        self._kick(db)
        self.psql(f'ALTER DATABASE "{db}" RENAME TO "{pre}"; ALTER DATABASE "{scratch}" RENAME TO "{db}"')
        state["postgres"]["swapped"] = True
        self.store.write_state(state)
        self.psql(f'ALTER DATABASE "{pre}" ALLOW_CONNECTIONS true')
        yield self.emit(f"   {db}: swapped; the previous database is kept as {pre}")
        return pre

    def _kick(self, db: str) -> None:
        if not DB_NAME.match(db) and not re.match(r"^[A-Za-z0-9_]{1,62}$", db):
            raise RestoreError("unsafe database name")
        self.psql(f"select pg_terminate_backend(pid) from pg_stat_activity where datname='{db}' and pid<>pg_backend_pid()")  # noqa: S608  # nosec B608 - identifier validated above

    # --- files in a volume: stage inside the same volume, validate, move aside + move in (all renames)
    def _restore_files(self, name: str, archive: Path, short: str, state: dict[str, Any], undo: list[Callable[[], None]]) -> Generator[str, None, str]:
        spec, d = COMPONENTS[name], self.paths[name]
        stage, pre, failed = f"{d}/.gs-staging-{short}", f"{d}/.gs-pre-{short}", f"{d}/.gs-failed-{short}"
        marker = spec.get("marker")
        self.sh(name, "root", ["sh", "-c", 'rm -rf "$1" && mkdir -p "$1"', "sh", stage])
        try:
            self.feed(name, "root", ["tar", "xzf", "-", "--numeric-owner", "-C", stage], archive)
            self.sh(name, "root", ["sh", "-c", '[ -n "$(ls -A "$1")" ] || { echo "restored archive is empty" >&2; exit 1; }; [ -z "$2" ] || [ -e "$1/$2" ] || { echo "expected file $2 is missing" >&2; exit 1; }',
                                   "sh", stage, marker or ""])
        except BaseException:
            with contextlib.suppress(BackupError):
                self.sh(name, "root", ["rm", "-rf", stage])
            raise
        yield self.emit(f"   {name}: staged and validated")
        state[name] = {"dir": d, "stage": stage, "pre": pre, "failed": failed, "swapped": False}
        self.store.write_state(state)

        def undo_files() -> None:
            st = state[name]
            if st["swapped"]:
                self.sh(name, "root", ["sh", "-c",
                                       'set -e; cd "$1"; mkdir -p "$3"; find . -mindepth 1 -maxdepth 1 ! -name ".gs-*" -exec mv {} "$3"/ \\; ; '
                                       'find "$2" -mindepth 1 -maxdepth 1 -exec mv {} . \\; ; rmdir "$2"', "sh", st["dir"], st["pre"], st["failed"]])
                st["swapped"] = False
                self.store.write_state(state)
            self.sh(name, "root", ["rm", "-rf", st["stage"]])
        undo.append(undo_files)
        self.sh(name, "root", ["sh", "-c",
                               'set -e; cd "$1"; mkdir "$3"; find . -mindepth 1 -maxdepth 1 ! -name ".gs-*" -exec mv {} "$3"/ \\; ; '
                               'find "$2" -mindepth 1 -maxdepth 1 -exec mv {} . \\; ; rmdir "$2"', "sh", d, stage, pre])
        state[name]["swapped"] = True
        self.store.write_state(state)
        yield self.emit(f"   {name}: swapped; the previous files are kept in {pre}")
        return pre

    def _run_undo(self, undo: list[Callable[[], None]]) -> list[str]:
        errors = []
        for fn in reversed(undo):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - every undo step is attempted; failures are reported, never hidden
                errors.append(f"{type(e).__name__}: {self.mask(str(e))[:200]}")
        return errors

    # ------------------------------------------------------------------ recovery of an interrupted restore
    def recover(self) -> Iterator[str]:
        """Put the previous data back after a restore that was interrupted (process killed, connection lost).  Idempotent."""
        state = self.store.read_state()
        if not state:
            yield "nothing to recover"
            return
        yield self.emit(f"== recovering interrupted restore of {state.get('backup', '?')} (phase {state.get('phase')})")
        errors: list[str] = []
        for name, st in state.items():
            if not isinstance(st, dict) or "swapped" not in st:
                continue
            try:
                if name == "postgres":
                    yield from self._recover_db(st)
                else:
                    yield from self._recover_files(name, st)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {self.mask(str(e))[:200]}")
        if errors:
            raise RestoreError("recovery incomplete: " + "; ".join(errors))
        self.store.clear_state()
        yield self.emit("== recovered; the previous data is back in place")

    def _recover_db(self, st: dict[str, Any]) -> Iterator[str]:
        db, scratch, pre = st["db"], st["scratch"], st["pre"]
        have = set(self.psql("select datname from pg_database", db="postgres").splitlines())
        if db in have and scratch in have and pre not in have:
            pass                                                                # never swapped: only the scratch copy needs to go
        elif pre in have and db in have and scratch not in have:
            self._kick(db)                                                       # swapped: put the original back, the restored copy becomes scratch
            self.psql(f'ALTER DATABASE "{db}" ALLOW_CONNECTIONS false')
            self._kick(db)
            self.psql(f'ALTER DATABASE "{db}" RENAME TO "{scratch}"')
            self.psql(f'ALTER DATABASE "{pre}" RENAME TO "{db}"')
        elif pre in have and db not in have:
            self.psql(f'ALTER DATABASE "{pre}" RENAME TO "{db}"')                # interrupted between the two renames
        self.psql(f'ALTER DATABASE "{db}" ALLOW_CONNECTIONS true')
        self.psql(f'DROP DATABASE IF EXISTS "{scratch}"')
        yield self.emit(f"   {db}: original database in place")

    def _recover_files(self, name: str, st: dict[str, Any]) -> Iterator[str]:
        script = ('set -e; cd "$1"; if [ -d "$3" ]; then mkdir -p "$4"; find . -mindepth 1 -maxdepth 1 ! -name ".gs-*" -exec mv {} "$4"/ \\; ; '
                  'find "$3" -mindepth 1 -maxdepth 1 -exec mv {} . \\; ; rmdir "$3"; fi; rm -rf "$2"')
        self.sh(name, "root", ["sh", "-c", script, "sh", st["dir"], st["stage"], st["pre"], st["failed"]])
        with contextlib.suppress(Exception):
            self.restart(name)
        yield self.emit(f"   {name}: original files in place")

    def discard_previous(self, retained: dict[str, str], *, confirm: bool = False) -> Iterator[str]:
        """Remove the data a finished restore kept aside (`<db>_pre_*` databases, `.gs-pre-*` directories).  Explicit and confirmed only."""
        if confirm is not True:
            raise RestoreError("discarding previous data needs confirm=true")
        for name, where in retained.items():
            if COMPONENTS[name]["kind"] == "database":
                if not re.match(r"^[A-Za-z0-9_]+_pre_r[0-9a-z]+$", where):
                    raise RestoreError("refusing: that is not a restore-retained database")
                self.psql(f'DROP DATABASE IF EXISTS "{where}"')
            else:
                if not re.search(r"/\.gs-pre-r[0-9a-z]+$", where) or ".." in where:
                    raise RestoreError("refusing: that is not a restore-retained directory")
                self.sh(name, "root", ["rm", "-rf", where])
            yield self.emit(f"   removed {where}")


def fsutil_rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# ====================================================================== environment adapters
class ComposeBackupProvider(BackupProvider):
    kind, family, runtime = "compose", "compose", "docker"

    def place(self, component: str) -> Place:
        return Place("compose", COMPONENTS[component]["workload"], project=str(self.cfg["project"]), runtime=self.runtime)

    def restart(self, component: str) -> None:
        self.host(component, f'{shlex.quote(self.runtime)} restart "$GS_CTR" >/dev/null', timeout=300)


class PodmanBackupProvider(ComposeBackupProvider):
    """Compose files, podman runtime: the same code path with `podman` instead of `docker` (backups are interchangeable with Compose)."""
    kind, runtime = "podman", "podman"


class KubernetesBackupProvider(BackupProvider):
    kind, family = "k8s", "k8s"

    def place(self, component: str) -> Place:
        return Place("k8s", COMPONENTS[component]["workload"], namespace=str(self.cfg["k8s"]["namespace"]))

    def running(self, component: str) -> bool:
        """Ready replicas first (a workload scaled to zero has no pod to exec into, and `kubectl exec` would wait for one for a minute)."""
        from ..targets import WORKLOADS
        workload = WORKLOADS[COMPONENTS[component]["workload"]][1]
        ns = shlex.quote(str(self.cfg["k8s"]["namespace"]))
        try:
            ready = self.host(component, f"kubectl -n {ns} get {shlex.quote(workload)} -o jsonpath='{{.status.readyReplicas}}'", timeout=60).strip()
        except (BackupError, subprocess.TimeoutExpired):
            return False
        return ready not in ("", "0") and super().running(component)

    def restart(self, component: str) -> None:
        from ..targets import WORKLOADS
        workload = WORKLOADS[COMPONENTS[component]["workload"]][1]
        ns = shlex.quote(str(self.cfg["k8s"]["namespace"]))
        self.host(component, f"kubectl -n {ns} rollout restart {shlex.quote(workload)} >/dev/null && kubectl -n {ns} rollout status {shlex.quote(workload)} --timeout=300s >/dev/null", timeout=400)


def provider_for(cfg: dict[str, Any], mode: str = "local", ssh: Optional[dict[str, Any]] = None, **kw: Any) -> BackupProvider:
    target = cfg.get("deploy_target", "native")
    if target == "compose":
        return (PodmanBackupProvider if cfg.get("runtime") == "podman" else ComposeBackupProvider)(cfg, mode, ssh, **kw)
    if target == "k8s":
        return KubernetesBackupProvider(cfg, mode, ssh, **kw)
    raise DeployError("the native target has its own backup tool (gisstack backup ...); this is for compose, podman and k8s")


__all__ = ["BackupProvider", "ComposeBackupProvider", "PodmanBackupProvider", "KubernetesBackupProvider", "provider_for"]
