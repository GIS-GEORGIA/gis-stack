"""Backup and restore of an installed native stack (Phase 7).

The work is done on the server by `gisstack-backup` (templates/native/gisstack-backup.sh): sealed backups with a manifest and
checksums, retention, and restores that never overwrite silently.  This module installs that tool, drives it through the
transport (here or over SSH), parses its answers, copies backups to / from this machine and manages the systemd schedule."""
from __future__ import annotations

import hashlib
import io
import os
import re
import shlex
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Iterator, Optional

from . import __version__, backup_crypto, journal, plugins, runner, transport
from .core import paths
from .core.errors import DeployError

TOOL = "/usr/local/sbin/gisstack-backup"
REMOTE_ROOT = "/var/backups/gisstack/stack"
COMPONENTS = ("config", "postgres", "geoserver", "qgis", "cache")
_ID = re.compile(r"^(pre-)?\d{8}-\d{6}-[0-9a-f]{8}$")
_CAL = re.compile(r"^[A-Za-z0-9*:,./\- ~]{1,60}$")
TOOL_SOURCE = Path(__file__).resolve().parent / "templates/native/gisstack-backup.sh"


def tool_text() -> str:
    """The tool with this program's version stamped into the manifests it writes."""
    return TOOL_SOURCE.read_text(encoding="utf-8").replace('"${GS_VERSION_STAMP:-}"', f'"${{GS_VERSION_STAMP:-{__version__}}}"')


def tool_digest() -> str:
    return hashlib.sha256(tool_text().encode()).hexdigest()


def _check_id(bid: str) -> str:
    if not _ID.match(str(bid)):
        raise DeployError("ბექაფის ID არასწორია")
    return str(bid)


def _check_components(comps: Optional[list[str]]) -> list[str]:
    out = list(comps or [])
    for c in out:
        if c not in COMPONENTS:
            raise DeployError(f"უცნობი კომპონენტი: {c} ({', '.join(COMPONENTS)})")
    return out


def _cmd(*args: str) -> str:
    return " ".join([shlex.quote(TOOL), *map(shlex.quote, args)])


def _require_native(cfg: dict[str, Any]) -> None:
    if cfg.get("deploy_target") != "native":
        raise DeployError("ბექაფი/აღდგენა ჯერ მხოლოდ native სამიზნეზეა")


def install_tool(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], force: bool = False) -> bool:
    """Put gisstack-backup on the server (idempotent).  Returns True when it was (re)installed."""
    _require_native(cfg)
    want = tool_digest()
    if not force:
        have = transport.run(mode, ssh, f"sha256sum {shlex.quote(TOOL)} 2>/dev/null | cut -d' ' -f1; true", root=True).strip()
        if have == want:
            return False
    script = (f"set -e; install -d -m 755 {shlex.quote(str(Path(TOOL).parent))}; cat > {shlex.quote(TOOL)}.new; "
              f"chmod 755 {shlex.quote(TOOL)}.new; mv -f {shlex.quote(TOOL)}.new {shlex.quote(TOOL)}; "
              f"install -d -m 700 {shlex.quote(REMOTE_ROOT)}")
    transport.run(mode, ssh, script, root=True, timeout=60, input_text=tool_text())
    return True


def _host(mode: str, ssh: Optional[dict[str, Any]]) -> str:
    return transport.host_label(mode, ssh)


def _run_tool(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], op: str, args: list[str], *,
              timeout: int = 3600, check_marker: Optional[str] = None) -> Iterator[str]:
    """Stream a tool command (journalled, bounded).  Raises DeployError when `check_marker` is missing from the output."""
    mask = runner.redactor(runner.secrets_of(cfg))
    seen: list[str] = []
    with journal.Operation(f"backup:{op}", redact=mask, target="native", mode=mode, host=_host(mode, ssh), project=cfg.get("project", "")):
        try:
            for line in transport.stream(mode, ssh, _cmd(*args), root=True, deadline=timeout):
                seen.append(line)
                yield mask(line)
        except DeployError as e:
            raise DeployError(mask(str(e))) from e
        if check_marker and not any(check_marker in x for x in seen):
            raise DeployError(f"{op} [{check_marker}]: ვერ დადასტურდა")


def create(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], components: Optional[list[str]] = None, *,
           with_cache: bool = False, note: str = "", kind: str = "manual") -> Iterator[str]:
    """Create a verified backup on the server.  Yields log lines; the last useful one is `BACKUP_VERIFIED=<id>`."""
    _require_native(cfg)
    comps = _check_components(components)
    if kind not in ("manual", "scheduled"):
        raise DeployError("bad kind")
    if install_tool(cfg, mode, ssh):
        yield "gisstack-backup დაყენებულია"
    args = ["create", "--kind", kind]
    if comps:
        args += ["--components", ",".join(comps)]
    if with_cache:
        args.append("--with-cache")
    if note:
        args += ["--note", note[:200]]
    yield from _run_tool(cfg, mode, ssh, "create", args, check_marker="BACKUP_VERIFIED=")


def created_id(lines: list[str]) -> str:
    for line in reversed(lines):
        m = re.match(r"BACKUP_VERIFIED=(\S+)", line)
        if m:
            return m.group(1)
    raise DeployError("ბექაფის ID ვერ მოიძებნა")


def list_backups(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    _require_native(cfg)
    install_tool(cfg, mode, ssh)
    out = transport.run(mode, ssh, _cmd("list"), root=True, timeout=60)
    rows = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) == 6:
            rows.append({"id": f[0], "status": f[1], "kind": f[2], "created": f[3], "bytes": int(f[4] or 0), "components": f[5].split()})
    return sorted(rows, key=lambda r: r["id"].removeprefix("pre-"), reverse=True)


def verify(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], bid: str) -> Iterator[str]:
    _require_native(cfg)
    _check_id(bid)
    yield from _run_tool(cfg, mode, ssh, "verify", ["verify", bid], timeout=1800, check_marker="VERIFY_OK=")


def prune(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], keep: int = 14, days: Optional[int] = None,
          keep_pre: int = 5, dry_run: bool = False) -> Iterator[str]:
    _require_native(cfg)
    if keep < 1 or keep_pre < 0 or (days is not None and days < 0):
        raise DeployError("keep ≥ 1 (უახლესი ბექაფი არასდროს იშლება)")
    args = ["prune", "--keep", str(int(keep)), "--keep-pre", str(int(keep_pre))]
    if days is not None:
        args += ["--days", str(int(days))]
    if dry_run:
        args.append("--dry-run")
    yield from _run_tool(cfg, mode, ssh, "prune", args, timeout=900, check_marker="PRUNE_DONE")


def restore(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], bid: str, *, only: Optional[list[str]] = None,
            dry_run: bool = True, staging: bool = False, confirm: bool = False, pre_backup: bool = True, rollback: bool = True) -> Iterator[str]:
    """Restore a backup.  By default it only PLANS (dry_run=True); replacing live data needs dry_run=False AND confirm=True."""
    _require_native(cfg)
    _check_id(bid)
    comps = _check_components(only)
    if not dry_run and not staging and not confirm:
        raise DeployError("აღდგენა ცოცხალ მონაცემებს ცვლის: საჭიროა ექსპლიციტური დადასტურება (confirm)")
    install_tool(cfg, mode, ssh)
    args = ["restore", bid]
    if comps:
        args += ["--only", ",".join(comps)]
    if dry_run:
        args.append("--dry-run")
    elif staging:
        args.append("--staging")
    else:
        args.append("--yes")
        if not pre_backup:
            args.append("--no-pre-backup")
        if not rollback:
            args.append("--no-rollback")
    marker = "RESTORE_DRY_RUN_OK" if dry_run else ("RESTORE_STAGING_OK=" if staging else "RESTORE_OK=")
    yield from _run_tool(cfg, mode, ssh, "restore:" + ("plan" if dry_run else "staging" if staging else "run"), args, timeout=7200, check_marker=marker)


# --------------------------------------------------------------------------- copies on this machine
def _dest_dir(mode: str, ssh: Optional[dict[str, Any]], dest: Optional[str]) -> Path:
    from . import fsutil
    base = Path(dest).expanduser() if dest else paths.backups_dir() / "stack"
    return fsutil.private_dir(base / re.sub(r"[^A-Za-z0-9._-]", "_", _host(mode, ssh)))


def download(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], bid: str, dest: Optional[str] = None,
             encrypt: Optional[backup_crypto.Spec] = None) -> Path:
    """Copy one backup to this machine as <id>.tar.gz (never overwrites) and verify it against the checksums inside.
    With `encrypt` the result is <id>.tar.gz.gpg: the verified archive is encrypted here (see backup_crypto) and the plaintext is removed."""
    _require_native(cfg)
    _check_id(bid)
    if encrypt is not None:
        encrypt.validate()
        backup_crypto.gpg()                                  # fail before downloading gigabytes
        final = _dest_dir(mode, ssh, dest) / f"{bid}.tar.gz{backup_crypto.SUFFIX}"
        if final.exists():
            raise DeployError(f"ფაილი უკვე არსებობს და არ გადაიწერება: {final}")
        import shutil
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="gisstack-enc-", dir=final.parent))      # private (0700) staging for the plaintext
        try:
            plain = download(cfg, mode, ssh, bid, str(tmp))
            enc = backup_crypto.encrypt(plain, final, encrypt)
            try:
                if encrypt.passphrase is not None:
                    with backup_crypto.decrypted(enc, encrypt.passphrase) as back:
                        verify_archive(back)                  # the encrypted copy really restores to a valid backup
                else:
                    backup_crypto.check_packets(enc, encrypt.recipients)
            except BaseException:
                enc.unlink(missing_ok=True)
                raise
            return enc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    out = _dest_dir(mode, ssh, dest) / f"{bid}.tar.gz"
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as e:
        raise DeployError(f"ფაილი უკვე არსებობს და არ გადაიწერება: {out}") from e
    script = f"tar czf - -C {shlex.quote(REMOTE_ROOT)} {shlex.quote(bid)}"
    try:
        with os.fdopen(fd, "wb") as fh:
            p = subprocess.run(transport.argv(mode, ssh, script, root=True), stdin=subprocess.DEVNULL, stdout=fh,
                               stderr=subprocess.PIPE, timeout=3600)
        if p.returncode != 0 or out.stat().st_size == 0:
            raise DeployError("ჩამოტვირთვა ვერ მოხერხდა: " + p.stderr.decode(errors="replace").strip()[-200:])
        verify_archive(out)
    except subprocess.TimeoutExpired as e:
        out.unlink(missing_ok=True)
        raise DeployError("ჩამოტვირთვა ვერ მოხერხდა: დრო ამოიწურა (1 საათი); ნაწილობრივი ფაილი წაიშალა") from e
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    return out


def offsite(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], bid: str, encrypt: Optional[backup_crypto.Spec] = None) -> Iterator[str]:
    """Copy one backup, verified (and encrypted when `encrypt` is given), to every BackupProvider plugin.  Yields log lines; a provider that fails is reported and the others still run."""
    import shutil
    import tempfile
    _check_id(bid)
    targets = plugins.get("backup_provider")
    if not targets:
        raise DeployError("BackupProvider პლაგინი არ არის ჩართული (GISSTACK_FEATURES=plugins და დაყენებული პლაგინი)")
    tmp = Path(tempfile.mkdtemp(prefix="gisstack-offsite-"))          # mkdtemp is 0700
    failed = 0
    try:
        yield f"ჩამოტვირთვა: {bid}"
        archive = download(cfg, mode, ssh, bid, str(tmp), encrypt=encrypt) if encrypt else download(cfg, mode, ssh, bid, str(tmp))
        yield f"შემოწმებულია{' და დაშიფრულია' if encrypt else ''}: {archive.name}"
        for plug in targets:
            try:
                where = plug.store(archive, bid)  # type: ignore[attr-defined]
                yield f"{plug.name}: {where}"
            except Exception as e:  # noqa: BLE001 - third-party code
                failed += 1
                yield f"{plug.name}: ვერ მოხერხდა ({type(e).__name__})"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failed:
        raise DeployError(f"{failed} პლაგინმა ვერ შეინახა ასლი")


def verify_archive(path: str | Path) -> int:
    """Check a downloaded backup against the SHA256SUMS inside it (every listed file present and intact).  Returns the file count."""
    want: dict[str, str] = {}
    try:
        with tarfile.open(path, "r:gz") as tf:
            members = {m.name: m for m in tf.getmembers() if m.isfile()}
            sums = next((n for n in members if n.endswith("/SHA256SUMS")), None)
            if sums is None:
                raise DeployError("ბექაფში SHA256SUMS არ არის")
            prefix = sums[: -len("SHA256SUMS")]
            for line in (tf.extractfile(members[sums]) or io.BytesIO()).read().decode("utf-8", "replace").splitlines():
                parts = line.split()
                if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                    want[parts[1].lstrip("*")] = parts[0]
            if not want or (prefix + "MANIFEST.json") not in members:
                raise DeployError("ბექაფის მანიფესტი არ არის")
            for name, digest in want.items():
                m = members.get(prefix + name)
                if m is None:
                    raise DeployError(f"ბექაფში ფაილი აკლია: {name}")
                h = hashlib.sha256()
                fh = tf.extractfile(m)
                while fh is not None:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
                if h.hexdigest() != digest:
                    raise DeployError(f"ბექაფის ფაილი დაზიანებულია (SHA-256): {name}")
    except (tarfile.TarError, EOFError, OSError) as e:
        raise DeployError(f"ბექაფის არქივი ვერ იკითხება: {e}") from e
    return len(want)


def upload(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], archive: str | Path, passphrase: Optional[str] = None) -> str:
    """Put a downloaded backup onto a server (disaster recovery to a new machine).  The archive is verified here first and the
    server refuses to overwrite an existing backup.  An encrypted copy (.gpg) is decrypted here, in a private temporary directory, and the
    server only ever receives the plaintext archive over the (encrypted) SSH connection.  Returns the backup id."""
    _require_native(cfg)
    if backup_crypto.looks_encrypted(archive):
        with backup_crypto.decrypted(archive, passphrase) as plain:
            return upload(cfg, mode, ssh, plain)
    verify_archive(archive)
    with tarfile.open(archive, "r:gz") as tf:
        tops = {n.split("/", 1)[0] for n in tf.getnames()}
        for m in tf.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts or m.issym() or m.islnk():
                raise DeployError("ბექაფის არქივი სახიფათოა")
    if len(tops) != 1 or not _ID.match(next(iter(tops))):
        raise DeployError("არქივი ერთი ბექაფის საქაღალდეს უნდა შეიცავდეს")
    bid = next(iter(tops))
    install_tool(cfg, mode, ssh)
    script = (f"set -e; R={shlex.quote(REMOTE_ROOT)}; umask 077; [ ! -e \"$R/{bid}\" ] || {{ echo 'ALREADY_EXISTS'; exit 3; }}; "
              f"mkdir -p \"$R\" && tar xzf - -C \"$R\" --no-same-owner && echo UPLOAD_OK={bid}")
    with open(archive, "rb") as fh:
        p = runner.run(transport.argv(mode, ssh, script, root=True), timeout=3600, stdin=fh, error=DeployError)
    if p.returncode != 0 or f"UPLOAD_OK={bid}" not in p.stdout:
        raise DeployError("ატვირთვა ვერ მოხერხდა: " + (p.stdout + p.stderr).strip()[-200:])
    return bid


# --------------------------------------------------------------------------- schedule
def schedule_units(calendar: str = "daily", keep: int = 14, days: Optional[int] = None, components: Optional[list[str]] = None,
                   with_cache: bool = False) -> tuple[str, str]:
    """(service, timer) unit files for scheduled backups followed by pruning."""
    if not _CAL.match(calendar):
        raise DeployError("OnCalendar მნიშვნელობა არასწორია")
    comps = _check_components(components)
    if keep < 1 or (days is not None and days < 0):
        raise DeployError("keep ≥ 1")
    create_args = ["create", "--kind", "scheduled"] + (["--components", ",".join(comps)] if comps else []) + (["--with-cache"] if with_cache else [])
    prune_args = ["prune", "--keep", str(int(keep))] + (["--days", str(int(days))] if days is not None else [])
    service = (f"[Unit]\nDescription=gis-stack scheduled backup\nAfter=network.target\n\n[Service]\nType=oneshot\nNice=10\nIOSchedulingClass=idle\n"
               f"ExecStart={TOOL} {' '.join(create_args)}\nExecStartPost={TOOL} {' '.join(prune_args)}\n")
    timer = (f"[Unit]\nDescription=gis-stack scheduled backup\n\n[Timer]\nOnCalendar={calendar}\nRandomizedDelaySec=15min\nPersistent=true\n\n"
             f"[Install]\nWantedBy=timers.target\n")
    return service, timer


def schedule_enable(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]], **kw: Any) -> str:
    _require_native(cfg)
    service, timer = schedule_units(**kw)
    install_tool(cfg, mode, ssh)
    for name, text in (("gisstack-backup.service", service), ("gisstack-backup.timer", timer)):
        transport.run(mode, ssh, f"cat > /etc/systemd/system/{name} && chmod 644 /etc/systemd/system/{name}", root=True, input_text=text)
    transport.run(mode, ssh, "systemctl daemon-reload && systemctl enable --now gisstack-backup.timer", root=True, timeout=60)
    return schedule_status(cfg, mode, ssh)


def schedule_disable(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]]) -> str:
    _require_native(cfg)
    transport.run(mode, ssh, "systemctl disable --now gisstack-backup.timer 2>&1; "
                  "rm -f /etc/systemd/system/gisstack-backup.timer /etc/systemd/system/gisstack-backup.service; systemctl daemon-reload; true",
                  root=True, timeout=60)
    return schedule_status(cfg, mode, ssh)


def schedule_status(cfg: dict[str, Any], mode: str, ssh: Optional[dict[str, Any]]) -> str:
    _require_native(cfg)
    out = transport.run(mode, ssh, "systemctl is-enabled gisstack-backup.timer 2>/dev/null || echo disabled; "
                        "systemctl list-timers gisstack-backup.timer --no-legend 2>/dev/null | head -1; true", root=True, timeout=30)
    return out.strip()


def format_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(ბექაფი არ არის)"
    head = ("ID", "STATUS", "KIND", "CREATED", "SIZE", "COMPONENTS")
    data = [(r["id"], r["status"], r["kind"], r["created"], f"{r['bytes'] / 1048576:.1f} MB", " ".join(r["components"])) for r in rows]
    w = [max(len(str(x[i])) for x in [head, *data]) for i in range(len(head))]
    return "\n".join("  ".join(str(c).ljust(w[i]) for i, c in enumerate(row)).rstrip() for row in [head, *data])
