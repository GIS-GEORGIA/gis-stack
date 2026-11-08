"""MapProxy manager (Phase 6): configuration view and guarded edits (validated by MapProxy itself, backed up, restorable), cache statistics, seeding
with a tile-count guard, cache cleanup (dry-run by default), tile preview and health.

Notes the user must know (also in docs/MAPPROXY.md): comments in mapproxy.yaml are not kept by edits made here; `gisstack deploy/apply` regenerates the
file from the project, so an edit that should survive must also go into the project configuration."""
from __future__ import annotations

import copy
import ipaddress
import re
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Iterator, Optional

import yaml

from ..core.errors import DeployError
from .server import MAX_FILE, Check, Gateway, Tile

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
BUILTIN_GRIDS = ("GLOBAL_MERCATOR", "GLOBAL_WEBMERCATOR", "GLOBAL_GEODETIC")
FORMATS = {"png": "image/png", "jpeg": "image/jpeg", "mixed": "mixed"}
MAX_TILES_DEFAULT, MAX_TILES_HARD = 2_000_000, 50_000_000
OSM_MAX_LEVEL = 10                         # the generated config says: do not seed OpenStreetMap's public servers at scale
MANAGED_GRID_DIR = re.compile(r"^(?P<cache>.+?)_(?P<rest>[A-Za-z0-9._-]+)$")
SECRET_QUERY = re.compile(r"(token|key|password|passwd|secret|sig|signature|auth)", re.I)


class MapproxyError(DeployError):
    def __init__(self, message: str, code: str = "mapproxy", details: Optional[Any] = None) -> None:
        super().__init__(message)
        self.code, self.details = code, details


# --------------------------------------------------------------------------- parsing and summary
def load(text: Optional[str], what: str = "mapproxy.yaml") -> dict[str, Any]:
    if text is None:
        raise MapproxyError(f"mapproxy: {what} does not exist on the server", "not_found")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise MapproxyError(f"mapproxy: {what} is not valid YAML: {str(e)[:200]}", "config") from e
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise MapproxyError(f"mapproxy: {what} must be a mapping", "config")
    return doc


def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def redact_url(url: Any) -> str:
    """Hide credentials in userinfo and secret-looking query values."""
    if not isinstance(url, str):
        return ""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return "(unreadable url)"
    host = u.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{u.port}" if u.port else "")
    if u.username or u.password:
        netloc = "***@" + netloc
    q = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    query = "&".join(f"{k}={'***' if SECRET_QUERY.search(k) else v}" for k, v in q)
    return urllib.parse.urlunsplit((u.scheme, netloc, u.path, query, ""))


def summarize(conf: dict[str, Any], seed: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    caches: dict[str, Any] = conf.get("caches") or {}
    sources: dict[str, Any] = conf.get("sources") or {}
    grids: dict[str, Any] = conf.get("grids") or {}
    layers = []
    for lay in conf.get("layers") or []:
        if isinstance(lay, dict) and lay.get("name"):
            layers.append({"name": str(lay["name"]), "title": str(lay.get("title", "")), "sources": [str(x) for x in (lay.get("sources") or [])]})
    out_caches = []
    for name, c in caches.items():
        c = c if isinstance(c, dict) else {}
        cache = _d(c.get("cache"))
        out_caches.append({"name": str(name), "grids": [str(g) for g in (c.get("grids") or [])], "sources": [str(x) for x in (c.get("sources") or [])],
                           "format": str(c.get("format", "image/png")), "type": str(cache.get("type", "file")), "meta_size": c.get("meta_size")})
    out_sources = []
    for name, src in sources.items():
        src = src if isinstance(src, dict) else {}
        req = _d(src.get("req"))
        headers = _d(_d(src.get("http")).get("headers"))
        out_sources.append({"name": str(name), "type": str(src.get("type", "")), "url": redact_url(src.get("url") or req.get("url")),
                            "layers": str(req.get("layers", "")), "grid": str(src.get("grid", "")), "header_names": sorted(str(k) for k in headers)})
    out_grids = [{"name": g, "builtin": True, "srs": {"GLOBAL_WEBMERCATOR": "EPSG:3857", "GLOBAL_MERCATOR": "EPSG:900913", "GLOBAL_GEODETIC": "EPSG:4326"}[g], "bbox": None}
                 for g in BUILTIN_GRIDS]
    for name, g in grids.items():
        g = g if isinstance(g, dict) else {}
        out_grids.append({"name": str(name), "builtin": False, "srs": str(g.get("srs", "")), "bbox": g.get("bbox")})
    seeds = [{"name": str(n), "caches": [str(c) for c in ((v or {}).get("caches") or [])], "levels": (v or {}).get("levels")}
             for n, v in ((seed or {}).get("seeds") or {}).items() if isinstance(v, dict)]
    base = ((conf.get("globals") or {}).get("cache") or {}).get("base_dir") if isinstance((conf.get("globals") or {}).get("cache"), dict) else None
    return {"services": sorted((conf.get("services") or {}).keys()), "layers": layers, "caches": out_caches, "sources": out_sources, "grids": out_grids,
            "seeds": seeds, "cache_dir": str(base) if base else "", "demo": "demo" in (conf.get("services") or {})}


def snapshot(gw: Gateway) -> dict[str, Any]:
    files = gw.read()
    conf = load(files.get("mapproxy.yaml"))
    seed = load(files["seed.yaml"], "seed.yaml") if files.get("seed.yaml") else None
    return {"config": summarize(conf, seed), "conf": conf, "seed": seed, "files": files}


# --------------------------------------------------------------------------- validation and edits
def validate(gw: Gateway) -> dict[str, Any]:
    files = gw.read()
    load(files.get("mapproxy.yaml"))
    chk = gw.apply(files["mapproxy.yaml"] or "", files.get("seed.yaml"), dry_run=True)
    return {"ok": chk.ok, "errors": chk.errors, "warnings": chk.warnings}


def dump(conf: dict[str, Any], note: str) -> str:
    body = yaml.safe_dump(conf, sort_keys=False, default_flow_style=False, allow_unicode=True, width=120)
    return f"# {note}\n# (comments of the previous version are not kept; gisstack deploy/apply regenerates this file from the project)\n{body}"


def check_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 1000:
        raise MapproxyError("mapproxy: invalid url", "bad_request")
    try:
        u = urllib.parse.urlsplit(url.replace("%(z)s", "0").replace("%(x)s", "0").replace("%(y)s", "0"))
    except ValueError as e:
        raise MapproxyError("mapproxy: invalid url", "bad_request") from e
    if u.scheme not in ("http", "https") or not u.hostname:
        raise MapproxyError("mapproxy: the source url must be http(s)", "bad_request")
    if u.username or u.password:
        raise MapproxyError("mapproxy: credentials do not belong in a url", "bad_request")
    try:
        ip = ipaddress.ip_address(u.hostname)
        if ip.is_link_local:
            raise MapproxyError("mapproxy: link-local addresses (cloud metadata) are not allowed as a source", "bad_request")
    except ValueError:
        if u.hostname.lower() in ("metadata.google.internal", "metadata"):
            raise MapproxyError("mapproxy: metadata hosts are not allowed as a source", "bad_request") from None
    return url


def known_grids(conf: dict[str, Any]) -> set[str]:
    return set(BUILTIN_GRIDS) | set((conf.get("grids") or {}).keys())


def add_layer(conf: dict[str, Any], p: dict[str, Any]) -> list[str]:
    name = p.get("name", "")
    if not NAME_RE.match(str(name)):
        raise MapproxyError("mapproxy: layer name must match [A-Za-z][A-Za-z0-9_]{0,40}", "bad_request")
    cache_name, src_name = f"{name}_cache", f"{name}_src"
    conf.setdefault("layers", [])
    conf.setdefault("caches", {})
    conf.setdefault("sources", {})
    if any(isinstance(layer, dict) and layer.get("name") == name for layer in conf["layers"]) or cache_name in conf["caches"] or src_name in conf["sources"]:
        raise MapproxyError(f"mapproxy: layer {name!r} already exists", "conflict")
    grids = list(p.get("grids") or ["GLOBAL_WEBMERCATOR"])
    unknown = [g for g in grids if g not in known_grids(conf)]
    if unknown:
        raise MapproxyError(f"mapproxy: unknown grid(s) {unknown}", "bad_request")
    fmt = FORMATS.get(p.get("format", "png"))
    if fmt is None:
        raise MapproxyError("mapproxy: format must be png, jpeg or mixed", "bad_request")
    kind = p.get("source")
    if kind == "wms":
        src: dict[str, Any] = {"type": "wms", "req": {"url": check_url(p.get("url", "")), "layers": _wms_layers(p.get("layers", ""))}}
        if fmt != "image/jpeg":
            src["req"]["transparent"] = True
    elif kind == "tile":
        src = {"type": "tile", "grid": p.get("grid") if p.get("grid") in known_grids(conf) else "GLOBAL_WEBMERCATOR", "url": check_url(p.get("url", ""))}
        if not all(t in src["url"] for t in ("%(z)s", "%(x)s", "%(y)s")):
            raise MapproxyError("mapproxy: a tile url needs %(z)s, %(x)s and %(y)s", "bad_request")
    else:
        raise MapproxyError("mapproxy: source must be wms or tile", "bad_request")
    cache: dict[str, Any] = {"grids": grids, "sources": [src_name], "format": fmt, "cache": {"type": "file", "directory_layout": "tms"}}
    if fmt == "mixed":
        cache["request_format"] = "image/png"
    conf["layers"].append({"name": name, "title": str(p.get("title") or name)[:120], "sources": [cache_name]})
    conf["caches"][cache_name] = cache
    conf["sources"][src_name] = src
    return [f"layer {name}", f"cache {cache_name}", f"source {src_name}"]


def _wms_layers(v: Any) -> str:
    if not isinstance(v, str) or not re.match(r"^[A-Za-z0-9_:.,-]{1,300}$", v):
        raise MapproxyError("mapproxy: wms layers must look like workspace:layer[,other]", "bad_request")
    return v


def remove_layer(conf: dict[str, Any], p: dict[str, Any]) -> list[str]:
    name = p.get("name")
    layers = conf.get("layers") or []
    lay = next((x for x in layers if isinstance(x, dict) and x.get("name") == name), None)
    if lay is None:
        raise MapproxyError(f"mapproxy: no layer {name!r}", "not_found")
    conf["layers"] = [x for x in layers if x is not lay]
    removed = [f"layer {name}"]
    still = {c for x in conf["layers"] if isinstance(x, dict) for c in (x.get("sources") or [])}
    for c in lay.get("sources") or []:
        if c in (conf.get("caches") or {}) and c not in still:
            srcs = (conf["caches"][c] or {}).get("sources") or []
            del conf["caches"][c]
            removed.append(f"cache {c}")
            used = {s for cc in conf["caches"].values() if isinstance(cc, dict) for s in (cc.get("sources") or [])}
            for s in srcs:
                if s in (conf.get("sources") or {}) and s not in used:
                    del conf["sources"][s]
                    removed.append(f"source {s}")
    return removed


def add_grid(conf: dict[str, Any], p: dict[str, Any]) -> list[str]:
    name, srs, bbox = p.get("name", ""), p.get("srs", ""), p.get("bbox")
    if not NAME_RE.match(str(name)) or name in BUILTIN_GRIDS or name in (conf.get("grids") or {}):
        raise MapproxyError("mapproxy: grid name invalid or already used", "bad_request" if not NAME_RE.match(str(name)) else "conflict")
    if not re.match(r"^EPSG:\d{4,6}$", str(srs)):
        raise MapproxyError("mapproxy: srs must look like EPSG:32638", "bad_request")
    try:
        b = [float(x) for x in (bbox or [])]
    except (TypeError, ValueError):
        b = []
    if len(b) != 4 or not (b[0] < b[2] and b[1] < b[3]):
        raise MapproxyError("mapproxy: bbox must be [minx, miny, maxx, maxy]", "bad_request")
    conf.setdefault("grids", {})[name] = {"srs": srs, "bbox": b, "bbox_srs": srs, "origin": "nw"}
    return [f"grid {name}"]


def remove_grid(conf: dict[str, Any], p: dict[str, Any]) -> list[str]:
    name = p.get("name")
    if name not in (conf.get("grids") or {}):
        raise MapproxyError(f"mapproxy: no custom grid {name!r}", "not_found")
    users = [c for c, v in (conf.get("caches") or {}).items() if isinstance(v, dict) and name in (v.get("grids") or [])]
    users += [s for s, v in (conf.get("sources") or {}).items() if isinstance(v, dict) and v.get("grid") == name]
    if users:
        raise MapproxyError(f"mapproxy: grid {name!r} is used by {', '.join(users)}", "conflict")
    del conf["grids"][name]
    return [f"grid {name}"]


OPS = {"add_layer": add_layer, "remove_layer": remove_layer, "add_grid": add_grid, "remove_grid": remove_grid}


def edit(gw: Gateway, op: str, params: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """Apply one structured change to the live configuration: validated by MapProxy, backed up, installed atomically (unless dry_run)."""
    if op not in OPS:
        raise MapproxyError(f"mapproxy: unknown operation {op!r}", "bad_request")
    snap = snapshot(gw)
    conf = copy.deepcopy(snap["conf"])
    changes = OPS[op](conf, params)
    text = dump(conf, "Edited by the gisstack MapProxy manager")
    chk = gw.apply(text, None, dry_run=dry_run)
    if not chk.ok:
        raise MapproxyError("mapproxy: MapProxy rejects the resulting configuration: " + "; ".join(chk.errors)[:600], "config", {"errors": chk.errors})
    return {"changes": changes, "dry_run": dry_run, "backup": chk.backup, "warnings": chk.warnings,
            "note": "applied; MapProxy reloads its configuration by itself" if not dry_run else "valid - nothing was changed"}


def backups(gw: Gateway) -> list[dict[str, Any]]:
    return gw.backups()


def restore(gw: Gateway, backup_id: str) -> dict[str, Any]:
    files = gw.read_backup(backup_id)
    if not files.get("mapproxy.yaml"):
        raise MapproxyError("mapproxy: that backup has no mapproxy.yaml", "not_found")
    text = files.get("mapproxy.yaml") or ""
    load(text)
    chk = gw.apply(text, files.get("seed.yaml"), dry_run=False)
    if not chk.ok:
        raise MapproxyError("mapproxy: the backup no longer validates: " + "; ".join(chk.errors)[:600], "config")
    return {"restored": backup_id, "backup_of_previous": chk.backup}


# --------------------------------------------------------------------------- cache statistics
def cache_stats(gw: Gateway, summary: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    summary = summary or snapshot(gw)["config"]
    raw = gw.stats()
    names = sorted((c["name"] for c in summary["caches"]), key=len, reverse=True)
    caches = {c["name"]: {"name": c["name"], "type": c["type"], "entries": [], "bytes": 0, "tiles": 0, "partial": False} for c in summary["caches"]}
    unmanaged = []
    for e in raw["entries"].values():
        owner = next((n for n in names if e["name"] == n or e["name"].startswith(n + "_") or e["name"].startswith(n + ".")), None)
        levels = [{"level": z, "tiles": n} for z, n in sorted(e["levels"].items())]
        item = {"entry": e["name"], "kind": e["kind"], "bytes": e["bytes"], "tiles": e["tiles"], "partial": e["partial"], "levels": levels}
        if owner is None:
            unmanaged.append(item)
            continue
        c = caches[owner]
        c["entries"].append(item)
        c["bytes"] += e["bytes"]
        c["tiles"] += e["tiles"] or 0
        c["partial"] = c["partial"] or e["partial"]
    return {"available": raw["available"], "cache_dir": summary["cache_dir"], "disk_total_kb": raw["disk_total_kb"], "disk_free_kb": raw["disk_free_kb"],
            "caches": list(caches.values()), "unmanaged": unmanaged, "total_bytes": sum(c["bytes"] for c in caches.values()) + sum(u["bytes"] for u in unmanaged)}


# --------------------------------------------------------------------------- seeding
def build_seed(conf: dict[str, Any], p: dict[str, Any]) -> tuple[str, list[str]]:
    caches = p.get("caches") or []
    have = conf.get("caches") or {}
    if not caches or any(c not in have for c in caches):
        raise MapproxyError("mapproxy: choose existing caches to seed", "bad_request")
    lo, hi = int(p.get("level_from", 0)), int(p.get("level_to", 0))
    if not (0 <= lo <= hi <= 20):
        raise MapproxyError("mapproxy: levels must satisfy 0 <= from <= to <= 20", "bad_request")
    try:
        bbox = [float(x) for x in p.get("bbox") or []]
    except (TypeError, ValueError):
        bbox = []
    if len(bbox) != 4 or not (bbox[0] < bbox[2] and bbox[1] < bbox[3]) or not (bbox[0] >= -180 and bbox[2] <= 180 and bbox[1] >= -90 and bbox[3] <= 90):
        raise MapproxyError("mapproxy: bbox must be [minx, miny, maxx, maxy] in EPSG:4326", "bad_request")
    grids = p.get("grids")
    for c in caches:
        allowed = (have[c] or {}).get("grids") or []
        if grids and any(g not in allowed for g in grids):
            raise MapproxyError(f"mapproxy: cache {c} does not use grid(s) {grids}", "bad_request")
        for s in (have[c] or {}).get("sources") or []:
            url = ((conf.get("sources") or {}).get(s) or {}).get("url", "")
            if isinstance(url, str) and "tile.openstreetmap.org" in url and hi > OSM_MAX_LEVEL:
                raise MapproxyError(f"mapproxy: {c} is fed by OpenStreetMap's public servers; levels above {OSM_MAX_LEVEL} are refused (usage policy)", "conflict")
    task: dict[str, Any] = {"caches": list(caches), "coverages": ["area"], "levels": {"from": lo, "to": hi}}
    if grids:
        task["grids"] = list(grids)
    # MapProxy's default re-fetches EVERY tile; an old reference time means "only what is missing", `refresh` means "all of it again"
    task["refresh_before"] = {"minutes": 0} if p.get("refresh") else {"time": "2000-01-01T00:00:00"}
    doc = {"seeds": {"gs_manual": task}, "coverages": {"area": {"bbox": bbox, "srs": "EPSG:4326"}}}
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False), list(caches)


_TOTAL = re.compile(r"\((\d+) tiles\)\s*$")


def estimate_tiles(lines: list[str]) -> Optional[int]:
    """Total from mapproxy-seed's dry run: the last progress line ends with '(N tiles)'."""
    for line in reversed(lines):
        m = _TOTAL.search(line.strip())
        if m:
            return int(m.group(1))
    return None


def seed(gw: Gateway, p: dict[str, Any], stop: Optional[threading.Event] = None) -> Iterator[str]:
    """Dry run always first (its count is the guard); with dry_run=True that is all.  Real seeding needs confirm=True."""
    snap = snapshot(gw)
    text, caches = build_seed(snap["conf"], p)
    dry = p.get("dry_run", True) is not False
    if not dry and p.get("confirm") is not True:
        raise MapproxyError("mapproxy: seeding writes many tiles and may load the upstream server: send confirm=true", "plan")
    limit = min(int(p.get("max_tiles") or MAX_TILES_DEFAULT), MAX_TILES_HARD)
    probe = gw.probe()
    if probe.get("seeding", 0) > 0:
        raise MapproxyError("mapproxy: a seeding run is already in progress on this server", "busy")
    if not dry and probe.get("seeding_known", True) is False:
        raise MapproxyError("mapproxy: pgrep (package procps) is required to make sure only one seeding runs; install it first", "conflict")
    yield f"== seed estimate: {', '.join(caches)} levels {p.get('level_from', 0)}-{p.get('level_to', 0)}"
    lines: list[str] = []
    for line in gw.seed(text, 1, True, stop):
        lines.append(line)
        yield line
    total = estimate_tiles(lines)
    if total is None:
        raise MapproxyError("mapproxy: could not read the tile estimate; refusing to seed blind", "upstream")
    yield f"== estimated tiles to create: {total}"
    if dry:
        return
    if total > limit:
        raise MapproxyError(f"mapproxy: {total} tiles exceed the limit of {limit}; narrow the area/levels or raise max_tiles (hard cap {MAX_TILES_HARD})", "conflict")
    yield "== seeding"
    yield from gw.seed(text, max(1, min(int(p.get("concurrency") or 2), 4)), False, stop)
    if stop is not None and stop.is_set():
        yield "== seeding cancelled (tiles created so far are kept)"
        return
    yield "== seeding finished"


# --------------------------------------------------------------------------- cleanup, preview, health
def cleanup(gw: Gateway, cache: str, older_than_days: Optional[int] = None, dry_run: bool = True) -> dict[str, Any]:
    """Delete the cached tiles of one cache (or only tiles older than N days).  Dry run by default: reports what would go."""
    snap = snapshot(gw)
    summary = snap["config"]
    if cache not in {c["name"] for c in summary["caches"]}:
        raise MapproxyError(f"mapproxy: no cache {cache!r} in the configuration", "not_found")
    raw = gw.stats()
    stats = cache_stats(gw, summary) if raw["available"] else {"caches": []}
    entries = [e["entry"] for c in stats["caches"] if c["name"] == cache for e in c["entries"]]
    if not entries:
        return {"cache": cache, "dry_run": dry_run, "entries": [], "files": 0, "bytes": 0, "note": "nothing cached"}
    res = gw.cleanup(entries, older_than_days, dry_run)
    done = [r for r in res if "skipped" not in r]
    return {"cache": cache, "dry_run": dry_run, "older_than_days": older_than_days, "entries": res, "files": sum(r["files"] for r in done), "bytes": sum(r["bytes"] for r in done)}


def tile_preview(gw: Gateway, layer: str, grid: str, z: int, x: int, y: int) -> Tile:
    snap = snapshot(gw)
    conf, summary = snap["conf"], snap["config"]
    lay = next((x_ for x_ in summary["layers"] if x_["name"] == layer), None)
    if lay is None:
        raise MapproxyError(f"mapproxy: no layer {layer!r}", "not_found")
    cache = next((c for c in summary["caches"] if c["name"] in lay["sources"]), None)
    if cache is None or grid not in cache["grids"]:
        raise MapproxyError(f"mapproxy: layer {layer!r} is not cached on grid {grid!r}", "bad_request")
    if not (0 <= int(z) <= 24) or not (0 <= int(x) < 2 ** 26) or not (0 <= int(y) < 2 ** 26):
        raise MapproxyError("mapproxy: tile coordinates out of range", "bad_request")
    ext = "jpeg" if cache["format"] == "image/jpeg" else "png"
    t = gw.tile(layer, grid, int(z), int(x), int(y), ext)
    if t.status != 200 or t.content_type not in ("image/png", "image/jpeg"):
        raise MapproxyError(f"mapproxy: the tile request answered HTTP {t.status} ({t.content_type or 'no content type'})", "upstream")
    del conf
    return t


def parse_capabilities(xml: bytes) -> list[str]:
    head = xml[:4096].lower()
    if not xml or b"<!doctype" in head or b"<!entity" in xml.lower():
        return []
    try:
        root = ET.fromstring(xml)  # noqa: S314  # nosec B314 - DTDs/entities are rejected above
    except ET.ParseError:
        return []
    names = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "Layer":
            ident = next((c.text for c in el if c.tag.rsplit("}", 1)[-1] == "Identifier" and c.text), None)
            if ident:
                names.append(ident.strip())
    return names


def health(gw: Gateway) -> dict[str, Any]:
    probe = gw.probe()
    findings: list[dict[str, str]] = []
    cfg_ok, cfg_errors, cfg_warn = None, [], []
    summary = None
    try:
        snap = snapshot(gw)
        summary = snap["config"]
        v = gw.apply(snap["files"]["mapproxy.yaml"] or "", snap["files"].get("seed.yaml"), dry_run=True)
        cfg_ok, cfg_errors, cfg_warn = v.ok, v.errors, v.warnings
    except DeployError as e:
        cfg_ok, cfg_errors = False, [str(e)]
    if not probe["installed"]:
        findings.append({"level": "error", "what": "install", "message": "MapProxy is not installed in the virtualenv"})
    if probe["unit"] not in ("active", "unknown"):
        findings.append({"level": "error", "what": "service", "message": f"gisstack-mapproxy.service is {probe['unit']}"})
    if probe["http"] != 200:
        findings.append({"level": "error", "what": "http", "message": f"the WMTS capabilities answered HTTP {probe['http'] or 'nothing'}"})
    if probe["writable"] == "no":
        findings.append({"level": "error", "what": "cache", "message": "the cache directory is not writable by the mapproxy user"})
    elif probe["writable"] == "missing":
        findings.append({"level": "warn", "what": "cache", "message": "the cache directory does not exist yet (created on first use)"})
    if probe["disk_total_kb"] and probe["disk_free_kb"] is not None and probe["disk_free_kb"] < 0.1 * probe["disk_total_kb"]:
        findings.append({"level": "warn", "what": "disk", "message": f"only {probe['disk_free_kb'] // 1024} MB free on the cache volume"})
    if cfg_ok is False:
        findings.append({"level": "error", "what": "config", "message": "; ".join(cfg_errors)[:300]})
    for w in cfg_warn:
        findings.append({"level": "warn", "what": "config", "message": w})
    if summary and summary["demo"]:
        findings.append({"level": "info", "what": "demo", "message": "the demo page is enabled (switch off in production)"})
    served = parse_capabilities(probe["caps"])
    if summary:
        missing = [lay["name"] for lay in summary["layers"] if not any(s == lay["name"] or s.startswith(lay["name"]) for s in served)]
        if served and missing:
            findings.append({"level": "warn", "what": "wmts", "message": f"configured but not advertised: {', '.join(missing)}"})
    return {"ok": not any(f["level"] == "error" for f in findings), "findings": findings, "service": probe["unit"], "http": probe["http"],
            "seeding": probe["seeding"] > 0, "layers_served": served, "config_valid": cfg_ok}


__all__ = ["MAX_FILE", "Check", "MapproxyError"]
