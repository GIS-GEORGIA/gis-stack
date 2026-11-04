"""The plugin contract (version 1).

A plugin is a Python class (or a ready instance) published through an entry point of the installed distribution:

    [project.entry-points."gisstack.monitoring_providers"]
    my_pager = "my_pkg.pager:PagerNotifier"

Entry-point groups and the base class each one must derive from are listed in KINDS.  The contract is versioned by API_VERSION:
a plugin declares `api_version = 1`; a different major version is refused with a clear message instead of failing later.
Adding optional methods keeps version 1; changing or removing a method bumps it.  This module imports nothing from gisstack
so plugin packages can depend on it freely."""
from __future__ import annotations

import abc
from pathlib import Path
from typing import Any, Optional

API_VERSION = 1


class Plugin(abc.ABC):  # noqa: B024 - the abstract methods live in the kinds below
    """Common part: a unique `name` ([a-z0-9_-], 1-40) and the contract version it was written against."""
    name: str = ""
    api_version: int = API_VERSION
    description: str = ""


class ServiceProvider(Plugin):
    """Adds a deployment target next to native / compose / k8s.  `provider()` returns a gisstack.providers.base.Provider
    (see that class for the functions a target module implements)."""
    @abc.abstractmethod
    def provider(self) -> Any: ...


class MonitoringProvider(Plugin):
    """Adds notification channels to the monitor.  Each notifier has a `name` and `send(note: dict) -> None` (raise on failure);
    `note` carries rule, subject, level, state, message."""
    @abc.abstractmethod
    def notifiers(self) -> list[Any]: ...


class BackupProvider(Plugin):
    """Keeps a verified backup archive somewhere else (off-site).  `store` receives the local .tar.gz and the backup id and returns
    a human-readable location; it must not delete or alter the archive and must raise on failure."""
    @abc.abstractmethod
    def store(self, archive: Path, backup_id: str) -> str: ...


class AuthProvider(Plugin):
    """Recognises bearer tokens of another system (SSO, API gateway).  `authenticate(token)` returns {"name": str, "scope": "read"|"operate"|"admin"}
    for a valid token and None otherwise; it must be fast, side-effect free, and never raise for bad input."""
    @abc.abstractmethod
    def authenticate(self, token: str) -> Optional[dict[str, str]]: ...


class DataSource(Plugin):
    """Describes extra spatial data the stack can serve (a WMS/WFS endpoint, a database, a file store).  `layers()` returns
    [{"id", "title", "kind"}] and is exposed read-only at /api/v1/datasources."""
    @abc.abstractmethod
    def layers(self) -> list[dict[str, str]]: ...


# kind -> (entry-point group, required base class)
KINDS: dict[str, tuple[str, type[Plugin]]] = {
    "service_provider": ("gisstack.service_providers", ServiceProvider),
    "monitoring_provider": ("gisstack.monitoring_providers", MonitoringProvider),
    "backup_provider": ("gisstack.backup_providers", BackupProvider),
    "auth_provider": ("gisstack.auth_providers", AuthProvider),
    "data_source": ("gisstack.data_sources", DataSource),
}
