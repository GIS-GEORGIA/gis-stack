"""Static catalog: what can be installed and which options exist.

Everything the GUIs show (checkboxes, drop-downs, extension list) comes from
here, so adding a component or an extension is a one-place change.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Versions. Verified Nov 2026: GeoServer 3.0.2 (stable, needs Tomcat 11.0.x,
# Java 17/21) and 2.28.6 (maintenance, Tomcat 9). Tomcat 10.1 is NOT supported
# by GeoServer 3. Override any of these in the GUI "advanced" fields.
# --------------------------------------------------------------------------
GEOSERVER_CHANNELS = {
    "3": {
        "label": "GeoServer 3.0 (stable)",
        "version": "3.0.2",
        "tomcat_major": "11",
        "java": "17",
        "image": "docker.osgeo.org/geoserver:3.0.2",
    },
    "2": {
        "label": "GeoServer 2.28 (maintenance)",
        "version": "2.28.6",
        "tomcat_major": "9",
        "java": "17",
        "image": "docker.osgeo.org/geoserver:2.28.6",
    },
}

GEOSERVER_EXTENSIONS = ["vectortiles", "css", "wps", "importer", "ysld", "monitor",
                        "control-flow", "inspire", "ogcapi-features", "mbstyle"]

POSTGRES_VERSIONS =["18", "17", "16", "15", "14"]
TOMCAT_MAJORS = ["11", "10.1", "9"]
JAVA_VERSIONS = ["17", "21"]

# --------------------------------------------------------------------------
# Components (the "tick the boxes" list)
# --------------------------------------------------------------------------
COMPONENTS = [
    {
        "id": "postgis",
        "ka": "PostgreSQL + PostGIS",
        "en": "PostgreSQL + PostGIS",
        "desc_ka": "სივრცითი მონაცემთა ბაზა, ექსთენშენები მონიშვნით",
        "default": True,
        "requires": [],
    },
    {
        "id": "tomcat",
        "ka": "Apache Tomcat",
        "en": "Apache Tomcat",
        "desc_ka": "Java სერვლეტ-კონტეინერი (GeoServer-ისთვის ავტომატურად ირთვება)",
        "default": False,
        "requires": [],
    },
    {
        "id": "geoserver",
        "ka": "GeoServer",
        "en": "GeoServer",
        "desc_ka": "WMS/WFS/WMTS სერვერი (native რეჟიმში Tomcat-ში ინსტალირდება)",
        "default": True,
        "requires": ["tomcat"],  # only for native mode, see config.resolve()
    },
    {
        "id": "qgis_server",
        "ka": "QGIS Server",
        "en": "QGIS Server",
        "desc_ka": "QGIS პროექტების გამოქვეყნება WMS/WFS/OGC API-ით",
        "default": False,
        "requires": [],
    },
    {
        "id": "mapproxy",
        "ka": "MapProxy",
        "en": "MapProxy",
        "desc_ka": "ქეშირება, ტაილების გენერაცია, WMTS/TMS, seed",
        "default": True,
        "requires": [],
    },
    {
        "id": "caddy",
        "ka": "Caddy (reverse proxy + HTTPS)",
        "en": "Caddy (reverse proxy + HTTPS)",
        "desc_ka": "ერთი შესასვლელი: /, /geoserver, /mapproxy, /qgis; ავტომატური TLS",
        "default": True,
        "requires": [],
    },
    {
        "id": "webmap",
        "ka": "ვებ რუკა (ორენოვანი KA/EN)",
        "en": "Web map (bilingual KA/EN)",
        "desc_ka": "წინასწარ გამზადებული Leaflet რუკა, ლოკალურად (CDN-ის გარეშე)",
        "default": True,
        "requires": [],
    },
    {
        "id": "mobile_templates",
        "ka": "მობილური რუკები (React Native + Flutter)",
        "en": "Mobile map templates (React Native + Flutter)",
        "desc_ka": "მზა პროექტები, უკვე მიბმული შენს სერვერზე",
        "default": True,
        "requires": [],
    },
]

# --------------------------------------------------------------------------
# PostgreSQL extensions.  `deb` / `rpm` are package-name templates ({v} = PG
# major); None means "ships with PostgreSQL contrib", "" means "nothing extra".
# `image` marks extensions that need a different container image in Compose.
# --------------------------------------------------------------------------
EXTENSIONS = [
    {"id": "postgis", "ka": "PostGIS (core)", "default": True, "fixed": True,
     "deb": ["postgresql-{v}-postgis-3", "postgresql-{v}-postgis-3-scripts"],
     "rpm": ["@postgis"]},
    {"id": "postgis_raster", "ka": "PostGIS Raster", "default": True,
     "deb": [], "rpm": []},
    {"id": "postgis_topology", "ka": "PostGIS Topology", "default": True,
     "deb": [], "rpm": []},
    {"id": "postgis_sfcgal", "ka": "PostGIS SFCGAL (3D)", "default": False,
     "deb": [], "rpm": [], "note": "საჭიროებს SFCGAL ბიბლიოთეკას"},
    {"id": "address_standardizer", "ka": "address_standardizer", "default": False,
     "deb": [], "rpm": []},
    {"id": "postgis_tiger_geocoder", "ka": "TIGER geocoder", "default": False,
     "deb": [], "rpm": [], "cascade": True},
    {"id": "pgrouting", "ka": "pgRouting", "default": False,
     "deb": ["postgresql-{v}-pgrouting"], "rpm": ["pgrouting_{v}"],
     "image": "pgrouting"},
    {"id": "pointcloud", "ka": "pointcloud (LiDAR)", "default": False,
     "deb": ["postgresql-{v}-pointcloud"], "rpm": ["pointcloud_{v}"]},
    {"id": "ogr_fdw", "ka": "ogr_fdw (Shapefile/GPKG როგორც ცხრილი)", "default": False,
     "deb": ["postgresql-{v}-ogr-fdw"], "rpm": ["ogr_fdw_{v}"]},
    {"id": "h3", "ka": "H3 (hex grid)", "default": False,
     "deb": ["postgresql-{v}-h3"], "rpm": ["h3-pg_{v}"]},
    {"id": "hstore", "ka": "hstore", "default": True, "deb": [], "rpm": []},
    {"id": "pg_trgm", "ka": "pg_trgm (ძებნა)", "default": True, "deb": [], "rpm": []},
    {"id": "unaccent", "ka": "unaccent", "default": False, "deb": [], "rpm": []},
    {"id": "fuzzystrmatch", "ka": "fuzzystrmatch", "default": False, "deb": [], "rpm": []},
    {"id": "btree_gist", "ka": "btree_gist", "default": False, "deb": [], "rpm": []},
    {"id": "ltree", "ka": "ltree", "default": False, "deb": [], "rpm": []},
    {"id": "citext", "ka": "citext", "default": False, "deb": [], "rpm": []},
    {"id": "uuid-ossp", "ka": "uuid-ossp", "default": True, "deb": [], "rpm": []},
    {"id": "pg_stat_statements", "ka": "pg_stat_statements", "default": False,
     "deb": [], "rpm": [], "preload": True},
]

# --------------------------------------------------------------------------
# MapProxy
# --------------------------------------------------------------------------
GRIDS = [
    {"id": "GLOBAL_WEBMERCATOR", "ka": "EPSG:3857 Web Mercator", "default": True},
    {"id": "GLOBAL_GEODETIC", "ka": "EPSG:4326 WGS84", "default": False},
    {"id": "UTM38N", "ka": "EPSG:32638 UTM 38N (საქართველო)", "default": False,
     "srs": "EPSG:32638", "bbox": [150000, 4500000, 850000, 4950000]},
    {"id": "UTM37N", "ka": "EPSG:32637 UTM 37N", "default": False,
     "srs": "EPSG:32637", "bbox": [150000, 4500000, 850000, 4950000]},
]
CACHE_TYPES = ["file", "sqlite", "geopackage"]
TILE_FORMATS = ["image/png", "image/jpeg", "mixed"]

# --------------------------------------------------------------------------
# Deployment targets
# --------------------------------------------------------------------------
TARGETS = [
    {"id": "native", "ka": "პირდაპირ სერვერზე (apt / dnf)"},
    {"id": "compose", "ka": "Docker / Podman Compose"},
    {"id": "k8s", "ka": "Kubernetes"},
]
RUNTIMES = ["docker", "podman"]
TLS_MODES = [
    {"id": "auto", "ka": "ავტომატური Let's Encrypt (საჭიროა რეალური დომენი)"},
    {"id": "internal", "ka": "Caddy-ს შიდა CA (ლოკალური/ინტრანეტი)"},
    {"id": "none", "ka": "HTTP მხოლოდ (TLS-ის გარეშე)"},
]
BASEMAPS = [
    {"id": "osm", "ka": "OpenStreetMap (დაბალი დატვირთვისთვის!)",
     "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
     "attr": "&copy; OpenStreetMap contributors"},
    {"id": "carto_light", "ka": "CARTO Positron",
     "url": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
     "attr": "&copy; OpenStreetMap contributors &copy; CARTO"},
    {"id": "custom", "ka": "საკუთარი XYZ URL", "url": "", "attr": ""},
    {"id": "none", "ka": "არცერთი", "url": "", "attr": ""},
]


def component_ids() -> list[str]:
    return [str(c["id"]) for c in COMPONENTS]


def ui_catalog() -> dict:
    """JSON-serialisable catalog for the GUIs."""
    return {
        "components": COMPONENTS,
        "extensions": EXTENSIONS,
        "grids": GRIDS,
        "targets": TARGETS,
        "runtimes": RUNTIMES,
        "tls_modes": TLS_MODES,
        "basemaps": BASEMAPS,
        "postgres_versions": POSTGRES_VERSIONS,
        "tomcat_majors": TOMCAT_MAJORS,
        "geoserver_channels": GEOSERVER_CHANNELS,
        "geoserver_extensions": GEOSERVER_EXTENSIONS,
        "cache_types": CACHE_TYPES,
        "tile_formats": TILE_FORMATS,
    }
