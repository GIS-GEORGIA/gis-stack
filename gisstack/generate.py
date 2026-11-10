"""Render the deployment bundle (native / compose / k8s) from a resolved config."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Iterable

import jinja2

from . import __version__, catalog, fsutil, pins

PKG_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PKG_DIR / "templates"
VENDOR_DIR = PKG_DIR / "vendor"       # leaflet etc. (filled by packaging/vendor.sh)

POSTGIS_TAGS = {"14": "3.5", "15": "3.5", "16": "3.5", "17": "3.5", "18": "3.6"}


def _env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,  # nosec B701  # noqa: S701 - the output is shell/YAML/Caddy text, never HTML; the quoting filters below protect each context
    )
    env.filters["tojson_c"] = lambda v: json.dumps(v, ensure_ascii=False)
    env.filters["shq"] = lambda s: "'" + str(s).replace("'", "'\"'\"'") + "'"      # a whole word, single-quoted
    env.filters["shdq"] = _shdq            # inside "double quotes"
    env.filters["shdw"] = _shdw            # inside "${VAR:-default word}"
    env.filters["yq"] = lambda s: json.dumps(str(s), ensure_ascii=False)  # YAML-safe string
    return env


def pg_tuning(pg: dict) -> dict[str, str]:
    ram = max(1, int(pg["ram_gb"])) * 1024
    maxc = max(20, int(pg["max_connections"]))
    return {
        "shared_buffers": f"{max(128, ram // 4)}MB",
        "effective_cache_size": f"{int(ram * 0.6)}MB",
        "work_mem": f"{max(4, (ram // 4) // maxc)}MB",
        "maintenance_work_mem": f"{min(2048, max(64, ram // 16))}MB",
        "max_connections": str(maxc),
    }


def pg_packages(cfg: dict, flavor: str) -> list[str]:
    """Extra OS packages needed for the selected extensions (flavor: deb|rpm)."""
    v = cfg["postgres"]["version"]
    out: list[str] = []
    for e in catalog.EXTENSIONS:
        if e["id"] in cfg["postgres"]["extensions"]:
            out += [p.format(v=v) for p in e[flavor]]  # type: ignore[attr-defined]  # catalog entries are plain dicts
    return list(dict.fromkeys(out))


def effective_components(cfg: dict[str, Any], target: str) -> dict[str, bool]:
    """Native GeoServer lives inside Tomcat, so Tomcat is implied there."""
    comps = dict(cfg["components"])
    if target == "native" and comps["geoserver"]:
        comps["tomcat"] = True
    return comps


def webmap_conf(cfg: dict[str, Any], comps: dict[str, bool], basemap_url: str, attr: str) -> dict[str, Any]:
    """Everything the browser / mobile clients need, with server-relative URLs."""
    wm, mp = cfg["webmap"], cfg["mapproxy"]
    layers = []
    for l in mp["layers"]:
        title_en = l["title"]
        title_ka = l.get("title_ka") or title_en
        if comps["mapproxy"]:
            ext = "jpeg" if mp["format"] == "image/jpeg" else "png"
            layers.append({
                "id": l["name"], "type": "tms", "title": {"ka": title_ka, "en": title_en},
                "url": f"/mapproxy/wmts/{l['name']}/GLOBAL_WEBMERCATOR/{{z}}/{{x}}/{{y}}.{ext}",
                "opaque": ext == "jpeg",
            })
        elif comps["geoserver"] and l.get("source") == "geoserver":
            layers.append({
                "id": l["name"], "type": "wms", "title": {"ka": title_ka, "en": title_en},
                "url": "/geoserver/wms", "layers": l.get("wms_layers", ""), "opaque": False,
            })
    base = None
    if comps["mapproxy"] and mp["upstream_basemap"] == "osm":
        base = {"url": "/mapproxy/wmts/basemap/GLOBAL_WEBMERCATOR/{z}/{x}/{y}.png",
                "attr": "&copy; OpenStreetMap contributors", "subdomains": ""}
    elif basemap_url:
        base = {"url": basemap_url, "attr": attr, "subdomains": "abcd" if "{s}" in basemap_url else ""}
    return {
        "title": {"ka": wm["title_ka"], "en": wm["title_en"]},
        "lang": wm["default_lang"],
        "center": wm["center"], "zoom": wm["zoom"], "bounds": wm["bounds"],
        "basemap": base, "layers": layers,
    }


def build_context(cfg: dict[str, Any], target: str) -> dict[str, Any]:
    comps = effective_components(cfg, target)
    pg = cfg["postgres"]
    domain = cfg["domain"]
    tls = cfg["tls"]
    scheme = "http" if tls == "none" else "https"
    public = f"{scheme}://{domain}"

    if target == "native":
        up = {
            "pg_host": "127.0.0.1",
            "geoserver": f"127.0.0.1:{cfg['tomcat']['port']}",
            "tomcat": f"127.0.0.1:{cfg['tomcat']['port']}",
            "mapproxy": "127.0.0.1:8081",
            "qgis": "127.0.0.1:9993",
            "webroot": "/var/www/gisstack/webmap",
            "mapproxy_conf": "/etc/gisstack/mapproxy/mapproxy.yaml",
            "mapproxy_cache": "/var/lib/gisstack/mapproxy/cache_data",
            "qgis_projects": cfg["qgis"]["projects_dir"],
        }
    else:  # compose & k8s share service names
        up = {
            "pg_host": "postgis",
            "geoserver": "geoserver:8080",
            "tomcat": "tomcat:8080",
            "mapproxy": "mapproxy:8081",
            "qgis": "qgis:80",
            "webroot": "/srv/webmap",
            "mapproxy_conf": "/etc/mapproxy/mapproxy.yaml",
            "mapproxy_cache": "/var/lib/mapproxy/cache_data",
            "qgis_projects": "/etc/qgisserver",
        }

    # Basemap
    wm = cfg["webmap"]
    bm = next((b for b in catalog.BASEMAPS if b["id"] == wm["basemap"]), catalog.BASEMAPS[-1])
    basemap_url = wm["basemap_url"] or bm["url"]

    # Native listens on 80/443 via Caddy; without Caddy services use own ports
    extra_pkgs_image = [
        p for p in pg_packages(cfg, "deb")
        if p not in (f"postgresql-{pg['version']}-postgis-3", f"postgresql-{pg['version']}-postgis-3-scripts")
    ]

    preload = [e["id"] for e in catalog.EXTENSIONS
               if e.get("preload") and e["id"] in pg["extensions"]]

    return {
        "webmap_conf": webmap_conf(cfg, comps, basemap_url, bm["attr"]),
        "cfg": cfg,
        "c": comps,
        "caddy_version": pins.CADDY_VERSION,
        "pg": pg,
        "tomcat": cfg["tomcat"],
        "gs": cfg["geoserver"],
        "qgis": cfg["qgis"],
        "mp": cfg["mapproxy"],
        "wm": wm,
        "k8s": cfg["k8s"],
        "target": target,
        "up": up,
        "public": public,
        "scheme": scheme,
        "domain": domain,
        "tls": tls,
        "runtime": cfg["runtime"],
        "reg": "docker.io/" if cfg["runtime"] == "podman" else "",      # Podman refuses short image names when several registries are configured and there is no TTY
        "tuning": pg_tuning(pg),
        "preload": preload,
        "deb_pkgs": pg_packages(cfg, "deb"),
        "rpm_pkgs": pg_packages(cfg, "rpm"),
        "image_pkgs": extra_pkgs_image,
        "pg_image_tag": f"{pg['version']}-{POSTGIS_TAGS.get(pg['version'], '3.5')}",
        "basemap_url": basemap_url,
        "basemap_attr": bm["attr"],
        "grids_all": catalog.GRIDS,
        "version": __version__,
        "project": cfg["project"],
        "gs_extensions": cfg["geoserver"].get("extensions", []),
    }


# ---------------------------------------------------------------------------
# File plan.  (template, destination, condition(ctx) -> bool, executable)
# ---------------------------------------------------------------------------
def _plan(cfg: dict[str, Any]) -> Iterable[tuple[str, str, str, bool]]:
    """yield (target, template, dest, executable)"""
    t = cfg["targets"]

    if "native" in t:
        c = effective_components(cfg, "native")
        yield "native", "native/install.sh.j2", "native/install.sh", True
        yield "native", "native/uninstall.sh.j2", "native/uninstall.sh", True
        yield "native", "native/stack.env.j2", "native/stack.env", False
        if c["postgis"]:
            yield "native", "common/init.sql.j2", "native/config/init-extensions.sql", False
        if c["caddy"]:
            yield "native", "common/Caddyfile.j2", "native/config/Caddyfile", False
        if c["mapproxy"]:
            yield "native", "common/mapproxy.yaml.j2", "native/config/mapproxy/mapproxy.yaml", False
            yield "native", "common/seed.yaml.j2", "native/config/mapproxy/seed.yaml", False
            yield "native", "common/mapproxy_app.py.j2", "native/config/mapproxy/app.py", False
            yield "native", "native/mapproxy.service.j2", "native/systemd/gisstack-mapproxy.service", False
        if c["tomcat"]:
            yield "native", "native/tomcat.service.j2", "native/systemd/gisstack-tomcat.service", False
            yield "native", "native/setenv.sh.j2", "native/config/tomcat/setenv.sh", True
        if c["qgis_server"]:
            yield "native", "native/qgis.service.j2", "native/systemd/gisstack-qgis.service", False
            yield "native", "native/qgis.socket.j2", "native/systemd/gisstack-qgis.socket", False

    if "compose" in t:
        c = effective_components(cfg, "compose")
        yield "compose", "compose/docker-compose.yml.j2", "compose/docker-compose.yml", False
        yield "compose", "compose/env.j2", "compose/.env", False
        yield "compose", "compose/up.sh.j2", "compose/up.sh", True
        if c["postgis"]:
            yield "compose", "common/init.sql.j2", "compose/postgis/init/01-extensions.sql", False
            yield "compose", "compose/postgis.Dockerfile.j2", "compose/postgis/Dockerfile", False
        if c["caddy"]:
            yield "compose", "common/Caddyfile.j2", "compose/caddy/Caddyfile", False
        if c["mapproxy"]:
            yield "compose", "common/mapproxy.yaml.j2", "compose/mapproxy/mapproxy.yaml", False
            yield "compose", "common/seed.yaml.j2", "compose/mapproxy/seed.yaml", False
            yield "compose", "common/mapproxy_app.py.j2", "compose/mapproxy/app.py", False
            yield "compose", "compose/mapproxy.Dockerfile.j2", "compose/mapproxy/Dockerfile", False

    if "k8s" in t:
        c = effective_components(cfg, "k8s")
        for name in ("kustomization.yaml", "namespace.yaml", "secret.yaml"):
            yield "k8s", f"k8s/{name}.j2", f"k8s/{name}", False
        yield "k8s", "k8s/apply.sh.j2", "k8s/apply.sh", True
        if c["postgis"]:
            yield "k8s", "k8s/postgis.yaml.j2", "k8s/postgis.yaml", False
            yield "k8s", "common/init.sql.j2", "k8s/files/init-extensions.sql", False
        if c["geoserver"]:
            yield "k8s", "k8s/geoserver.yaml.j2", "k8s/geoserver.yaml", False
        if c["tomcat"]:
            yield "k8s", "k8s/tomcat.yaml.j2", "k8s/tomcat.yaml", False
        if c["qgis_server"]:
            yield "k8s", "k8s/qgis.yaml.j2", "k8s/qgis.yaml", False
        if c["caddy"]:
            yield "k8s", "k8s/caddy.yaml.j2", "k8s/caddy.yaml", False
            yield "k8s", "common/Caddyfile.j2", "k8s/files/Caddyfile", False
        if c["mapproxy"]:
            yield "k8s", "k8s/mapproxy.yaml.j2", "k8s/mapproxy.yaml", False
            yield "k8s", "common/mapproxy.yaml.j2", "k8s/files/mapproxy.yaml", False
            yield "k8s", "common/seed.yaml.j2", "k8s/files/seed.yaml", False
            yield "k8s", "common/mapproxy_app.py.j2", "k8s/files/app.py", False


def _shdq(value: Any) -> str:
    """Escape text for use inside a double-quoted shell string: no expansion, no early end of the string."""
    return re.sub(r'([\\"$`])', r"\\\1", str(value))


def _shdw(value: Any) -> str:
    """Same, for the default word of "${VAR:-word}" (where an unescaped } would also end the expansion)."""
    # a single quote would open a quoted section inside the word: close the double quote around it instead
    return re.sub(r'([\\"$`}])', r"\\\1", str(value)).replace("'", "\"'\"")


def _write(path: Path, text: str, executable: bool = False, secret: bool = False) -> None:
    """Secret files get 0600 from their first byte; everything else keeps the usual 0644 / 0755."""
    mode = 0o600 if secret else (0o755 if executable else 0o644)
    fsutil.write_file(path, text, mode, atomic=False)


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    for p in src.rglob("*"):
        if p.is_file():
            out = dst / p.relative_to(src)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)


def _render_tree(env: jinja2.Environment, src_dir: str, dst: Path, ctx: dict) -> None:
    """Render a directory of templates: *.j2 are rendered, others copied verbatim."""
    root = TEMPLATE_DIR / src_dir
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if p.suffix == ".j2":
            out = dst / rel.with_suffix("")
            _write(out, env.get_template(f"{src_dir}/{rel.as_posix()}").render(**ctx))
        else:
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)


def _make_tgz(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                tf.add(p, arcname=str(p.relative_to(src)))


def generate_bundle(cfg: dict[str, Any], out_dir: str | os.PathLike) -> Path:
    """Write the full bundle to out_dir and return the path."""
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    fsutil.private_dir(out)            # the bundle holds secrets: owner-only from the start
    env = _env()
    targets = cfg["targets"]
    ctx_any = build_context(cfg, targets[0] if targets else "native")

    # Web map first: it is copied into every target afterwards
    wm_tmp = out / "webmap"
    if cfg["components"]["webmap"]:
        _render_tree(env, "webmap", wm_tmp, ctx_any)
        _copy_tree(VENDOR_DIR / "leaflet", wm_tmp / "vendor" / "leaflet")

    for target, tpl, dest, exe in _plan(cfg):
        ctx = build_context(cfg, target)
        secret = dest.endswith(("stack.env", "/.env", "secret.yaml"))
        _write(out / dest, env.get_template(tpl).render(**ctx), exe, secret)

    if "native" in targets:       # what install.sh verifies its downloads against
        _write(out / "native/config/pins.sha256", pins.pins_file())
        tool = (TEMPLATE_DIR / "native/gisstack-backup.sh").read_text(encoding="utf-8")
        _write(out / "native/gisstack-backup", tool.replace('"${GS_VERSION_STAMP:-}"', f'"${{GS_VERSION_STAMP:-{__version__}}}"'), executable=True)
        if effective_components(cfg, "native")["mapproxy"]:
            for name in ("mapproxy-requirements.txt", "mapproxy-requirements-py310.txt"):
                _write(out / "native/config" / name, (TEMPLATE_DIR / "native" / name).read_text())

    if cfg["components"]["webmap"]:
        if "native" in targets:
            _copy_tree(wm_tmp, out / "native" / "webmap")
        if "compose" in targets:
            _copy_tree(wm_tmp, out / "compose" / "webmap")
        if "k8s" in targets and cfg["components"]["caddy"]:
            _make_tgz(wm_tmp, out / "k8s" / "files" / "webmap.tgz")

    if cfg["components"]["mobile_templates"]:
        _render_tree(env, "mobile/react-native", out / "mobile" / "react-native", ctx_any)
        _render_tree(env, "mobile/flutter", out / "mobile" / "flutter", ctx_any)

    if cfg["components"]["qgis_server"] and "compose" in targets:
        _write(out / "compose" / "qgis" / "projects" / "README.txt",
               "Put your .qgs / .qgz projects here.\n"
               "WMS: /qgis/?MAP=/etc/qgisserver/<name>.qgs&SERVICE=WMS&REQUEST=GetCapabilities\n")

    _write(out / "README.md", env.get_template("common/README.md.j2").render(**ctx_any))
    clean = {k: v for k, v in cfg.items() if k != "_meta"}
    _write(out / "stack.json", json.dumps(clean, indent=2, ensure_ascii=False), secret=True)
    return out


def bundle_zip_bytes(bundle_dir: str | os.PathLike) -> bytes:
    buf = io.BytesIO()
    root = Path(bundle_dir)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                info = zipfile.ZipInfo.from_file(p, arcname=str(Path(root.name) / p.relative_to(root)))
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, p.read_bytes())
    return buf.getvalue()
