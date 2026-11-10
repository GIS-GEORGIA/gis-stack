# GIS Stack API (v1)

*Generated from the route table by `gisstack api docs` - do not edit. The machine-readable version is `docs/openapi.json` (also served at `/api/v1/openapi.json`).*

## Conventions

* Base path `/api/v1`. Every JSON answer is an envelope `{"data": ..., "error": null | {"message", "code", "details"?}, "meta": {"version": 1, "request_id"}}`.
* Authentication: `Authorization: Bearer <token>` (an API token from `gisstack token create`, or the web access token), or the browser session cookie. Browser sessions must also send the header `X-GisStack: 1` on state-changing requests (CSRF protection); Bearer clients do not need it.
* **Scopes**: `read` < `operate` < `admin`. A token's scope must be at least the route's scope, otherwise `403 forbidden`. API tokens only work on `/api/v1`.
* Requests are validated against the schemas below; problems come back as `400 bad_request` with every problem listed in `error.details`. Unknown fields are rejected.
* Long operations return `{"data": {"job": "<id>"}}`; follow them with `GET /jobs/{id}?since=<n>` (poll) and stop with `POST /jobs/{id}/cancel`. Only one mutating job runs at a time (`409 busy`).
* Error codes are stable: `bad_request` 400, `config` 400, `plan` 400, `unauthorized` 401, `forbidden` 403, `not_found` 404, `feature_disabled` 404, `conflict` 409, `preflight` 409, `busy` 409, `too_large` 413, `rate_limited` 429, `transport` 502, `update` 502, `timeout` 504, `internal` 500.
* The pre-1.0 `/api/...` routes still work for one more release and answer with `Deprecation: true` and a `Link: <...>; rel="successor-version"` header. Each route below lists the old paths it replaces.

## backups

### `POST /api/v1/backups/create`

Create a verified backup (job).

* Scope: **operate**
* Replaces (deprecated): `/api/backup/create`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `components` (array)
  - `with_cache` (boolean)
  - `note` (string) - maxLength `200`
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/backups/list`

Backups on the server.

* Scope: **read**
* Replaces (deprecated): `/api/backup/list`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`:
  - `backups` (array, required)
* Errors: 400, 401, 403, 502

### `POST /api/v1/backups/offsite`

Copy a verified backup to every BackupProvider plugin (job).

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `id` (string, required) - pattern `^(pre-)?\d{8}-\d{6}-[0-9a-f]{8}$`
  - `encrypt_to` (array)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/backups/prune`

Delete old backups (job). Defaults to a dry run; the newest backup is never deleted.

* Scope: **admin**
* Replaces (deprecated): `/api/backup/prune`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `keep` (integer) - min `1`
  - `keep_pre` (integer) - min `0`
  - `days` (integer|null) - min `0`
  - `dry_run` (boolean)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/backups/restore`

Restore a backup (job). Defaults to a plan; replacing live data needs dry_run=false AND confirm=true.

* Scope: **admin**
* Replaces (deprecated): `/api/backup/restore`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `id` (string, required) - pattern `^(pre-)?\d{8}-\d{6}-[0-9a-f]{8}$`
  - `only` (array)
  - `dry_run` (boolean)
  - `staging` (boolean)
  - `confirm` (boolean)
  - `pre_backup` (boolean)
  - `rollback` (boolean)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/backups/verify`

Re-read a backup and check every checksum (job).

* Scope: **operate**
* Replaces (deprecated): `/api/backup/verify`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `id` (string, required) - pattern `^(pre-)?\d{8}-\d{6}-[0-9a-f]{8}$`
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

## config

### `POST /api/v1/bundle`

Download the bundle as a zip (contains the generated passwords).

* Scope: **admin**
* Replaces (deprecated): `/api/bundle`
* Request body (JSON):
  - `config` (object)
* Response: `application/octet-stream` (no envelope)
* Errors: 400, 401, 403

### `POST /api/v1/bundle/preview`

Render the files of a bundle (passwords masked).

* Scope: **read**
* Replaces (deprecated): `/api/preview`
* Request body (JSON):
  - `config` (object)
* Response `data`:
  - `files` (object, required)
  - `warnings` (array)
* Errors: 400, 401, 403

### `GET /api/v1/catalog`

What can be installed: components, versions, extensions, defaults.

* Scope: **read**
* Replaces (deprecated): `/api/catalog`
* Response `data`:
  - `catalog` (object, required)
  - `defaults` (object, required)
  - `is_root` (boolean)
* Errors: 401, 403

### `POST /api/v1/config/validate`

Validate a configuration without touching anything.

* Scope: **read**
* Request body (JSON):
  - `config` (object, required)
* Response `data`:
  - `valid` (boolean, required)
  - `errors` (array)
  - `warnings` (array)
  - `deploy_target` (string)
  - `components` (array)
* Errors: 400, 401, 403

### `GET /api/v1/projects`

Saved projects (configurations).

* Scope: **read**
* Replaces (deprecated): `/api/projects`
* Response `data`:
  - `projects` (array, required)
* Errors: 401, 403

### `GET /api/v1/projects/{name}`

One saved project, complete with defaults.

* Scope: **read**
* Replaces (deprecated): `/api/projects/<name>`
* Path parameter `name` (string, required) - project name
* Response `data`:
  - `config` (object, required)
* Errors: 401, 403, 404

### `PUT /api/v1/projects/{name}`

Save a project, or only one program's settings of it.

* Scope: **admin**
* Replaces (deprecated): `/api/save`
* Path parameter `name` (string, required) - project name
* Request body (JSON):
  - `config` (object, required)
  - `component` (string) - one of `postgis`, `tomcat`, `geoserver`, `qgis_server`, `mapproxy`, `caddy`, `webmap`
* Response `data`:
  - `saved` (string, required)
* Errors: 400, 401, 403

## dashboard

### `GET /api/v1/dashboard/config`

What the dashboard needs to start: version, features, projects, settings.

* Scope: **read**
* Response `data`:
  - `version` (string, required)
  - `features` (array, required)
  - `is_root` (boolean)
  - `projects` (array)
  - `settings` (object)
* Errors: 401, 403

### `POST /api/v1/dashboard/summary`

Live status + newest stored metrics + recent operations in one call (each part reports its own error).

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`:
  - `project` (string, required)
  - `target` (string, required)
  - `mode` (string)
  - `domain` (string)
  - `status` (object)
  - `monitoring` (object)
  - `operations` (array)
  - `errors` (object, required)
* Errors: 400, 401, 403

## deploy

### `POST /api/v1/cleanups`

Remove the whole stack or some programs (job). Purge deletes data.

* Scope: **admin**
* Replaces (deprecated): `/api/clean`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `components` (array|null)
  - `purge` (boolean)
  - `remove_postgres` (boolean)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/components/{id}/apply`

Roll out only one program with the current settings (job).

* Scope: **admin**
* Replaces (deprecated): `/api/apply`
* Path parameter `id` (string, required) - component id
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `save` (boolean)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/deployments`

Deploy the stack (job). Runs the pre-flight and port-conflict checks first.

* Scope: **admin**
* Replaces (deprecated): `/api/deploy`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `skip_preflight` (boolean)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409, 502

### `GET /api/v1/deployments/history`

Recorded deployments and rollbacks: state before, outcome and health after (no secrets).

* Scope: **read**
* Query parameter `project` (string)
* Query parameter `limit` (integer)
* Response `data`: an array
* Errors: 400, 401, 403

### `POST /api/v1/deployments/rollback`

Re-apply the configuration of an earlier healthy deployment (job). Never deletes data or backups; refused without a verified recovery point.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `record` (string, required)
  - `confirm` (boolean, required)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/deployments/rollback/plan`

Is a verified recovery path available for this machine, and what would it re-apply? (reads local records only).

* Scope: **operate**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `record` (string)
* Response `data`:
  - `eligible` (boolean, required)
  - `reason` (string, required)
  - `current` (any)
  - `target` (any)
  - `changes` (array)
  - `limitations` (array, required)
* Errors: 400, 401, 403

## existing

### `POST /api/v1/existing/backup`

Back up the settings of programs already installed (job).

* Scope: **operate**
* Replaces (deprecated): `/api/existing/backup`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `components` (array, required)
  - `with_data` (boolean)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/existing/execute`

Back up -> remove -> (install) -> import for programs already on the server (job).

* Scope: **admin**
* Replaces (deprecated): `/api/existing/execute`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `plan` (object, required)
  - `with_data` (boolean)
  - `install` (boolean)
  - `confirm_data_loss` (boolean)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/existing/scan`

What is already installed / which of our ports are taken.

* Scope: **read**
* Replaces (deprecated): `/api/existing/scan`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Errors: 400, 401, 403, 502

## geoserver

### `POST /api/v1/geoserver/capabilities`

Layers and formats a WMS / WFS / WMTS capabilities document advertises.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `service` (string, required) - one of `WMS`, `WFS`, `WMTS`
* Response `data`:
  - `service` (string, required)
  - `version` (string)
  - `layers` (array, required)
  - `formats` (array)
* Errors: 400, 401, 403, 404, 502

### `POST /api/v1/geoserver/health`

REST reachable, credentials accepted, WMS answering.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`:
  - `ok` (boolean, required)
  - `version` (object)
  - `workspaces` (integer|null)
  - `checks` (array, required)
* Errors: 400, 401, 403, 404, 502

### `POST /api/v1/geoserver/inventory`

Workspaces, datastores, layers, layer groups and styles (each part reports its own error).

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`:
  - `version` (object)
  - `workspaces` (array)
  - `datastores` (array)
  - `layers` (array)
  - `layergroups` (array)
  - `styles` (array)
  - `errors` (object, required)
* Errors: 400, 401, 403, 404, 502

### `POST /api/v1/geoserver/layers/detail`

One layer: type, default style, enabled.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `name` (string, required) - pattern `^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,62})?$`
* Response `data`:
  - `name` (string, required)
  - `type` (string)
  - `enabled` (boolean)
  - `default_style` (string)
  - `resource` (string)
  - `resource_type` (string)
* Errors: 400, 401, 403, 404, 502

### `POST /api/v1/geoserver/preview`

Layer preview: WMS GetMap proxied from the server (PNG/JPEG, max 2048 px).

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `layers` (string, required) - maxLength `400`
  - `bbox` (string, required) - maxLength `120`
  - `width` (integer, required) - min `16`; max `2048`
  - `height` (integer, required) - min `16`; max `2048`
  - `srs` (string) - maxLength `12`
  - `format` (string) - one of `image/png`, `image/jpeg`
  - `style` (string) - maxLength `80`
* Response: `application/octet-stream` (no envelope)
* Errors: 400, 401, 403, 404, 502

### `POST /api/v1/geoserver/reload`

Reload GeoServer's catalog and configuration from disk.

* Scope: **operate**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`:
  - `reloaded` (boolean)
* Errors: 400, 401, 403, 404, 502

### `POST /api/v1/geoserver/validate`

Configuration findings: empty workspaces, datastores without layers ....

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`: an array
* Errors: 400, 401, 403, 404, 502

### `POST /api/v1/geoserver/workspaces`

Create a workspace.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `name` (string, required) - pattern `^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$`
* Response `data`:
  - `name` (string, required)
* Errors: 400, 401, 403, 404, 502

### `POST /api/v1/geoserver/workspaces/delete`

Delete a workspace; recurse=true also deletes everything in it and needs confirm=true.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `name` (string, required) - pattern `^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$`
  - `recurse` (boolean)
  - `confirm` (boolean)
* Response `data`:
  - `deleted` (string)
* Errors: 400, 401, 403, 404, 502

## jobs

### `GET /api/v1/jobs/{id}`

Progress of a background job.

* Scope: **read**
* Replaces (deprecated): `/api/jobs/<jid>`
* Path parameter `id` (string, required) - job id
* Query parameter `since` (integer)
* Response `data`:
  - `lines` (array, required)
  - `next` (integer, required)
  - `done` (boolean, required)
  - `ok` (boolean, required)
* Errors: 400, 401, 403, 404

### `POST /api/v1/jobs/{id}/cancel`

Ask a job to stop (log following ends at once).

* Scope: **operate**
* Replaces (deprecated): `/api/jobs/<jid>/cancel`
* Path parameter `id` (string, required) - job id
* Response `data`:
  - `ok` (boolean)
* Errors: 401, 403, 404

## mapproxy

### `POST /api/v1/mapproxy/cache`

Cache size, tile count and tiles per level for every cache.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`:
  - `available` (boolean, required)
  - `cache_dir` (string)
  - `disk_total_kb` (integer|null)
  - `disk_free_kb` (integer|null)
  - `caches` (array, required)
  - `unmanaged` (array)
  - `total_bytes` (integer)
* Errors: 400, 401, 403, 404, 409, 502

### `POST /api/v1/mapproxy/cache/cleanup`

Delete a cache's tiles (or only tiles older than N days). Dry run by default; real deletion needs dry_run=false AND confirm=true.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `cache` (string, required) - pattern `^[A-Za-z0-9_]{1,60}$`
  - `older_than_days` (integer) - min `1`; max `36500`
  - `dry_run` (boolean)
  - `confirm` (boolean)
* Response `data`:
  - `cache` (string, required)
  - `dry_run` (boolean, required)
  - `entries` (array)
  - `files` (integer, required)
  - `bytes` (integer, required)
  - `older_than_days` (integer|null)
  - `note` (string)
* Errors: 400, 401, 403, 404, 409, 502

### `POST /api/v1/mapproxy/config`

The live configuration summarised: layers, caches, sources (credentials hidden), grids, seeds.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`:
  - `services` (array)
  - `layers` (array, required)
  - `caches` (array, required)
  - `sources` (array, required)
  - `grids` (array, required)
  - `seeds` (array)
  - `cache_dir` (string)
  - `demo` (boolean)
* Errors: 400, 401, 403, 404, 409, 502

### `POST /api/v1/mapproxy/config/backups`

Configuration backups made automatically before every change (newest first).

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`: an array
* Errors: 400, 401, 403, 404, 409, 502

### `POST /api/v1/mapproxy/config/edit`

Add/remove a layer or a grid: validated by MapProxy, backed up, installed atomically (dry_run defaults to true).

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `op` (string, required) - one of `add_grid`, `add_layer`, `remove_grid`, `remove_layer`
  - `params` (object, required)
  - `dry_run` (boolean)
* Response `data`:
  - `changes` (array, required)
  - `dry_run` (boolean, required)
  - `backup` (string)
  - `warnings` (array)
  - `note` (string)
* Errors: 400, 401, 403, 404, 409, 502

### `POST /api/v1/mapproxy/config/restore`

Put a configuration backup back (the current one is backed up first); confirm=true is required.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `id` (string, required) - pattern `^\d{8}-\d{6}-[0-9a-f]{4}$`
  - `confirm` (boolean, required)
* Response `data`:
  - `restored` (string, required)
  - `backup_of_previous` (string)
* Errors: 400, 401, 403, 404, 409, 502

### `POST /api/v1/mapproxy/health`

Service, HTTP, cache directory, disk, configuration validity, advertised layers.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`:
  - `ok` (boolean, required)
  - `findings` (array, required)
  - `service` (string)
  - `http` (integer)
  - `seeding` (boolean)
  - `layers_served` (array)
  - `config_valid` (boolean|null)
* Errors: 400, 401, 403, 404, 409, 502

### `POST /api/v1/mapproxy/seed`

Estimate (default) or run seeding of caches (job): a dry run always comes first, real runs need confirm=true and stay under max_tiles.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `caches` (array, required)
  - `grids` (array)
  - `level_from` (integer) - min `0`; max `20`
  - `level_to` (integer, required) - min `0`; max `20`
  - `bbox` (array, required)
  - `refresh` (boolean)
  - `concurrency` (integer) - min `1`; max `4`
  - `max_tiles` (integer) - min `1`; max `50000000`
  - `dry_run` (boolean)
  - `confirm` (boolean)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 404, 409, 502

### `POST /api/v1/mapproxy/tile`

Preview ONE tile through the running MapProxy (PNG/JPEG; a missing tile is fetched and cached).

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `layer` (string, required) - pattern `^[A-Za-z][A-Za-z0-9_]{0,40}$`
  - `grid` (string, required) - pattern `^[A-Za-z][A-Za-z0-9_]{0,60}$`
  - `z` (integer, required) - min `0`; max `24`
  - `x` (integer, required) - min `0`
  - `y` (integer, required) - min `0`
* Response: `application/octet-stream` (no envelope)
* Errors: 400, 401, 403, 404, 409, 502

### `POST /api/v1/mapproxy/validate`

Validate the live configuration (and seed.yaml) with MapProxy itself, strictly.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`:
  - `ok` (boolean, required)
  - `errors` (array, required)
  - `warnings` (array)
* Errors: 400, 401, 403, 404, 409, 502

## meta

### `GET /api/v1/i18n`

Georgian -> English translation table used by every interface.

* Scope: **read**
* Replaces (deprecated): `/api/i18n`
* Response `data`:
  - `en` (object, required)
* Errors: 401, 403

### `GET /api/v1/meta`

Version, enabled features, and who you are.

* Scope: **read**
* Response `data`:
  - `version` (string, required)
  - `api_version` (integer, required)
  - `features` (array, required)
  - `scopes` (array, required)
  - `is_root` (boolean)
  - `principal` (object, required)
* Errors: 401, 403

### `GET /api/v1/openapi.json`

This API as an OpenAPI 3.1 document.

* Scope: **read**
* Errors: 401, 403

## monitoring

### `GET /api/v1/alerts`

Alert state and recent events.

* Scope: **read**
* Query parameter `project` (string, required)
* Query parameter `host` (string)
* Response `data`:
  - `alerts` (array)
  - `events` (array)
* Errors: 400, 401, 403, 404

### `GET /api/v1/metrics`

Newest stored sample, firing alerts and availability.

* Scope: **read**
* Query parameter `project` (string, required)
* Query parameter `host` (string)
* Response `data`:
  - `ts` (number|null)
  - `age_seconds` (number|null)
  - `metrics` (object)
  - `alerts` (array)
  - `availability` (object)
* Errors: 400, 401, 403, 404

### `GET /api/v1/metrics/history`

Stored values of one metric.

* Scope: **read**
* Query parameter `project` (string, required)
* Query parameter `host` (string)
* Query parameter `name` (string, required)
* Query parameter `since` (number)
* Query parameter `step` (number)
* Query parameter `limit` (integer)
* Query parameter `labels` (string)
* Response `data`: an array
* Errors: 400, 401, 403, 404

### `POST /api/v1/monitor/collect`

Take one real sample now, store it, evaluate alerts (and notify).

* Scope: **operate**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`:
  - `snapshot` (object)
  - `notifications` (array)
  - `errors` (array)
* Errors: 400, 401, 403, 502

### `GET /api/v1/monitor/rules`

Alert rules (built-in and custom) with effective values, the current revision and last modification time.

* Scope: **read**
* Response `data`:
  - `revision` (string, required)
  - `modified` (number|null)
  - `metrics` (array)
  - `rules` (array, required)
* Errors: 400, 401, 403

### `POST /api/v1/monitor/rules`

Create a custom alert rule (id must be new; `revision` is the one read from GET).

* Scope: **admin**
* Request body (JSON):
  - `revision` (string, required) - pattern `^[0-9a-f]{1,64}$`
  - `id` (string, required) - pattern `^[a-z][a-z0-9_]{2,40}$`
  - `metric` (string, required) - one of `host_cpu_percent`, `host_mem_used_percent`, `host_swap_used_percent`, `host_load1`, `disk_used_percent`, `service_up`, `service_cpu_percent`, `service_rss_bytes`, `probe_up`, `probe_seconds`, `pg_up`, `pg_connections_percent`, `pg_longest_query_seconds`, `pg_waiting_locks`, `pg_database_size_bytes`, `pg_cache_hit_ratio`, `mapproxy_cache_bytes`, `backup_last_age_seconds`, `backup_failed_count`, `monitor_reachable`, `collect_errors`
  - `op` (string, required) - one of `>`, `>=`, `<`, `<=`, `==`
  - `threshold` (number, required)
  - `for` (integer, required) - min `1`; max `1000`
  - `level` (string, required) - one of `warn`, `crit`
  - `text` (string, required) - maxLength `200`
  - `description` (string) - maxLength `200`
  - `enabled` (boolean)
  - `renotify` (number) - min `0`; max `2592000.0`
* Response `data`:
  - `revision` (string, required)
  - `modified` (number|null)
  - `metrics` (array)
  - `rules` (array, required)
* Errors: 400, 401, 403, 404, 409

### `DELETE /api/v1/monitor/rules/{id}`

Delete a custom rule (built-in rules can only be disabled).

* Scope: **admin**
* Path parameter `id` (string, required) - rule id
* Query parameter `revision` (string, required)
* Response `data`:
  - `revision` (string, required)
  - `modified` (number|null)
  - `metrics` (array)
  - `rules` (array, required)
* Errors: 400, 401, 403, 404, 409

### `PUT /api/v1/monitor/rules/{id}`

Change a rule. Built-in: threshold, for, level, enabled, renotify (or reset=true to restore the defaults). Custom: any field.

* Scope: **admin**
* Path parameter `id` (string, required) - rule id
* Request body (JSON):
  - `revision` (string, required) - pattern `^[0-9a-f]{1,64}$`
  - `reset` (boolean)
  - `metric` (string) - one of `host_cpu_percent`, `host_mem_used_percent`, `host_swap_used_percent`, `host_load1`, `disk_used_percent`, `service_up`, `service_cpu_percent`, `service_rss_bytes`, `probe_up`, `probe_seconds`, `pg_up`, `pg_connections_percent`, `pg_longest_query_seconds`, `pg_waiting_locks`, `pg_database_size_bytes`, `pg_cache_hit_ratio`, `mapproxy_cache_bytes`, `backup_last_age_seconds`, `backup_failed_count`, `monitor_reachable`, `collect_errors`
  - `op` (string) - one of `>`, `>=`, `<`, `<=`, `==`
  - `threshold` (number)
  - `for` (integer) - min `1`; max `1000`
  - `level` (string) - one of `warn`, `crit`
  - `text` (string) - maxLength `200`
  - `description` (string) - maxLength `200`
  - `enabled` (boolean)
  - `renotify` (number) - min `0`; max `2592000.0`
* Response `data`:
  - `revision` (string, required)
  - `modified` (number|null)
  - `metrics` (array)
  - `rules` (array, required)
* Errors: 400, 401, 403, 404, 409

## operate

### `GET /api/v1/journal`

History of operations (deployments, backups, restores, rotations ...).

* Scope: **read**
* Query parameter `project` (string)
* Query parameter `limit` (integer)
* Response `data`: an array
* Errors: 400, 401, 403

### `POST /api/v1/ops/control`

Start / stop / restart / enable / disable programs (job).

* Scope: **operate**
* Replaces (deprecated): `/api/ops/control`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `action` (string, required) - one of `start`, `stop`, `restart`, `enable`, `disable`
  - `components` (array|null)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/ops/logs`

Read or follow a program's log (job; cancel it to stop following).

* Scope: **read**
* Replaces (deprecated): `/api/ops/logs`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `component` (string, required) - one of `postgis`, `tomcat`, `geoserver`, `qgis_server`, `mapproxy`, `caddy`, `webmap`, `qgis`
  - `lines` (integer) - min `1`; max `5000`
  - `follow` (boolean)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/ops/preflight`

Can the machine take the selected stack? (OS, memory, disk, network, clock).

* Scope: **read**
* Replaces (deprecated): `/api/preflight`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`:
  - `ok` (boolean, required)
  - `target` (string)
  - `checks` (array, required)
  - `facts` (object)
* Errors: 400, 401, 403, 502

### `POST /api/v1/ops/status`

Live state of every program.

* Scope: **read**
* Replaces (deprecated): `/api/ops/status`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `components` (array)
* Errors: 400, 401, 403, 502

## plugins

### `GET /api/v1/datasources`

Spatial data sources contributed by DataSource plugins.

* Scope: **read**
* Response `data`: an array
* Errors: 401, 403

### `GET /api/v1/plugins`

Installed plugins, the plugin API version and plugins that failed to load.

* Scope: **read**
* Response `data`:
  - `enabled` (boolean, required)
  - `api_version` (integer, required)
  - `plugins` (array, required)
  - `failures` (array, required)
* Errors: 401, 403

## postgis

### `POST /api/v1/postgis/editor/setup`

Create or refresh the least-privilege read-only role (idempotent); its password stays on the server.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `statement_timeout` (integer) - min `1`; max `60`
* Response `data`:
  - `role` (string, required)
  - `database` (string, required)
  - `statement_timeout` (integer)
* Errors: 400, 401, 403, 409, 502, 504

### `POST /api/v1/postgis/elevate`

Open a write elevation for this caller, project and machine (max 15 minutes); confirm=true is required.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `minutes` (integer) - min `1`; max `15`
  - `confirm` (boolean, required)
* Response `data`:
  - `elevation` (string, required)
  - `expires` (number, required)
  - `minutes` (number)
* Errors: 400, 401, 403, 409, 502, 504

### `POST /api/v1/postgis/elevate/revoke`

End a write elevation now.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `elevation` (string, required)
* Response `data`:
  - `revoked` (boolean)
* Errors: 400, 401, 403, 409, 502, 504

### `POST /api/v1/postgis/execute`

Run ONE statement that may change data (needs a valid elevation and confirm=true; journalled and audited).

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `sql` (string, required) - maxLength `100000`
  - `elevation` (string, required)
  - `confirm` (boolean, required)
* Response `data`:
  - `columns` (array, required)
  - `rows` (array, required)
  - `row_count` (integer)
  - `has_more` (boolean)
  - `truncated` (boolean)
  - `elapsed_ms` (integer)
  - `offset` (integer)
  - `limit` (integer)
  - `paged` (boolean)
  - `tag` (string)
* Errors: 400, 401, 403, 409, 502, 504

### `POST /api/v1/postgis/health`

Findings over the inventory: connections, cache, geometry columns without index, missing extensions.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `database` (string) - pattern `^[A-Za-z_][A-Za-z0-9_]{0,62}$`
* Response `data`:
  - `ok` (boolean, required)
  - `findings` (array, required)
  - `server` (object)
* Errors: 400, 401, 403, 409, 502, 504

### `POST /api/v1/postgis/inventory`

Databases, schemas, tables, spatial columns (SRID, type, index), extensions, connections, query statistics.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `database` (string) - pattern `^[A-Za-z_][A-Za-z0-9_]{0,62}$`
* Response `data`:
  - `database` (string, required)
  - `server` (object, required)
  - `databases` (array)
  - `connections` (object)
  - `extensions` (array)
  - `available` (array)
  - `schemas` (array)
  - `tables` (array, required)
  - `spatial` (array, required)
  - `statements` (array|null)
  - `errors` (object, required)
* Errors: 400, 401, 403, 409, 502, 504

### `POST /api/v1/postgis/query`

Run ONE statement read-only (dedicated role, read-only transaction, timeout, row and size limits, paging).

* Scope: **operate**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `sql` (string, required) - maxLength `100000`
  - `limit` (integer) - min `1`; max `5000`
  - `offset` (integer) - min `0`
* Response `data`:
  - `columns` (array, required)
  - `rows` (array, required)
  - `row_count` (integer)
  - `has_more` (boolean)
  - `truncated` (boolean)
  - `elapsed_ms` (integer)
  - `offset` (integer)
  - `limit` (integer)
  - `paged` (boolean)
  - `tag` (string)
* Errors: 400, 401, 403, 409, 502, 504

## security

### `POST /api/v1/credentials/rotate`

Change the PostgreSQL role / GeoServer admin password on a native server (verified, rolled back on failure).

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `what` (string, required) - one of `postgres`, `geoserver`
* Response `data`:
  - `what` (string)
  - `target` (string)
  - `stored` (boolean)
* Errors: 400, 401, 403, 502

### `GET /api/v1/security/audit`

Newest administrative events from the audit log (optionally only those starting with a prefix such as postgis.).

* Scope: **admin**
* Query parameter `limit` (integer)
* Query parameter `event` (string)
* Response `data`: an array
* Errors: 400, 401, 403

### `GET /api/v1/security/posture`

Permissions and policy checks for the machine that runs gis-stack (read-only).

* Scope: **admin**
* Response `data`: an array
* Errors: 401, 403

## servers

### `GET /api/v1/servers`

Saved server profiles (no key material is ever stored or returned).

* Scope: **read**
* Response `data`: an array
* Errors: 400, 401, 403

### `POST /api/v1/servers/compare`

Compare 2-6 servers: OS, versions, service states and configuration hashes; `differs` marks drift.

* Scope: **read**
* Request body (JSON):
  - `ids` (array, required)
* Response `data`:
  - `servers` (array, required)
  - `rows` (array, required)
  - `drift` (integer, required)
  - `complete` (boolean, required)
* Errors: 400, 401, 403, 404

### `DELETE /api/v1/servers/{id}`

Delete a profile and its pinned host key (the server itself is not touched).

* Scope: **admin**
* Path parameter `id` (string, required) - server id
* Response `data`:
  - `deleted` (string, required)
* Errors: 401, 403, 404

### `PUT /api/v1/servers/{id}`

Create or update a server profile; changing host/port drops the pinned host key.

* Scope: **admin**
* Path parameter `id` (string, required) - server id
* Request body (JSON):
  - `name` (string)
  - `env` (string) - one of `production`, `development`, `testing`
  - `host` (string, required)
  - `port` (integer)
  - `user` (string)
  - `key` (string)
  - `notes` (string)
* Response `data`:
  - `id` (string, required)
  - `name` (string)
  - `env` (string) - one of `production`, `development`, `testing`
  - `host` (string)
  - `port` (integer)
  - `user` (string)
  - `key` (string)
  - `notes` (string)
  - `pinned` (boolean, required)
  - `fingerprints` (array)
  - `created` (integer)
  - `updated` (integer)
* Errors: 400, 401, 403

### `GET /api/v1/servers/{id}/history`

Operations (deploy, service control, edits ...) the journal recorded against this server.

* Scope: **read**
* Path parameter `id` (string, required) - server id
* Response `data`: an array
* Errors: 401, 403, 404

### `POST /api/v1/servers/{id}/hostkey/pin`

Pin the host key whose fingerprint equals `fingerprint` (compare it with the server console first); replacing a pin needs replace=true.

* Scope: **admin**
* Path parameter `id` (string, required) - server id
* Request body (JSON):
  - `fingerprint` (string, required) - pattern `^SHA256:[A-Za-z0-9+/]{43}$`
  - `replace` (boolean)
* Response `data`:
  - `id` (string, required)
  - `name` (string)
  - `env` (string) - one of `production`, `development`, `testing`
  - `host` (string)
  - `port` (integer)
  - `user` (string)
  - `key` (string)
  - `notes` (string)
  - `pinned` (boolean, required)
  - `fingerprints` (array)
  - `created` (integer)
  - `updated` (integer)
* Errors: 400, 401, 403, 404, 502

### `POST /api/v1/servers/{id}/hostkey/scan`

Show the host keys the server presents (SHA256 fingerprints) without trusting them.

* Scope: **admin**
* Path parameter `id` (string, required) - server id
* Response `data`:
  - `keys` (array, required)
  - `pinned` (array)
* Errors: 401, 403, 404, 502

### `POST /api/v1/servers/{id}/probe`

Connect with the profile's own credentials and pinned key; reports reachability, OS, sudo, load and free disk.

* Scope: **read**
* Path parameter `id` (string, required) - server id
* Response `data`:
  - `id` (string, required)
  - `reachable` (boolean, required)
  - `error` (string)
  - `seconds` (number)
  - `facts` (object)
* Errors: 400, 401, 403, 404

## settings

### `GET /api/v1/settings`

Interface language and theme (shared by the web and desktop GUI).

* Scope: **read**
* Replaces (deprecated): `/api/settings`
* Response `data`:
  - `lang` (string)
  - `theme` (string)
* Errors: 401, 403

### `PUT /api/v1/settings`

Change the interface language and/or theme.

* Scope: **admin**
* Replaces (deprecated): `/api/settings`
* Request body (JSON):
  - `lang` (string) - one of `auto`, `ka`, `en`
  - `theme` (string) - one of `auto`, `light`, `dark`
* Response `data`:
  - `lang` (string)
  - `theme` (string)
* Errors: 400, 401, 403

## stack-backups

### `POST /api/v1/stack-backups/check`

Pre-restore validation (integrity, compatibility, workloads running, PostgreSQL version) - changes nothing.

* Scope: **operate**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `id` (string, required) - pattern `^(pre-)?\d{8}T\d{6}Z-[0-9a-f]{4}$`
  - `components` (array)
* Response `data`:
  - `ok` (boolean, required)
  - `problems` (array, required)
  - `components` (array)
  - `backup` (string)
  - `created` (string)
  - `kind` (string)
* Errors: 400, 401, 403, 404

### `POST /api/v1/stack-backups/create`

Create a verified backup (job). Nothing is sealed unless every file was re-read and checked.

* Scope: **operate**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `components` (array)
  - `note` (string) - maxLength `200`
  - `with_cache` (boolean)
  - `encrypt_to` (array)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/stack-backups/delete`

Delete one backup (explicit; needs confirm=true).

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `id` (string, required) - pattern `^(pre-)?\d{8}T\d{6}Z-[0-9a-f]{4}$`
  - `confirm` (boolean, required)
* Response `data`:
  - `deleted` (string, required)
* Errors: 400, 401, 403, 404

### `POST /api/v1/stack-backups/export`

Download one verified backup as a .tar (still encrypted if it was made encrypted; contains GeoServer's own credential hashes when it holds GeoServer data).

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `id` (string, required) - pattern `^(pre-)?\d{8}T\d{6}Z-[0-9a-f]{4}$`
* Response: `application/octet-stream` (no envelope)
* Errors: 400, 401, 403, 404, 500

### `POST /api/v1/stack-backups/inspect`

One backup: manifest (no secrets) and its integrity status.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `id` (string, required) - pattern `^(pre-)?\d{8}T\d{6}Z-[0-9a-f]{4}$`
* Errors: 400, 401, 403, 404, 500

### `POST /api/v1/stack-backups/list`

Backups of this stack (newest first); partial, failed and damaged ones are listed with that status, never as ok.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`:
  - `backups` (array, required)
  - `restore_pending` (boolean, required)
  - `components` (array)
* Errors: 400, 401, 403

### `POST /api/v1/stack-backups/prune`

Apply the retention policy (keeps the newest intact backup whatever the policy says); dry_run shows what would go.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `keep` (integer) - min `1`; max `1000`
  - `days` (integer) - min `1`; max `3650`
  - `keep_pre` (integer) - min `1`; max `1000`
  - `dry_run` (boolean)
* Errors: 400, 401, 403

### `POST /api/v1/stack-backups/recover`

Put the previous data back after a restore that was interrupted (job); safe to repeat.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409

### `POST /api/v1/stack-backups/restore`

Restore a backup (job): validate, automatic pre-restore backup, stage, reversible swap, health check; undone automatically on failure. Needs confirm=true.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `id` (string, required) - pattern `^(pre-)?\d{8}T\d{6}Z-[0-9a-f]{4}$`
  - `components` (array)
  - `confirm` (boolean, required)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409, 500

### `POST /api/v1/stack-backups/verify`

Re-read every file and check checksums, the file set and the archive structure.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
  - `server` (string) - pattern `^[a-z][a-z0-9_-]{0,30}$`
  - `id` (string, required) - pattern `^(pre-)?\d{8}T\d{6}Z-[0-9a-f]{4}$`
* Response `data`:
  - `ok` (boolean, required)
  - `problems` (array, required)
  - `checked` (integer)
  - `id` (string)
* Errors: 400, 401, 403, 404

## updates

### `POST /api/v1/updates/apply`

Download, verify (SHA-256) and install the new release into this virtualenv (job).

* Scope: **admin**
* Replaces (deprecated): `/api/update/apply`
* Request body (JSON):
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409, 502

### `POST /api/v1/updates/check`

Is there a newer release on GitHub?.

* Scope: **read**
* Replaces (deprecated): `/api/update/check`
* Request body (JSON):
* Errors: 400, 401, 403, 502

## Schemas

### Config

```json
{
  "type": "object",
  "description": "A gis-stack configuration (see /catalog for the defaults); sparse is fine."
}
```

### Error

```json
{
  "type": "object",
  "required": [
    "message",
    "code"
  ],
  "properties": {
    "message": {
      "type": "string"
    },
    "code": {
      "type": "string"
    },
    "details": {}
  }
}
```

### Meta

```json
{
  "type": "object",
  "properties": {
    "version": {
      "type": "integer",
      "const": 1
    },
    "request_id": {
      "type": "string"
    }
  },
  "additionalProperties": true
}
```

### ServiceRow

```json
{
  "type": "object",
  "required": [
    "id",
    "state"
  ],
  "properties": {
    "id": {
      "type": "string"
    },
    "name": {
      "type": "string"
    },
    "state": {
      "type": "string",
      "enum": [
        "running",
        "stopped",
        "starting",
        "stopping",
        "failed",
        "missing",
        "unknown"
      ]
    },
    "detail": {
      "type": "string"
    },
    "enabled": {
      "type": [
        "boolean",
        "null"
      ]
    },
    "pid": {
      "type": [
        "integer",
        "null"
      ]
    },
    "since": {
      "type": "string"
    },
    "restarts": {
      "type": [
        "integer",
        "null"
      ]
    },
    "listen": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "urls": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "checks": {
      "type": "array"
    },
    "ok": {
      "type": "boolean"
    },
    "unit": {
      "type": "string"
    }
  }
}
```

### Ssh

```json
{
  "type": "object",
  "properties": {
    "host": {
      "type": "string"
    },
    "port": {
      "type": [
        "integer",
        "string"
      ]
    },
    "user": {
      "type": "string"
    },
    "key": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### Status

```json
{
  "type": "object",
  "required": [
    "target",
    "rows"
  ],
  "properties": {
    "target": {
      "type": "string"
    },
    "project": {
      "type": "string"
    },
    "domain": {
      "type": "string"
    },
    "time": {
      "type": "string"
    },
    "took": {
      "type": "number"
    },
    "rows": {
      "type": "array",
      "items": {
        "$ref": "#/components/schemas/ServiceRow"
      }
    }
  }
}
```
