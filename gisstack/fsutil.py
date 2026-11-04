"""Safe file creation for anything that may hold secrets.

Files are created with their final restrictive mode from the first byte (no write-then-chmod window), written
to a temporary name in the same directory and renamed into place, so a crash never leaves a half-written
secret file and a concurrent reader never sees loose permissions."""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def private_dir(path: str | Path, mode: int = 0o700) -> Path:
    """Create `path` (and parents) and make sure the directory itself is not readable by others.

    Only the leaf is tightened; an existing directory is tightened only when this user owns it."""
    p = Path(path)
    missing = [q for q in [p, *p.parents] if not q.exists()]
    for q in reversed(missing):                 # every directory we create is private, not just the leaf
        try:
            q.mkdir(mode=mode)
        except FileExistsError:
            pass
        else:
            os.chmod(q, mode)                   # mkdir's mode is filtered through the umask
    try:
        st = p.stat()
        if hasattr(os, "geteuid") and st.st_uid == os.geteuid() and (st.st_mode & 0o077) and mode & 0o077 == 0:
            p.chmod(mode)
    except OSError:
        pass
    return p


def write_file(path: str | Path, data: str | bytes, mode: int = 0o600, *, atomic: bool = True) -> Path:
    """Write `data` to `path` with `mode` set at creation time (never wider than `mode`, whatever the umask)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if not atomic:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.fchmod(fd, mode)
            os.write(fd, raw)
        finally:
            os.close(fd)
        return p
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")     # created 0600 by mkstemp
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return p


def write_private(path: str | Path, data: str | bytes) -> Path:
    """Owner-only file (0600)."""
    return write_file(path, data, 0o600)
