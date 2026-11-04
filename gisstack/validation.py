"""Strict validation of a gisstack configuration.

Configuration values end up in root shell scripts, `stack.env`, a Caddyfile, `pg_hba.conf`, systemd units and
Compose / Kubernetes YAML.  Validation is therefore the first security boundary: every field has a type, a range
or a pattern, control characters are rejected everywhere, and unknown keys are reported (typos are no longer
silently ignored).  The templates additionally quote every value for the context it is written into."""
from __future__ import annotations

import ipaddress
import os
import re
from typing import Any, Callable, Optional, Tuple

# Environment switch for unknown keys: "warn" (default; they are reported, not fatal) or "error".
STRICT_ENV = "GISSTACK_STRICT_CONFIG"

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,24}$")
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,199}(?:@sha256:[0-9a-f]{64})?$")
_VERSION = re.compile(r"^\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.]{1,20})?$")
_PATH = re.compile(r"^/[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)*$")
_URL = re.compile(r"^https?://[A-Za-z0-9._~:/?#\[\]@!&'()*+,;=%{}-]{1,500}$")
_K8S_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_K8S_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")
_K8S_QTY = re.compile(r"^[1-9][0-9]{0,5}(?:Mi|Gi|Ti)$")
_WMS_LAYERS = re.compile(r"^[A-Za-z0-9_.:,/ -]{0,300}$")
_QGIS_MAP = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

Check = Callable[[Any], Tuple[Any, Optional[str]]]      # value -> (normalised value, error or None); typing aliases keep Python 3.9 working


def _int(lo: int, hi: int) -> Check:
    def f(v: Any) -> tuple[Any, str | None]:
        if isinstance(v, bool):
            return v, "must be a number"
        if isinstance(v, str) and re.fullmatch(r"-?\d{1,12}", v.strip()):
            v = int(v.strip())
        if not isinstance(v, int):
            return v, "must be a whole number"
        return v, None if lo <= v <= hi else f"must be between {lo} and {hi}"
    return f


def _float(lo: float, hi: float) -> Check:
    def f(v: Any) -> tuple[Any, str | None]:
        if isinstance(v, bool):
            return v, "must be a number"
        try:
            x = float(v)
        except (TypeError, ValueError):
            return v, "must be a number"
        if x != x or x in (float("inf"), float("-inf")):
            return v, "must be a finite number"
        return x, None if lo <= x <= hi else f"must be between {lo} and {hi}"
    return f


def _bool(v: Any) -> tuple[Any, str | None]:
    return v, None if isinstance(v, bool) else "must be true or false"


def _text(maxlen: int = 200, pattern: re.Pattern[str] | None = None, empty: bool = True) -> Check:
    def f(v: Any) -> tuple[Any, str | None]:
        if not isinstance(v, str):
            return v, "must be text"
        if not v:
            return v, None if empty else "must not be empty"
        if len(v) > maxlen:
            return v, f"is longer than {maxlen} characters"
        if _CTRL.search(v):
            return v, "contains control characters (newline, NUL, ...)"
        if pattern is not None and not pattern.match(v):
            return v, "has an invalid format"
        return v, None
    return f


def _enum(*allowed: str) -> Check:
    def f(v: Any) -> tuple[Any, str | None]:
        return v, None if isinstance(v, str) and v in allowed else f"must be one of: {', '.join(allowed)}"
    return f


def _cidr(v: Any) -> tuple[Any, str | None]:
    if not isinstance(v, str) or _CTRL.search(v) or len(v) > 50 or " " in v:
        return v, "must be a CIDR such as 10.0.0.0/8"
    try:
        ipaddress.ip_network(v, strict=False)
    except ValueError:
        return v, "must be a CIDR such as 10.0.0.0/8"
    return v, None


def _path(v: Any) -> tuple[Any, str | None]:
    if not isinstance(v, str) or not _PATH.match(v) or len(v) > 200 or ".." in v.split("/"):
        return v, "must be an absolute path (letters, digits, _ . - @ + and /; no '..')"
    return v, None


def _list_of(item: Check, maxlen: int = 100) -> Check:
    def f(v: Any) -> tuple[Any, str | None]:
        if not isinstance(v, list):
            return v, "must be a list"
        if len(v) > maxlen:
            return v, f"has more than {maxlen} items"
        out = []
        for i, x in enumerate(v):
            nx, err = item(x)
            if err:
                return v, f"item {i + 1} {err}"
            out.append(nx)
        return out, None
    return f


def _pair(lo: Check, hi: Check) -> Check:
    def f(v: Any) -> tuple[Any, str | None]:
        if not isinstance(v, list) or len(v) != 2:
            return v, "must be a list of two values"
        a, e1 = lo(v[0])
        b, e2 = hi(v[1])
        return ([a, b], None) if not (e1 or e2) else (v, e1 or e2)
    return f


def _bbox(v: Any) -> tuple[Any, str | None]:
    if not isinstance(v, list) or len(v) != 4:
        return v, "must be [west, south, east, north]"
    chk = [_float(-180, 180), _float(-90, 90), _float(-180, 180), _float(-90, 90)]
    out = []
    for c, x in zip(chk, v):
        nx, err = c(x)
        if err:
            return v, err
        out.append(nx)
    if out[0] >= out[2] or out[1] >= out[3]:
        return v, "west < east and south < north are required"
    return out, None


def _bounds(v: Any) -> tuple[Any, str | None]:
    if not isinstance(v, list) or len(v) != 2:
        return v, "must be [[south, west], [north, east]]"
    p = _pair(_float(-90, 90), _float(-180, 180))
    out = []
    for x in v:
        nx, err = p(x)
        if err:
            return v, err
        out.append(nx)
    return out, None


def _levels(v: Any) -> tuple[Any, str | None]:
    out, err = _pair(_int(0, 25), _int(0, 25))(v)
    if err:
        return v, err
    return (out, None) if out[0] <= out[1] else (v, "from must not be greater than to")


# ---------------------------------------------------------------- schema (path -> check)
LAYER_KEYS = {"name", "title", "title_ka", "source", "wms_layers", "url", "qgis_map"}


def _layer(v: Any) -> tuple[Any, str | None]:
    if not isinstance(v, dict):
        return v, "must be an object"
    out = dict(v)
    checks: dict[str, Check] = {
        "name": _text(60, re.compile(r"^[A-Za-z][A-Za-z0-9_]*$"), empty=False),
        "title": _text(200), "title_ka": _text(200),
        "source": _enum("geoserver", "qgis", "wms", "tiles"),
        "wms_layers": _text(300, _WMS_LAYERS),
        "url": _text(500, _URL),
        "qgis_map": _text(100, _QGIS_MAP),
    }
    for k, c in checks.items():
        if k in out:
            nv, err = c(out[k])
            if err:
                return v, f"{k} {err}"
            out[k] = nv
    return out, None


def schema(basemaps: list[str]) -> dict[str, Check]:
    t = _text
    return {
        "email": lambda v: (v, None) if v == "" else t(254, _EMAIL)(v),
        "postgres.ram_gb": _int(1, 4096), "postgres.max_connections": _int(20, 10000),
        "postgres.remote_access": _bool, "postgres.allowed_cidr": _cidr, "postgres.port": _int(1, 65535),
        "tomcat.heap_mb": _int(128, 262144), "tomcat.port": _int(1, 65535),
        "geoserver.heap_mb": _int(256, 262144),
        "geoserver.version": lambda v: (v, None) if v == "" else t(40, _VERSION)(v),
        "geoserver.image": lambda v: (v, None) if v == "" else t(300, _IMAGE)(v),
        "qgis.projects_dir": _path, "qgis.processes": _int(1, 64),
        "mapproxy.meta_size": _pair(_int(1, 32), _int(1, 32)), "mapproxy.meta_buffer": _int(0, 1024),
        "mapproxy.concurrent_tile_creators": _int(1, 64), "mapproxy.workers": _int(1, 64),
        "mapproxy.demo": _bool, "mapproxy.upstream_basemap": _enum("none", "osm"),
        "mapproxy.layers": _list_of(_layer, 500),
        "mapproxy.seed.enabled": _bool, "mapproxy.seed.levels": _levels, "mapproxy.seed.bbox": _bbox,
        "webmap.title_ka": t(120), "webmap.title_en": t(120), "webmap.default_lang": _enum("ka", "en"),
        "webmap.center": _pair(_float(-90, 90), _float(-180, 180)), "webmap.zoom": _int(1, 22),
        "webmap.basemap": _enum(*basemaps),
        "webmap.basemap_url": lambda v: (v, None) if v == "" else t(500, _URL)(v),
        "webmap.bounds": _bounds,
        "k8s.namespace": t(63, _K8S_LABEL, empty=False),
        "k8s.storage_class": lambda v: (v, None) if v == "" else t(253, _K8S_NAME)(v),
        "k8s.pg_storage": t(10, _K8S_QTY, empty=False), "k8s.gs_storage": t(10, _K8S_QTY, empty=False),
        "k8s.mp_storage": t(10, _K8S_QTY, empty=False),
        "k8s.service_type": _enum("LoadBalancer", "NodePort", "ClusterIP"),
        "k8s.postgis_image": lambda v: (v, None) if v == "" else t(300, _IMAGE)(v),
        "k8s.mapproxy_image": lambda v: (v, None) if v == "" else t(300, _IMAGE)(v),
    }


def _get(d: dict[str, Any], path: str) -> tuple[bool, Any]:
    cur: Any = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return False, None
        cur = cur[k]
    return True, cur


def _set(d: dict[str, Any], path: str, value: Any) -> None:
    cur = d
    for k in path.split(".")[:-1]:
        cur = cur[k]
    cur[path.split(".")[-1]] = value


def check_shape(cfg: Any, template: dict[str, Any], path: str = "") -> list[str]:
    """Sections must be objects and scalars must not be replaced by objects (prevents crashes on malformed files)."""
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return [f"{path or 'config'}: must be an object"]
    for k, tv in template.items():
        if k not in cfg:
            continue
        v = cfg[k]
        here = f"{path}{k}"
        if isinstance(tv, dict):
            if not isinstance(v, dict):
                errors.append(f"{here}: must be an object")
            else:
                errors += check_shape(v, tv, here + ".")
        elif isinstance(tv, list) and not isinstance(v, list):
            errors.append(f"{here}: must be a list")
        elif isinstance(tv, bool) and not isinstance(v, bool):
            errors.append(f"{here}: must be true or false")
        elif isinstance(tv, str) and not isinstance(v, str):
            errors.append(f"{here}: must be text")
        elif isinstance(tv, (int, float)) and not isinstance(tv, bool) and (isinstance(v, (dict, list, bool)) or v is None):
            errors.append(f"{here}: must be a number")
    return errors


def unknown_keys(cfg: dict[str, Any], template: dict[str, Any], path: str = "") -> list[str]:
    out: list[str] = []
    for k, v in cfg.items():
        if k.startswith("_"):
            continue
        here = f"{path}{k}"
        if k not in template:
            out.append(here)
        elif isinstance(template[k], dict) and isinstance(v, dict):
            out += unknown_keys(v, template[k], here + ".")
    return out


def _walk_strings(v: Any, path: str, out: list[str]) -> None:
    if isinstance(v, str):
        if _CTRL.search(v):
            out.append(f"{path}: contains control characters (newline, NUL, ...)")
    elif isinstance(v, dict):
        for k, x in v.items():
            _walk_strings(x, f"{path}.{k}" if path else str(k), out)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _walk_strings(x, f"{path}[{i}]", out)


def validate(cfg: dict[str, Any], template: dict[str, Any], basemaps: list[str],
             strict: bool | None = None) -> tuple[list[str], list[str]]:
    """Validate and normalise `cfg` in place.  Returns (errors, warnings)."""
    errors: list[str] = check_shape(cfg, template)
    if errors:
        return errors, []
    _walk_strings(cfg, "", errors)
    for path, chk in schema(basemaps).items():
        present, val = _get(cfg, path)
        if not present:
            continue
        nv, err = chk(val)
        if err:
            errors.append(f"{path}: {err}")
        else:
            _set(cfg, path, nv)
    for i, layer in enumerate(cfg.get("mapproxy", {}).get("layers", [])):
        if isinstance(layer, dict):
            for k in layer:
                if k not in LAYER_KEYS:
                    errors.append(f"mapproxy.layers[{i + 1}].{k}: unknown key")
    warnings: list[str] = []
    unknown = unknown_keys(cfg, template)
    if unknown:
        msg = "unknown configuration keys (ignored; check for typos): " + ", ".join(unknown)
        if strict is None:
            strict = os.environ.get(STRICT_ENV, "warn").lower() == "error"
        (errors if strict else warnings).append(msg)
    return errors, warnings
