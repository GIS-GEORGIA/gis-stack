"""The provider interface (audit A-02): everything that differs between native, Compose and Kubernetes.

A provider is a small object over a module of plain functions; adding a target (or a feature that must behave per target) means
implementing this interface once instead of touching `ops`, `deploy` and `generate` separately."""
from __future__ import annotations

from types import ModuleType
from typing import Any, Callable


class Provider:
    def __init__(self, name: str, module: ModuleType) -> None:
        self.name = name
        self._m = module

    # --- bundle entry points
    @property
    def entry(self) -> str:
        """Script in the bundle that (re)installs: install.sh / up.sh / apply.sh."""
        return str(self._m.ENTRY)

    @property
    def clean_entry(self) -> str:
        return str(self._m.CLEAN_ENTRY)

    @property
    def needs_root(self) -> bool:
        """Do operations on this target run through sudo when the user is not root?"""
        return bool(self._m.ROOT)

    def clean_args(self, short: list[str], purge: bool, remove_postgres: bool, force_postgres: bool) -> list[str]:
        return list(self._m.clean_args(short, purge, remove_postgres, force_postgres))

    def apply_args(self, component: str) -> list[str]:
        return list(self._m.apply_args(component))

    # --- live operations
    def status_rows(self, cfg: dict[str, Any], ids: list[str], run: Callable[[str], str]) -> list[dict[str, Any]]:
        """Query the target through `run(script) -> stdout` and return one status row per program."""
        return list(self._m.fetch_status(cfg, ids, run))

    def control_script(self, cfg: dict[str, Any], action: str, order: list[str]) -> str:
        return str(self._m.control_script(cfg, action, order))

    def logs_script(self, cfg: dict[str, Any], comp: str, lines: int, follow: bool) -> str:
        return str(self._m.logs_script(cfg, comp, lines, follow))
