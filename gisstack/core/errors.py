"""One error hierarchy for the whole package (audit A-06).

Every error has a stable machine-readable `code` (used in API responses and logs) and a human message.  The old names
(`deploy.DeployError`, `config.ConfigError`, ...) are the very same classes, re-exported where they used to live."""
from __future__ import annotations


class GisStackError(RuntimeError):
    code = "error"


class ConfigError(GisStackError, ValueError):
    code = "config"


class SecretError(ConfigError):
    code = "secret"


class ProjectError(GisStackError, ValueError):
    code = "project"


class DeployError(GisStackError):
    code = "deploy"


class PlanError(DeployError):
    code = "plan"


class TransportError(DeployError):
    """A command could not be run, failed, or the connection broke."""
    code = "transport"


class RunnerError(TransportError):
    code = "runner"


class Timeout(RunnerError):
    """The command exceeded its deadline or produced no output for too long."""
    code = "timeout"


class HostKeyUnknown(DeployError):
    """An SSH server whose host key has never been trusted (ad-hoc connection, strict policy)."""
    code = "hostkey_unknown"


class HostKeyChanged(DeployError):
    """The server presents a different host key than the one that was trusted: refused, never silently accepted."""
    code = "hostkey_changed"


class Busy(GisStackError):
    """Another operation holds the lock."""
    code = "busy"


class UpdateError(GisStackError):
    code = "update"


def code_of(exc: BaseException) -> str:
    return exc.code if isinstance(exc, GisStackError) else "internal"
