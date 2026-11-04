"""Configuration: defaults, validation and dependency resolution."""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Callable

from . import catalog, fsutil, secretsrc, validation

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9_.\-]{8,64}$")
_DOMAIN_RE = re.compile(r"^(localhost|[a-z0-9]([a-z0-9.-]*[a-z0-9])?)(:\d{2,5})?$", re.I)


def _secrets_path(project: str) -> Path:
    home = Path(os.environ.get("GISSTACK_HOME") or (Path.home() / ".gisstack"))
    return home / "secrets" / f"{project}.json"


SCHEMA_VERSION = 1
# from_version -> function(raw config) -> raw config of the next version.  Add an entry whenever a key is renamed, moved or
# changes meaning; old files keep working and new files are refused by older programs instead of being misread.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    0: lambda raw: raw,          # v0 = files written before schema_version existed; same layout
}


def migrate(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Bring a user config up to SCHEMA_VERSION.  Returns (config, notes).  A file from a newer gisstack is refused."""
    ver = raw.get("schema_version", 0)
    if isinstance(ver, bool) or not isinstance(ver, int) or ver < 0:
        raise ConfigError("schema_version: მთელი რიცხვი უნდა იყოს")
    if ver > SCHEMA_VERSION:
        raise ConfigError(f"schema_version {ver} > {SCHEMA_VERSION}: კონფიგურაცია უფრო ახალი gisstack-ისთვისაა, განაახლე პროგრამა")
    notes: list[str] = []
    cur, start = dict(raw), ver
    while ver < SCHEMA_VERSION:
        cur = dict(MIGRATIONS[ver](cur))
        ver += 1
        cur["schema_version"] = ver
    if start and start < SCHEMA_VERSION:
        notes.append(f"schema_version {start} -> {SCHEMA_VERSION}: კონფიგურაცია განახლდა")
    cur["schema_version"] = SCHEMA_VERSION
    return cur, notes


def store_secret(project: str, name: str, value: str) -> None:
    """Remember a (rotated) secret for `project` so later generations reuse it instead of the old one."""
    path = _secrets_path(project)
    try:
        saved = {k: v for k, v in json.loads(path.read_text()).items() if isinstance(v, str)}
    except (OSError, ValueError):
        saved = {}
    saved[name] = value
    fsutil.private_dir(path.parent)
    fsutil.write_private(path, json.dumps(saved))


def default_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project": "gisstack",
        "domain": "localhost",
        "email": "",
        "targets": ["native", "compose"],
        "deploy_target": "compose",
        "runtime": "docker",  # docker | podman
        "components": {c["id"]: c["default"] for c in catalog.COMPONENTS},
        "tls": "internal",
        "postgres": {
            "version": "17",
            "db": "gis",
            "user": "gis",
            "password": "",
            "extensions": [e["id"] for e in catalog.EXTENSIONS if e["default"]],
            "ram_gb": 8,
            "max_connections": 100,
            "remote_access": False,
            "allowed_cidr": "10.0.0.0/8",
            "port": 5432,
        },
        "tomcat": {"major": "11", "heap_mb": 1024, "port": 8080, "manager_password": ""},
        "geoserver": {
            "channel": "3",
            "heap_mb": 2048,
            "admin_user": "admin",
            "admin_password": "",
            "version": "",   # empty = catalog default
            "image": "",
            "extensions": [],   # e.g. ["vectortiles", "css", "wps"]
        },
        "qgis": {"projects_dir": "/srv/qgis/projects", "processes": 4},
        "mapproxy": {
            "grids": ["GLOBAL_WEBMERCATOR"],
            "cache_type": "file",
            "format": "image/png",
            "meta_size": [4, 4],
            "meta_buffer": 80,
            "concurrent_tile_creators": 2,
            "workers": 4,
            "demo": True,          # /mapproxy/demo/ page; switch off in production
            "upstream_basemap": "none",
            "layers": [
                {"name": "example", "title": "Example layer",
                 "source": "geoserver", "wms_layers": "workspace:layer"},
            ],
            "seed": {"enabled": False, "levels": [0, 10],
                     "bbox": [39.9, 41.0, 46.8, 43.7]},
        },
        "webmap": {
            "title_ka": "ჩემი რუკა",
            "title_en": "My map",
            "default_lang": "ka",
            "center": [42.0, 43.5],     # lat, lon  (Georgia)
            "zoom": 7,
            "basemap": "osm",
            "basemap_url": "",
            "bounds": [[40.9, 39.9], [43.7, 46.8]],
        },
        "k8s": {"namespace": "gisstack", "storage_class": "", "pg_storage": "20Gi",
                "gs_storage": "5Gi", "mp_storage": "20Gi",
                "service_type": "LoadBalancer",
                "postgis_image": "", "mapproxy_image": ""},
        "ssh": {"host": "", "port": 22, "user": "root", "key": ""},
    }


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class ConfigError(ValueError):
    pass


def resolve(user_cfg: dict[str, Any] | None, *, generate_secrets: bool = True,
            persist_secrets: bool = False) -> dict[str, Any]:
    """Merge user input over defaults, validate, apply dependencies, fill secrets.

    persist_secrets: remember generated passwords per project (~/.gisstack/secrets/<project>.json, mode 600)
    and reuse them, so re-deploying or cleaning the same project does not rotate them on every run."""
    template = default_config()
    if user_cfg is not None and not isinstance(user_cfg, dict):
        raise ConfigError("config: must be an object")
    user_cfg, migration_notes = migrate(user_cfg or {})
    cfg = _merge(template, user_cfg)
    shape = validation.check_shape(cfg, template)
    if shape:                                 # a malformed file must produce a clear error, never a traceback
        raise ConfigError("\n".join(shape))
    for name in secretsrc.NAMES:          # secrets supplied through the environment / a secret file, not through the config
        sec, key = name.split(".")
        if not cfg[sec].get(key):
            try:
                provided = secretsrc.lookup(name)
            except secretsrc.SecretError as e:
                raise ConfigError(str(e)) from e
            if provided:
                cfg[sec][key] = provided
    errors: list[str] = []
    v_errors, v_warnings = validation.validate(cfg, template, [b["id"] for b in catalog.BASEMAPS])
    errors.extend(dict.fromkeys(v_errors))

    if not _NAME_RE.match(cfg["project"]):
        errors.append("project: მხოლოდ a-z, 0-9, _ (პატარა ასოებით დაწყებული)")
    if not _DOMAIN_RE.match(str(cfg["domain"])):
        errors.append("domain: არასწორი დომენი")
    if cfg["tls"] == "auto" and cfg["domain"] in ("localhost", "127.0.0.1"):
        errors.append("tls=auto საჭიროებს რეალურ დომენს; localhost-ისთვის აირჩიე internal")
    for t in cfg["targets"]:
        if t not in {x["id"] for x in catalog.TARGETS}:
            errors.append(f"targets: უცნობი სამიზნე {t}")
    if cfg["deploy_target"] not in cfg["targets"]:
        cfg["targets"] = list(dict.fromkeys(cfg["targets"] + [cfg["deploy_target"]]))
    if cfg["runtime"] not in catalog.RUNTIMES:
        errors.append("runtime: docker ან podman")

    pg = cfg["postgres"]
    for key in ("db", "user"):
        if not _NAME_RE.match(str(pg[key])):
            errors.append(f"postgres.{key}: მხოლოდ a-z, 0-9, _")
    if str(pg["version"]) not in catalog.POSTGRES_VERSIONS:
        errors.append("postgres.version: მხარდაუჭერელი ვერსია")
    pg["version"] = str(pg["version"])
    known_ext = {e["id"] for e in catalog.EXTENSIONS}
    bad = [e for e in pg["extensions"] if e not in known_ext]
    if bad:
        errors.append(f"postgres.extensions: უცნობი {bad}")
    if "postgis" not in pg["extensions"]:
        pg["extensions"].insert(0, "postgis")
    # raster/topology/tiger depend on postgis; keep order stable (postgis first)
    pg["extensions"] = sorted(dict.fromkeys(pg["extensions"]),
                              key=lambda e: (e != "postgis", e))

    comps = cfg["components"]
    for cid in catalog.component_ids():
        comps[cid] = bool(comps.get(cid, False))

    gs_channel = str(cfg["geoserver"]["channel"])
    if gs_channel not in catalog.GEOSERVER_CHANNELS:
        errors.append("geoserver.channel: 3 ან 2")
        gs_channel = "3"
    ch = catalog.GEOSERVER_CHANNELS[gs_channel]
    gs = cfg["geoserver"]
    gs["channel"] = gs_channel
    gs["version"] = gs["version"] or ch["version"]
    gs["image"] = gs["image"] or ch["image"]
    gs["java"] = ch["java"]

    # GeoServer runs inside Tomcat in native mode, so the Tomcat major is
    # dictated by the GeoServer channel (3 -> Tomcat 11, 2 -> Tomcat 9). The
    # native generator adds Tomcat automatically; Compose/K8s use GeoServer's
    # own image and only add a separate Tomcat if it was ticked explicitly.
    if comps["geoserver"]:
        cfg["tomcat"]["major"] = ch["tomcat_major"]
    if str(cfg["tomcat"]["major"]) not in catalog.TOMCAT_MAJORS:
        errors.append("tomcat.major: 11, 10.1 ან 9")
    cfg["tomcat"]["major"] = str(cfg["tomcat"]["major"])
    gs["admin_user"] = "admin"   # GeoServer's built-in superuser; password is what we set
    bad_ext = [e for e in gs["extensions"] if e not in catalog.GEOSERVER_EXTENSIONS]
    if bad_ext:
        errors.append(f"geoserver.extensions: უცნობი {bad_ext}")

    # Passwords end up in shell, YAML, SQL and XML; keep the alphabet safe everywhere.
    for label, val in (("postgres.password", pg["password"]),
                       ("geoserver.admin_password", gs["admin_password"]),
                       ("tomcat.manager_password", cfg["tomcat"]["manager_password"])):
        if val and not _SECRET_RE.match(str(val)):
            errors.append(f"{label}: 8-64 სიმბოლო, მხოლოდ A-Z a-z 0-9 _ . -")

    meta_warnings: list[str] = list(v_warnings) + migration_notes
    if not comps["caddy"] and any(comps[c] for c in ("geoserver", "mapproxy", "webmap", "qgis_server")):
        meta_warnings.append("Caddy გამორთულია: სერვისები პირდაპირ პორტებზე იქნება ხელმისაწვდომი")
    if comps["geoserver"] and comps["tomcat"] and "native" not in cfg["targets"]:
        meta_warnings.append(
            "Compose/K8s-ში GeoServer თავის image-შია (Tomcat-ით); "
            "ცალკე Tomcat მხოლოდ თქვენი WAR-ებისთვის დაემატება.")
    if "k8s" in cfg["targets"] and set(pg["extensions"]) & {"pgrouting", "pointcloud", "ogr_fdw", "h3"}:
        meta_warnings.append(
            "K8s: pgRouting/pointcloud/ogr_fdw/H3 საჭიროებს საკუთარ PostGIS image-ს "
            "(compose/postgis/Dockerfile) და k8s.postgis_image-ის მითითებას.")

    mp = cfg["mapproxy"]
    known_grids = {g["id"] for g in catalog.GRIDS}
    if any(g not in known_grids for g in mp["grids"]):
        errors.append("mapproxy.grids: უცნობი grid")
    if not mp["grids"] or ((comps["webmap"] or comps["mobile_templates"]) and "GLOBAL_WEBMERCATOR" not in mp["grids"]):
        mp["grids"] = ["GLOBAL_WEBMERCATOR"] + [g for g in mp["grids"] if g != "GLOBAL_WEBMERCATOR"]
    if mp["cache_type"] not in catalog.CACHE_TYPES:
        errors.append("mapproxy.cache_type")
    if mp["format"] not in catalog.TILE_FORMATS:
        errors.append("mapproxy.format")
    for lyr in mp["layers"]:
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", str(lyr.get("name", ""))):
            errors.append(f"mapproxy layer name არასწორია: {lyr.get('name')!r}")

    if errors:
        raise ConfigError("\n".join(errors))

    if generate_secrets:
        store = _secrets_path(cfg["project"]) if persist_secrets else None
        saved: dict[str, str] = {}
        if store is not None and store.is_file():
            try:
                saved = {k: v for k, v in json.loads(store.read_text()).items() if isinstance(v, str)}
            except (OSError, ValueError):
                saved = {}
        before = dict(saved)

        def fill(d: dict, k: str, name: str, n: int = 18) -> None:
            if d.get(k):
                return
            if not saved.get(name):
                saved[name] = secrets.token_urlsafe(n)
            d[k] = saved[name]
        fill(pg, "password", "postgres.password")
        fill(cfg["geoserver"], "admin_password", "geoserver.admin_password")
        fill(cfg["tomcat"], "manager_password", "tomcat.manager_password")
        if store is not None and saved != before:
            fsutil.private_dir(store.parent)
            fsutil.write_private(store, json.dumps(saved))

    cfg["_meta"] = {"warnings": meta_warnings}
    return cfg


def selected(cfg: dict[str, Any]) -> list[str]:
    return [k for k, v in cfg["components"].items() if v]
