"""Providers: native (systemd), Compose (docker/podman) and Kubernetes behind one interface."""
from __future__ import annotations

from ..core.errors import DeployError
from . import compose, kubernetes, native
from .base import Provider
from .common import TARGETS

_REGISTRY = {"native": Provider("native", native), "compose": Provider("compose", compose), "k8s": Provider("k8s", kubernetes)}


def get(target: str) -> Provider:
    try:
        return _REGISTRY[target]
    except KeyError:
        raise DeployError("უცნობი სამიზნე") from None


__all__ = ["Provider", "TARGETS", "get", "compose", "kubernetes", "native"]
