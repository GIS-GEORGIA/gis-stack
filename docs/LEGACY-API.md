# Legacy `/api/*` routes - deprecation and retirement plan

Before `/api/v1` existed, the web GUI used routes directly under `/api/`. They still work, unchanged, **only for compatibility**; every one of them has an `/api/v1` replacement.

## Current state

* All legacy routes answer with `Deprecation: true` and `Link: </api/v1/...>; rel="successor-version"` (tested: `tests/test_api.py::LegacyTests`).
* They are authenticated and CSRF-protected exactly like the new routes, but they accept **only the interactive session cookie**, never a scoped API token (tested), so they cannot be used by automation with a limited token.
* Nothing new is added to them: every new feature (security, alert rules, deployments, stack backups, ...) exists **only** under `/api/v1`.
* The dashboard (`/dashboard`) uses `/api/v1` exclusively (tested). The classic page `/` still calls most of the routes below - that is the only reason they have not been removed.

## Why not remove them now

Removing a route the classic page uses breaks that page. The classic page no longer needs CSP `unsafe-inline` (its code was moved to `/static/classic/`; see `docs/DASHBOARD.md`), but it still calls most of these routes, so the routes are retired together with the page, in one release with a migration note - not piecemeal.

## Plan

1. **Now (this release line, 0.1.x):** deprecated and documented, behaviour unchanged. This file is checked against the code by a test, so it cannot go stale.
2. **Before retirement:** move the remaining classic-page workflows (deploy, clean, projects, programs already on the server) into the dashboard; verify each in a browser; then the classic page `/` redirects to `/dashboard`.
3. **Retirement release (target: the first 0.3.0 release, announced in the CHANGELOG one release earlier):** remove the legacy routes and the classic page together. `POST /api/v1/session/logout` already exists (replacement for `/api/logout`).
4. **Migration for scripts:** replace the path using the table below, send `Authorization: Bearer <token>` (create one with `gisstack token create --name NAME --scope read|operate|admin`) and read the result from the `data` field of the `/api/v1` envelope (`{"data": ..., "error": null, "meta": ...}`). Request bodies are the same objects, now validated against the OpenAPI schema (`docs/openapi.json`).

## Routes

| Legacy route | Method | Replacement | Scope | Used by the classic page `/` |
|---|---|---|---|---|
| `/api/apply` | POST | `/api/v1/components/{id}/apply` | admin | yes |
| `/api/backup/create` | POST | `/api/v1/backups/create` | operate | no |
| `/api/backup/list` | POST | `/api/v1/backups/list` | read | no |
| `/api/backup/prune` | POST | `/api/v1/backups/prune` | admin | no |
| `/api/backup/restore` | POST | `/api/v1/backups/restore` | admin | no |
| `/api/backup/verify` | POST | `/api/v1/backups/verify` | operate | no |
| `/api/bundle` | POST | `/api/v1/bundle` | admin | yes |
| `/api/catalog` | GET | `/api/v1/catalog` | read | yes |
| `/api/clean` | POST | `/api/v1/cleanups` | admin | yes |
| `/api/deploy` | POST | `/api/v1/deployments` | admin | yes |
| `/api/existing/backup` | POST | `/api/v1/existing/backup` | operate | yes |
| `/api/existing/execute` | POST | `/api/v1/existing/execute` | admin | yes |
| `/api/existing/scan` | POST | `/api/v1/existing/scan` | read | yes |
| `/api/i18n` | GET | `/api/v1/i18n` | read | yes |
| `/api/jobs/<jid>` | GET | `/api/v1/jobs/{id}` | read | yes |
| `/api/jobs/<jid>/cancel` | POST | `/api/v1/jobs/{id}/cancel` | operate | yes |
| `/api/logout` | POST | `/api/v1/session/logout` | read | no |
| `/api/ops/control` | POST | `/api/v1/ops/control` | operate | yes |
| `/api/ops/logs` | POST | `/api/v1/ops/logs` | read | yes |
| `/api/ops/status` | POST | `/api/v1/ops/status` | read | yes |
| `/api/preflight` | POST | `/api/v1/ops/preflight` | read | no |
| `/api/preview` | POST | `/api/v1/bundle/preview` | read | yes |
| `/api/projects` | GET | `/api/v1/projects` | read | yes |
| `/api/projects/<name>` | GET | `/api/v1/projects/{name}` | read | yes |
| `/api/save` | PUT | `/api/v1/projects/{name}` | admin | yes |
| `/api/settings` | GET | `/api/v1/settings` | read | yes |
| `/api/settings` | PUT | `/api/v1/settings` | admin | yes |
| `/api/update/apply` | POST | `/api/v1/updates/apply` | admin | yes |
| `/api/update/check` | POST | `/api/v1/updates/check` | read | yes |

`/api/logout` is replaced by `POST /api/v1/session/logout` (added in this release; same effect, JSON envelope, audited, needs the `X-GisStack: 1` header for browser sessions like every state change). The last column is a text search of the classic page and is approximate.
