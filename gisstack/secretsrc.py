"""Where secrets may come from besides the saved config (audit S-05/A-05: no secrets in config files or command lines).

A *provider* answers `get(name)` for names such as ``postgres.password``.  Built in: environment variables
(``GISSTACK_POSTGRES_PASSWORD``) and secret files (``GISSTACK_POSTGRES_PASSWORD_FILE`` -> path; the file must not be readable
by group/others).  Adapters for Vault / SOPS / a cloud secret manager can be registered with `register()` later; nothing else in
the code base needs to change.  Precedence in `config.resolve`: value in the config > provider > saved/generated secret."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Protocol

MAX_SECRET_FILE = 4096
NAMES = ("postgres.password", "geoserver.admin_password", "tomcat.manager_password")


class SecretError(ValueError):
    pass


class SecretProvider(Protocol):
    def get(self, name: str) -> Optional[str]: ...


def env_name(name: str) -> str:
    return "GISSTACK_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


class EnvProvider:
    def get(self, name: str) -> Optional[str]:
        v = os.environ.get(env_name(name))
        return v if v else None


class FileProvider:
    """GISSTACK_<NAME>_FILE=/path/to/file.  Refuses files that other users can read (like ssh does for keys)."""

    def get(self, name: str) -> Optional[str]:
        path = os.environ.get(env_name(name) + "_FILE")
        if not path:
            return None
        p = Path(path)
        try:
            st = p.stat()
        except OSError as e:
            raise SecretError(f"{env_name(name)}_FILE: {path}: {e.strerror}") from e
        if not p.is_file():
            raise SecretError(f"{env_name(name)}_FILE: not a regular file")
        if hasattr(os, "geteuid") and st.st_mode & 0o077:
            raise SecretError(f"{path}: readable by group/others (chmod 600 it)")
        if st.st_size > MAX_SECRET_FILE:
            raise SecretError(f"{path}: too large for a secret")
        text = p.read_text(encoding="utf-8").strip("\r\n")
        return text or None


_providers: list[SecretProvider] = [EnvProvider(), FileProvider()]


def register(provider: SecretProvider, first: bool = False) -> None:
    _providers.insert(0, provider) if first else _providers.append(provider)


def lookup(name: str) -> Optional[str]:
    """First provider that has a value for `name`; None if nobody does."""
    for p in _providers:
        v = p.get(name)
        if v:
            return v
    return None
