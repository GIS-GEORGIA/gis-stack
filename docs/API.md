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
  - `id` (string, required) - pattern `^(pre-)?\d{8}-\d{6}-[0-9a-f]{8}$`
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
  - `skip_preflight` (boolean)
* Response `data`: `{"job": "<id>"}` (a background job)
* Errors: 400, 401, 403, 409, 502

## existing

### `POST /api/v1/existing/backup`

Back up the settings of programs already installed (job).

* Scope: **operate**
* Replaces (deprecated): `/api/existing/backup`
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
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
* Errors: 400, 401, 403, 502

## geoserver

### `POST /api/v1/geoserver/capabilities`

Layers and formats a WMS / WFS / WMTS capabilities document advertises.

* Scope: **read**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
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
* Response `data`: an array
* Errors: 400, 401, 403, 404, 502

### `POST /api/v1/geoserver/workspaces`

Create a workspace.

* Scope: **admin**
* Request body (JSON):
  - `config` (object)
  - `mode` (string, required) - one of `local`, `ssh`
  - `ssh` (object)
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
* Response `data`:
  - `snapshot` (object)
  - `notifications` (array)
  - `errors` (array)
* Errors: 400, 401, 403, 502

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
  - `what` (string, required) - one of `postgres`, `geoserver`
* Response `data`:
  - `what` (string)
  - `target` (string)
  - `stored` (boolean)
* Errors: 400, 401, 403, 502

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
