# GIS Stack - implementation status and verification report

*Inspection date: 2026-11-09. Repository state inspected: branch `main`, commit `91dc9a4` ("Deployment reliability: records, health verification, verified rollback"), version 0.1.7 in `pyproject.toml` (the newest tag is `v0.1.7`; the three commits after it are **not released**).*

Method: source code, tests and Git state were read directly; the test suite and quality tools were run for this report. Documentation was **not** treated as evidence. Classification terms:

* **COMPLETE** - implemented and verified at the level stated.
* **PARTIAL** - some functionality exists, requirements remain.
* **NOT IMPLEMENTED** - no code exists.
* **UNTESTED** - code exists, verification missing.
* **BROKEN** - tests or behaviour show a defect. *Nothing was found broken.*

"Verified" always names its level: **unit** (fakes/mocks), **real-software** (real PostgreSQL/Compose/kind/sshd/gpg in an opt-in suite), **browser** (checked by hand in a browser), **CI**.

## 1. Executive summary

* The repository is clean, in sync with GitHub, and everything in this report is committed and pushed (`main` = `origin/main`).
* All 809 tests that can run here pass (753 passed, 56 skipped, 0 failed); ruff, mypy (86 files), bandit (medium+) and the 10 JS tests are clean. CI `tests` and `codeql` are green for the newest commit.
* **Delivered since the audit:** Security page, Alert Rules manager, deployment records + health verification + *manual* verified rollback.
* **Not delivered, although they were on the request lists:** Web GIS improvements (legend, identify, search, WFS/GeoJSON, CRS info, fullscreen), GeoServer editing (datastores/layers/styles/layer groups), PostGIS saved queries / EXPLAIN tooling / index diagnostics beyond presence, backup and restore for Compose, Podman and Kubernetes, *automatic* rollback, removal of `unsafe-inline` from the classic page, removal of legacy `/api/*` routes.
* **The new deployment/rollback code has never run against a real server, container runtime or cluster.** The `integration` workflow was last run **before** today's commits (09:44 UTC; it was green then); it was not run for them.

## 2. Feature matrix

| Area | Feature | Status | Verified by |
|---|---|---|---|
| Security page | Permission checks, audit viewer, admin-only API, page | COMPLETE | unit + browser |
| Alert rules | CRUD API, admin-only writes, validation, atomic + revision-guarded updates, enable/disable, audit, dashboard editor, KA/EN | COMPLETE (see gaps) | unit; browser for create/validate/delete only |
| Alert rules | Per-rule notification *channel* settings | NOT IMPLEMENTED (only repeat interval + severity; channels stay in `notify.json`) | - |
| Deployment | History, pre-state capture, post-deploy health check | COMPLETE | unit (fakes) |
| Deployment | Manual rollback (plan, confirm, job, refusals, idempotent, keeps current secrets, never purges) | UNTESTED on real systems; unit-tested | unit (fakes), browser (plan dialog only) |
| Deployment | **Automatic** rollback on failure | NOT IMPLEMENTED (deliberate: rollback is explicit and confirmed) | - |
| Deployment | Native / Compose / Kubernetes rollback | IMPLEMENTED BUT NOT TESTED on real hosts (one shared code path; fake installer only) | unit |
| Deployment | Podman | IMPLEMENTED BUT NOT TESTED (`runtime: podman` is honoured by the compose provider; no rollback or deploy test uses podman) | - |
| Web GIS | Base-map + layer toggle, WMS and tile (WMTS-REST) overlays, scale bar, coordinates (WGS84), KA/EN, reset view | COMPLETE for what exists | generation/golden tests only; **no test or browser check executes the map JS** |
| Web GIS | Layer catalog UI, legend, feature identify, search/filter, WFS, GeoJSON, fullscreen, layer metadata, CRS info | NOT IMPLEMENTED | - |
| GeoServer | Version, health, validation, inventory (workspaces, datastores, layers, layer groups, styles), layer detail, WMS/WFS/WMTS capabilities, preview, workspace create/delete, reload, REST integration | COMPLETE (read + workspace) | unit; real-software on Compose and kind (GeoServer 2.28.6 / 3.0.2) in the opt-in suites, last run green 2026-11-09 before today's commits |
| GeoServer | Create/edit/delete datastores, layers, styles, layer groups; security rules; catalog backup; extension compatibility check | NOT IMPLEMENTED | - |
| PostGIS | Inventory (databases, extensions, tables, geometry columns with SRID, spatial-index presence), health, read-only query via dedicated role, time-boxed write mode with audit | COMPLETE | unit; real-software PostGIS 17 (Compose, kind) |
| PostGIS | Saved queries; EXPLAIN tooling (EXPLAIN only works as an ordinary read-only statement); index diagnostics beyond "has a GiST index"; spatial reference system overview; `postgresql.conf` recommendations (only a connection-usage warning exists) | NOT IMPLEMENTED | - |
| Backup/restore | Native: create, list, verify, restore with pre-restore backup and rollback, retention (`prune`), systemd schedule | COMPLETE for native | real bash/tar/PostgreSQL (opt-in, runs in CI via `GISSTACK_TEST_DOCKER`; skipped locally) + unit |
| Backup/restore | Encrypted off-site copies (local gpg, passphrase or recipient) | COMPLETE for local copies; server-side backups stay unencrypted | real gpg |
| Backup/restore | Docker Compose, Podman, Kubernetes | NOT IMPLEMENTED (code refuses: "backup/restore only on the native target") | - |
| Security | Auth (token -> expiring sessions, scoped API tokens), CSRF header, rate limit, Host allow-list, request-size limit, audit log, injection fuzz tests, 0600/0700 files | COMPLETE | unit (`test_web_security`, `test_api`, fuzz tests) |
| Security | SSH host-key verification | PARTIAL: strict pinned keys only for saved server profiles; ad-hoc `--ssh user@host` uses `StrictHostKeyChecking=accept-new` (trust on first use) | real sshd (profiles) |
| Security | Path traversal protection | COMPLETE in the modules that take paths (backup ids, MapProxy files, key paths, names) | unit |
| Security | CSP: strict on `/dashboard`; classic page `/` | PARTIAL: classic page still needs `unsafe-inline` | unit (dashboard rules) |
| Security | Legacy `/api/*` | PARTIAL: 28 routes, 27 deprecated with `Deprecation`/`Link` headers, `/api/logout` has no successor, none removed (classic page uses 18) | unit |

## 3. What exists, with file references

### Security page
* Code: `gisstack/security_report.py`, `gisstack/api/routes_security.py`, `gisstack/webgui/static/dashboard/js/views/security.js`.
* API: `GET /api/v1/security/posture`, `GET /api/v1/security/audit` (both `admin`).
* Works: owner-only checks on data dir, tokens, server profiles, pinned keys, secrets dir/files, audit log; signature policy; audit tail with prefix filter; no secret contents; symlinks not followed; missing files not invented.
* Does not: checks only the machine running gis-stack (not remote servers); POSIX modes only.
* Tests: `tests/test_security_report.py` (11).

### Alert rules
* Code: `gisstack/monitoring/rulestore.py`, `alerts.py` (`custom_rule`, `check_text`, `KNOWN_METRICS`), `gisstack/api/routes_monitor.py`, `gisstack/webgui/static/dashboard/js/views/rules.js`, strings in `gisstack/i18n.py`.
* API: `GET/POST /api/v1/monitor/rules`, `PUT/DELETE /api/v1/monitor/rules/{id}` (writes `admin`, reads `read`; `PUT {reset:true}` restores a built-in rule).
* Works: built-in rules cannot be deleted but can be tuned/disabled/reset; custom rules validated (metric from a fixed list, operator, severity, 1-1000 samples, text template limited to `{value}`/`{subject}`, repeat interval, id pattern, duplicates rejected); `revision` (file hash) + exclusive file lock + atomic 0600 replace; stale revision = `409`; 8 concurrent writers -> exactly one wins; damaged file reported and never overwritten; audit events `monitor.rule.create|update|reset|delete`; page shows last-modified time and revision, hides edit controls for non-admins, confirms deletion.
* Gaps: a running `monitor watch` only reads rules at start; `KNOWN_METRICS` is a hand-maintained list; **edit, enable/disable and reset in the dashboard were not exercised in a browser** (create, validation error and delete were); no per-rule notification channels.
* Tests: `tests/test_monitor_rules.py` (16); KA/EN coverage by `tests/test_gisstack.py::I18nTests` (strings exist; no test switches the rules page language in a browser, though that was checked by hand for the Security page, not for this one).

### Deployment records, health check, manual rollback
* Code: `gisstack/deployments.py`, hook in `gisstack/deploy.py::deploy`, `gisstack/services.py::rollback_*`, routes in `gisstack/api/routes.py`, UI in `.../views/deployments.js`, docs `docs/DEPLOYMENTS.md`.
* API: `GET /api/v1/deployments/history` (`read`), `POST /deployments/rollback/plan` (`operate`), `POST /deployments/rollback` (`admin`, `confirm:true`, job).
* Works (unit level): record per deployment with outcome `ok|unhealthy|unverified|failed|aborted|running|incomplete`, state before/after, secret-free; snapshot 0600; bounded history; health wait (`GISSTACK_VERIFY_TIMEOUT`, `GISSTACK_VERIFY=0`); a failing status read or unwritable history never stops a deployment; rollback refuses without a healthy recorded recovery point, keeps *current* secrets, never calls the clean entry, fails the job when not healthy afterwards, repeating it is a refused no-op; audit `deploy.start`, `deploy.rollback`.
* Missing / unverified: no automatic rollback; no real-host run of deploy-with-verification or rollback on native, Compose, Podman or Kubernetes; the dashboard rollback was checked only as far as the plan/confirm dialog (the job was deliberately not started); `existing.run` (adopt foreign programs) still has its own older backup flow and its inner `deploy.deploy` call is now also recorded (not separately tested); no audit event is written when a deploy job *finishes* (the deployment record and journal hold the outcome).
* Tests: `tests/test_deployments.py` (23). Some pre-existing tests were adjusted to skip the 60-second health wait (`GISSTACK_VERIFY=0` / `GISSTACK_VERIFY_TIMEOUT=0` in `tests/test_cli_deploy.py`); their assertions were not weakened.

### Web GIS (Leaflet map)
* Code: `gisstack/templates/webmap/app.js` (105 lines), `index.html.j2`, `config.js.j2`, URL generation in `gisstack/generate.py` (MapProxy WMTS REST URLs `/mapproxy/wmts/<layer>/GLOBAL_WEBMERCATOR/{z}/{x}/{y}`).
* Works: one base map, WMS and tile overlays with a layer control (only the first overlay is on at start), scale bar, mouse coordinates in WGS84, language switch, reset-view button, tile-error message.
* Missing: legend, identify, search, WFS, GeoJSON, fullscreen, metadata, CRS information/handling, a layer-catalog panel.
* Tests: generation/golden tests only. No test runs the map in a browser.

### GeoServer
* Code: `gisstack/geoserver/{manager,gateway}.py`, `gisstack/api/routes_geoserver.py`, `.../views/geoserver.js`.
* API (all `POST`): `/geoserver/inventory|health|validate|capabilities|layers/detail|preview|workspaces|workspaces/delete|reload`.
* Works: everything listed in the matrix; credentials stay server-side (curl inside the host/container, config on stdin); admin password never reaches the browser.
* Missing: any create/edit/delete of datastores, layers, styles, layer groups; extension compatibility; catalog backup; a link to the GeoServer admin UI was not verified.
* Tests: `tests/test_geoserver.py` (unit); `tests/integration/test_compose_managers.py`, `test_k8s_managers.py` (real GeoServer; opt-in).

### PostGIS
* Code: `gisstack/postgis/{manager,server,sqlsplit}.py`, `gisstack/api/routes_postgis.py`, `.../views/postgis.js`.
* API (all `POST`): `/postgis/inventory|health|editor/setup|query|elevate|elevate/revoke|execute`.
* Works: see matrix; safety boundary is the `gisstack_ro` role + read-only transaction + fixed prefix; writes need a time-boxed elevation and are audited with a statement digest.
* Missing: saved queries, EXPLAIN UI, unused/invalid-index diagnostics, SRS overview, configuration recommendations.
* Tests: `tests/test_postgis.py`; real PostGIS in the opt-in suites.

### Backup and restore
* Code: `gisstack/backup.py`, `gisstack/backup_crypto.py`, `gisstack/templates/native/gisstack-backup.sh`, routes `/backups/*` in `gisstack/api/routes.py`, `.../views/backups.js`.
* Works on **native only**; every function calls `_require_native` and refuses other targets. Verification (checksums), retention (`prune --keep/--days`), systemd timer schedule, pre-restore backup + rollback, local gpg encryption of downloaded/off-site copies.
* Not implemented: Compose, Podman, Kubernetes backup/restore; server-side encryption; MapProxy cache / GeoServer data dir for non-native.
* Tests: `tests/test_backup_tool.py`, `test_backup_manager.py`, `test_backup_crypto.py`.

## 4. Test and verification results

Run for this report on commit `91dc9a4` with Python 3.12 in a venv that has Flask, Jinja2 and PyYAML (without Flask the API tests silently skip - always test in such a venv):

| Check | Result |
|---|---|
| `python -m unittest discover -s tests` | **809 run: 753 passed, 56 skipped, 0 failed, 0 errors** |
| ruff | passed |
| mypy (`mypy`, 86 files) | passed |
| bandit (`-ll`) | no medium/high findings |
| `node --test tests/js/*.mjs` | 10 passed, 0 failed (`node --test tests/js` fails on this layout, also before these changes) |
| Python 3.9 | the changed modules parse with the 3.9 grammar; **no 3.9 interpreter was available, tests were not run on 3.9** |

**Skipped (not passed) - 56:** 22 Compose manager tests (`GISSTACK_TEST_INTEGRATION`), 15 MapProxy script tests (mapproxy/pgrep prerequisites missing here), 6 GeoServer-on-Compose, 4 Kubernetes (`GISSTACK_TEST_K8S`), 4 Playwright browser tests (`GISSTACK_TEST_E2E`), 1 shellcheck (not installed), 2 installer-in-distro-container, 1 mapproxy import, 1 Compose smoke (`GISSTACK_TEST_DOCKER`).

**Not run here:** all of the above opt-in suites, the shellcheck/kubeconform/YAML gates, Python 3.9-3.13 matrix, pip-audit, coverage ratchet.

**CI (checked with `gh run list` for the newest commit `91dc9a4`):** `tests` success, `codeql` success. **The `integration` workflow has not been run for `91dc9a4`**; its last run (workflow_dispatch, 2026-11-09 09:44 UTC, before the Security page / rules / rollback commits) was green. Release v0.1.7 workflow: success.

**Real-server / real-software testing:** performed earlier (before these three commits) for installers in distro containers, Compose stacks, a kind cluster, real sshd, real gpg, and signed-release update. **None of it covers the Security page, Alert Rules manager, deployment records or rollback.**

**Features verified only through unit tests:** Security page (plus a manual browser check), Alert Rules, deployment records/health verification/rollback, all `/api/v1` contract behaviour, the monitoring rule evaluation.

## 5. Security gaps

1. Classic page `/` needs CSP `unsafe-inline` (2 inline scripts, 1 style block, 13 `style=` attributes, 1 `onsubmit=`, plus inline styles inside JS-built HTML). The dashboard is strict.
2. Ad-hoc SSH (`--ssh user@host`) trusts the host key on first use (`accept-new`); strict pinning exists only for saved server profiles.
3. 27 deprecated legacy `/api/*` routes (and `/api/logout`) remain reachable; they carry the same authentication but widen the surface.
4. Backups stored on the server are unencrypted (they can contain live secrets); only off-site copies can be encrypted.
5. The web server defaults to the Werkzeug development server unless `--server waitress` is chosen.
6. The Security page cannot inspect remote servers.
7. Rollback re-applies a stored configuration snapshot that contains secrets (0600 in the data directory, never returned by the API); this is as sensitive as the existing `~/.gisstack/secrets`.
8. No audit event for the *completion* of a deployment job (only start events and the deployment record).

## 6. Deployment and platform compatibility gaps

* Rollback, health verification and deployment records: never run on a real native host, Compose project, Podman or kind/real Kubernetes cluster.
* Podman: configuration/provider support exists; no test of any kind uses podman.
* Rollback cannot remove programs, containers or Kubernetes objects that only the newer configuration added (documented limitation).
* Kubernetes MapProxy configuration (ConfigMap) is read-only from gis-stack.
* Backup/restore: native only.
* Python 3.9 is declared supported but was not executed in this inspection.
* Release v0.1.7 does not contain the three newest commits; a user installing 0.1.7 has none of them.

## 7. Top 10 remaining tasks (priority order)

1. Run the `integration` workflow on `91dc9a4` and add a real Compose (and kind) test for deploy -> health verification -> rollback. Without it, the rollback claim is unit-level only.
2. Browser-test the Alert Rules page (edit, enable/disable, reset, KA/EN) and the rollback job path; add it to the Playwright suite.
3. Backup and restore for Docker Compose / Podman, then Kubernetes (volumes, `pg_dump` inside the container, same manifest/verify/rollback rules).
4. Web map: layer catalog, legend (`GetLegendGraphic`), feature identify (`GetFeatureInfo`), search, fullscreen, CRS display; add a browser test of the map.
5. Retire the classic page (move deploy / clean / projects / existing-programs wizards into the dashboard), then drop `unsafe-inline` and the legacy `/api/*` aliases in one release with a migration note.
6. GeoServer editing: datastores, layers, styles, layer groups (REST), with confirmations and audit; catalog backup.
7. PostGIS: EXPLAIN view, saved queries, unused/invalid index diagnostics, SRS overview, configuration recommendations.
8. Strict host-key pinning for ad-hoc SSH (or a clear `--accept-new` opt-in), and server-side backup encryption.
9. Cut a release (0.1.8) containing the Security page, Alert Rules and rollback; run Python 3.9 in CI for the new modules.
10. Automatic rollback policy (opt-in, only to a verified healthy record, with notification) - only after item 1 proves manual rollback on real systems.

## 8. Recommended next phase

**Verify before building more.** One focused phase: (a) run and extend the integration/E2E workflows to cover the new Security page, Alert Rules and deploy -> verify -> rollback on real Compose and kind; (b) fix whatever that finds; (c) release 0.1.8. Only then start the larger feature phases (Compose/K8s backup, then web map, then GeoServer editing) so that new features are not stacked on code that has only ever run against fakes.

## 9. Git and repository state (at inspection time, before this file was added)

* Branch: `main`. Commit: `91dc9a460832c4512e4faad14be16203cc5a8318`.
* `git status`: clean (0 modified, 0 untracked). `main` and `origin/main`: 0 ahead / 0 behind. Stash: empty.
* All earlier changes are committed and pushed. This document is the only new, uncommitted file.
