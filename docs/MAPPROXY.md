# MapProxy manager (Phase 6, feature flag `mapproxy_manager`)

`GISSTACK_FEATURES=mapproxy_manager gisstack web` -> dashboard page **MapProxy manager** (`/dashboard#/mapproxy`) and `POST /api/v1/mapproxy/*`.
Native target only for now (Compose/Kubernetes give a clear error).

## What you get
* **Health:** service state, WMTS capabilities over HTTP, cache directory writable by the `mapproxy` user, free disk, strict configuration check, layers
  configured vs. advertised, whether a seeding run is active.
* **Configuration view:** layers, caches, sources, grids, seeds - with credentials hidden (userinfo and secret-looking query values are masked, header
  values are never shown, only their names).
* **Cache statistics:** size, tile count and tiles per level for every cache (partial results are marked `≈` when a very large cache hit the time budget);
  folders that belong to no configured cache are listed separately.
* **Tile preview:** one tile through the running MapProxy (PNG/JPEG). A tile that is not cached is fetched from the source and cached.
* **Guarded configuration edits:** add/remove a layer (WMS or tile source) and add/remove a grid. The new file is validated by **MapProxy itself**
  (strict: unknown keys and dangling references are errors - plain `load_configuration` only *warns* about them), the old one is backed up under
  `/var/lib/gisstack/mapproxy/config-backups/<id>/` (newest 30 kept), and the file is replaced atomically; MapProxy reloads it by itself. `dry_run` is the
  default. Backups can be restored (the current file is backed up first).
* **Seeding:** pick caches, levels (0-20) and a bbox. An **estimate always runs first** (`mapproxy-seed --dry-run`); a real run needs `confirm=true`,
  is refused when the estimate exceeds `max_tiles` (default 2 000 000, hard cap 50 000 000) or cannot be read, runs as the unprivileged `mapproxy` user at low
  priority, shows live progress, can be cancelled, and only one seeding runs at a time. By default only **missing** tiles are created (MapProxy's own default
  would re-fetch every tile on every run); `refresh` rebuilds all. Caches fed by OpenStreetMap's public servers are refused above level 10 (usage policy).
* **Cache cleanup:** delete all tiles of one cache or only tiles older than N days (file caches). Dry run by default (reports files and bytes); real deletion
  needs `dry_run=false` and `confirm=true` (the dashboard shows the numbers and asks you to type the cache name). It never follows symlinks, never leaves
  the cache directory, never touches lock directories and refuses while seeding runs.

## Things to know
* Edits made here **do not carry comments** over (the file is rewritten from its parsed form), and `gisstack deploy` / `apply` regenerates `mapproxy.yaml`
  from the project configuration - an edit that must survive a redeploy also has to go into the project.
* Source URLs must be http(s), may not carry credentials, and link-local / metadata addresses are refused.
* Statements of seeding are written to a temporary `seed.yaml` (readable by the `mapproxy` user, removed afterwards).

## Scopes
read: `config`, `validate`, `health`, `cache`, `tile`, `config/backups` - admin: `config/edit`, `config/restore`, `seed` (job), `cache/cleanup`.
Every change is journalled and audited (principal, scope, what changed, backup id - never file content).

## Tests
`tests/test_mapproxy.py`: config summary/redaction, every edit and its rejections, statistics grouping, seeding guards, cleanup/preview/health, script
construction (no content in command lines), API scopes and audit - and **the real shell scripts against a real MapProxy** (its own validator, a real WSGI
app serving tiles from a local source, real `mapproxy-seed` runs incl. cancel, real cache files): estimate vs. seed vs. skip-existing vs. refresh, backups and
restore, age-based cleanup, symlink and busy protection. Skipped where MapProxy is not installed (MapProxy needs Python 3.10+).
