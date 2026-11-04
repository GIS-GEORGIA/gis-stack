"""Feature flags (roadmap rule: large features stay off until the owner accepts the phase).

GISSTACK_FEATURES=monitoring,prometheus  (comma list).  `all` switches every known flag on.  Unknown names are ignored so a newer
configuration never breaks an older program; the web GUI and the CLI ask `enabled("monitoring")` before exposing a feature."""
from __future__ import annotations

import os

KNOWN = ("dashboard", "geoserver_manager", "postgis_manager", "mapproxy_manager", "backups", "monitoring", "prometheus", "multi_server",
         "api", "plugins")


def enabled_set() -> frozenset[str]:
    raw = {x.strip().lower() for x in os.environ.get("GISSTACK_FEATURES", "").split(",") if x.strip()}
    return frozenset(KNOWN) if "all" in raw else frozenset(raw & set(KNOWN))


def enabled(name: str) -> bool:
    if name not in KNOWN:
        raise KeyError(f"unknown feature: {name}")
    return name in enabled_set()
