"""Encryption of backup copies that leave the server (uses `gpg`).

A backup on the server sits next to the live secrets it protects (credentials.txt, stack.env), so what encryption adds is protection of
the COPIES: the file downloaded to a laptop, an off-site upload, a USB drive.  Encryption therefore happens on the operator's machine,
after the download has been verified; the server never sees a passphrase or a private key and `gisstack-backup` is unchanged.

Two ways, both OpenPGP (authenticated encryption, so a modified file does not decrypt):
  * passphrase  - symmetric AES-256; the passphrase comes from a file, the environment or a prompt, never from the command line;
  * recipient   - public-key; only the holder of the private key can decrypt (the key id / fingerprint / e-mail must already be in your keyring).
The plaintext is checked (checksums inside the archive) BEFORE it is encrypted and the encrypted copy is checked afterwards (a full
decrypt-and-verify for passphrase copies; a packet check for recipient copies, whose private key may be offline)."""
from __future__ import annotations

import contextlib
import getpass
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from .core.errors import DeployError

SUFFIX = ".gpg"
MIN_PASSPHRASE = 12
ENV_PASSPHRASE = "GISSTACK_BACKUP_PASSPHRASE"
_RECIPIENT = re.compile(r"^[A-Za-z0-9@._+][A-Za-z0-9@._+ <>-]{2,99}$")      # no leading "-": a recipient can never become a gpg option


@dataclass
class Spec:
    """How to encrypt: a passphrase, or one or more recipients (not both)."""
    passphrase: Optional[str] = None
    recipients: list[str] = field(default_factory=list)

    def validate(self) -> Spec:
        if bool(self.passphrase) == bool(self.recipients):
            raise DeployError("დაშიფვრა: ან პაროლი, ან მიმღები (გასაღები) — ერთ-ერთი")
        if self.passphrase is not None and len(self.passphrase) < MIN_PASSPHRASE:
            raise DeployError(f"დაშიფვრის პაროლი მინიმუმ {MIN_PASSPHRASE} სიმბოლო უნდა იყოს")
        for r in self.recipients:
            if not _RECIPIENT.match(r):
                raise DeployError("მიმღების გასაღები არასწორია (გამოიყენე key ID, fingerprint ან e-mail)")
        return self


def gpg() -> str:
    exe = shutil.which("gpg")
    if not exe:
        raise DeployError("დაშიფვრისთვის gpg სჭირდება (apt install gnupg / dnf install gnupg2)")
    return exe


def read_passphrase(file: Optional[str] = None, *, prompt: bool = False, confirm: bool = False,
                    ask: Callable[[str], str] = getpass.getpass) -> Optional[str]:
    """Passphrase from a file (first line), $GISSTACK_BACKUP_PASSPHRASE, or - when `prompt` - the terminal.  None when none was given."""
    if file:
        p = Path(file).expanduser()
        try:
            if p.stat().st_mode & 0o077 and hasattr(os, "geteuid"):
                raise DeployError(f"{p}: პაროლის ფაილი სხვებისთვისაც წაკითხვადია (chmod 600)")
            return p.read_text(encoding="utf-8").split("\n", 1)[0].rstrip("\r")
        except OSError as e:
            raise DeployError(f"პაროლის ფაილი ვერ იკითხება: {e}") from e
    if os.environ.get(ENV_PASSPHRASE):
        return os.environ[ENV_PASSPHRASE]
    if prompt:
        if not sys.stdin.isatty():
            raise DeployError("პაროლის შეყვანა ტერმინალს საჭიროებს (გამოიყენე --passphrase-file ან " + ENV_PASSPHRASE + ")")
        one = ask("Backup passphrase: ")
        if confirm and ask("Repeat: ") != one:
            raise DeployError("პაროლები არ ემთხვევა")
        return one
    return None


def _run(cmd: list[str], passphrase: Optional[str], timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, input=passphrase + "\n" if passphrase is not None else None, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise DeployError(f"gpg ვერ გაეშვა: {e}") from e


def _fail(what: str, p: subprocess.CompletedProcess[str]) -> DeployError:
    lines = [x for x in (p.stderr or "").strip().splitlines() if x.strip()]
    return DeployError(f"{what}: " + (lines[-1][:200] if lines else f"gpg exit {p.returncode}"))


def encrypt(src: str | Path, dest: str | Path, spec: Spec) -> Path:
    """Encrypt `src` to `dest` (never overwrites; owner-only) and delete `src` once the result exists."""
    spec.validate()
    src, dest = Path(src), Path(dest)
    if dest.exists():
        raise DeployError(f"ფაილი უკვე არსებობს და არ გადაიწერება: {dest}")
    tmp = dest.with_name(dest.name + ".partial")
    tmp.unlink(missing_ok=True)
    base = [gpg(), "--batch", "--no-tty", "--output", str(tmp), "--compress-algo", "none"]
    if spec.passphrase is not None:
        cmd = base + ["--pinentry-mode", "loopback", "--passphrase-fd", "0", "--symmetric", "--cipher-algo", "AES256",
                      "--s2k-digest-algo", "SHA512", "--s2k-count", "65011712", str(src)]
    else:
        cmd = base + ["--trust-model", "always", "--cipher-algo", "AES256"]
        for r in spec.recipients:
            cmd += ["--recipient", r]
        cmd += ["--encrypt", str(src)]
    old = os.umask(0o077)
    try:
        p = _run(cmd, spec.passphrase)
    finally:
        os.umask(old)
    if p.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise _fail("დაშიფვრა ვერ მოხერხდა", p)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    src.unlink(missing_ok=True)
    return dest


def decrypt(src: str | Path, dest: str | Path, passphrase: Optional[str] = None) -> Path:
    """Decrypt `src` into `dest` (never overwrites; owner-only).  Without a passphrase gpg asks for the private key itself (gpg-agent / pinentry)."""
    src, dest = Path(src), Path(dest)
    if dest.exists():
        raise DeployError(f"ფაილი უკვე არსებობს და არ გადაიწერება: {dest}")
    tmp = dest.with_name(dest.name + ".partial")
    tmp.unlink(missing_ok=True)
    old = os.umask(0o077)
    try:
        if passphrase is not None:
            p = _run([gpg(), "--batch", "--no-tty", "--pinentry-mode", "loopback", "--passphrase-fd", "0", "--output", str(tmp), "--decrypt", str(src)], passphrase)
        else:                                            # public-key copy: the agent may need to ask for the key's passphrase on the terminal
            p = subprocess.run([gpg(), "--output", str(tmp), "--decrypt", str(src)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=3600)
    except (OSError, subprocess.SubprocessError) as e:
        raise DeployError(f"gpg ვერ გაეშვა: {e}") from e
    finally:
        os.umask(old)
    if p.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise _fail("გაშიფვრა ვერ მოხერხდა (არასწორი პაროლი/გასაღები ან დაზიანებული ფაილი)", p)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    return dest


def looks_encrypted(path: str | Path) -> bool:
    return str(path).endswith(SUFFIX)


def check_packets(path: str | Path, recipients: list[str] | None = None) -> None:
    """Without decrypting: the file is a well-formed OpenPGP message with integrity protection (MDC / AEAD)."""
    p = _run([gpg(), "--batch", "--no-tty", "--list-only", "--list-packets", str(path)], None, timeout=300)
    text = (p.stdout or "") + (p.stderr or "")
    aead = "aead encrypted packet" in text                       # newer gpg: authenticated encryption packet
    classic = "encrypted data packet" in text                     # older gpg / compatibility mode: integrity via MDC
    if p.returncode != 0 or not (aead or classic):
        raise DeployError("დაშიფრული ფაილი არასწორია")
    if classic and not aead and "mdc_method" not in text:
        raise DeployError("დაშიფრულ ფაილს მთლიანობის დაცვა (MDC) არ აქვს")
    if recipients and "pubkey enc packet" not in text:
        raise DeployError("დაშიფრული ფაილი მიმღების გასაღებისთვის არ არის")


@contextlib.contextmanager
def decrypted(path: str | Path, passphrase: Optional[str] = None) -> Iterator[Path]:
    """Yield a plaintext copy of an encrypted backup in a private temporary directory that is removed afterwards."""
    d = Path(tempfile.mkdtemp(prefix="gisstack-dec-"))           # mkdtemp is 0700
    try:
        name = Path(path).name[: -len(SUFFIX)] if looks_encrypted(path) else Path(path).name
        yield decrypt(path, d / name, passphrase)
    finally:
        shutil.rmtree(d, ignore_errors=True)
