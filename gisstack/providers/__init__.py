"""Providers: native (systemd), Compose (docker/podman) and Kubernetes behind one interface."""
from __future__ import annotations

from .. import plugins
from ..core.errors import DeployError
from . import compose, kubernetes, native
from .base import Provider
from .common import TARGETS

_REGISTRY = {"native": Provider("native", native), "compose": Provider("compose", compose), "k8s": Provider("k8s", kubernetes)}


def get(target: str) -> Provider:
    if target in _REGISTRY:
        return _REGISTRY[target]
    for plug in plugins.get("service_provider"):                      # targets added by plugins never shadow the built-in ones
        if plug.name == target:
            prov = plugins.safe_call(plug, "provider")
            if prov is not None:
                return prov  # type: ignore[no-any-return]
    raise DeployError("უცნობი სამიზნე")


__all__ = ["Provider", "TARGETS", "get", "compose", "kubernetes", "native"]
