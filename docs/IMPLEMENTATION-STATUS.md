# GIS Stack - implementation status and verification report

*Updated 2026-11-09 after the integration-testing phase. Base: commit `1b36af2` (pushed). This phase's changes are in the working tree and described below; the newest release tag is `v0.1.7` (it contains none of the work since).*

Statuses (used consistently):

* **COMPLETE** - implemented and verified at the level named.
* **PARTIAL** - partially implemented.
* **NOT IMPLEMENTED** - missing.
* **IMPLEMENTED BUT NOT VERIFIED** - code exists, testing is incomplete or only against fakes.
* **BROKEN** - implemented but failing. *Nothing is currently BROKEN.*

Verification levels: **unit** (fakes), **real** (real Docker/kind/Chromium/PostgreSQL run in this phase), **CI** (GitHub Actions).

## 1. Summary of this phase

* The new Security page, Alert Rules manager, deployment history, health verification and manual rollback were previously only unit-tested. They were now exercised against **real Docker Compose, a real kind cluster (Kubernetes v1.37) and real Chromium**.
* The real runs found **two genuine defects** that unit tests could not see. Both are fixed with regression tests and re-verified on the real systems (section 3).
* Not done: Podman and a native host were not available/tested; Compose/Podman/Kubernetes backup was planned (section 7) but **not implemented**; the classic-page CSP, legacy `/api/*` removal, SSH trust and production-server items were reviewed and prioritised, not changed.
* Release preparation: nothing was tagged or published. 0.1.8 is ready to be proposed once these changes are committed, pushed and the `integration` workflow passes on them.

## 2. What was already implemented (before this phase)

Security page; Alert Rules manager (API, rule store with revision + lock, dashboard page); deployment records, health verification, manual rollback; Compose/Kubernetes managers for PostGIS/GeoServer/MapProxy; native backup/restore with verification, retention, schedule, rollback, encrypted off-site copies; multi-server profiles; signed releases. Details and file references: sections 4 and 5.

## 3. What failed, and how it was fixed

| # | Found by | Problem | Root cause | Fix | Regression tests |
|---|---|---|---|---|---|
| 1 | Real Compose test (`tests/integration/test_deploy_rollback.py`) | A deliberately broken upgrade (PostgreSQL 16 on PostgreSQL 17 data, crash-looping) was reported **healthy** | Right after `docker compose up`, `ps --format json` has an empty `Health` field while `Status` already says `(health: starting)`; the Compose provider therefore reported the row running/ok. The verifier also accepted the first healthy reading before the crash loop began | `providers/compose.py` reads `(health: starting)` / `(unhealthy)` from the status text when `Health` is empty; `deployments.verify` now requires the programs to **stay healthy for a settle period** (`GISSTACK_VERIFY_SETTLE`, default 10 s, and always within the timeout) | `tests/test_providers.py::ComposeHealthTextTests`, `tests/test_deployments.py::SettleTests` |
| 2 | Real kind test (`tests/integration/test_deploy_rollback_k8s.py`) | The **rollback itself failed** on Kubernetes (`apply.sh` timed out waiting for PostgreSQL) | Documented Kubernetes StatefulSet "forced rollback" issue: after the template is corrected, a pod that never became ready is not replaced (reproduced by hand: `updateRevision` was back to the good revision, the pod stayed on the broken one) | `templates/k8s/apply.sh.j2`: `replace_stale_pod` deletes only a pod that is **not ready and on an outdated revision**; its PVC is kept; healthy pods are never touched; called by `apply` and `only postgis`. Golden hash of `k8s/apply.sh` updated deliberately (only that entry changed) | `tests/test_k8s_apply_script.py` (fake kubectl, 5 cases) |
| 3 | Browser tests | Test order dependence: the server stores the chosen language, so a later test started in Georgian | test-only | the `dashboard()` helper selects English explicitly | n/a |

## 4. Feature status matrix

| Area | Feature | Status | Evidence |
|---|---|---|---|
| Security page | permission checks, audit viewer, admin-only API | **COMPLETE** | unit; real Chromium (loads, table, "In order", filter, KA/EN, no console errors) |
| Alert rules | CRUD API, admin-only writes, validation, atomic + revision-guarded updates, enable/disable, audit, dashboard editor, KA/EN | **COMPLETE** | unit; real Chromium: create, refused rule with reason, enable/disable, edit + reset built-in, delete with confirm/cancel, KA/EN switch, phone-width layout, console clean |
| Alert rules | per-rule notification channels | **NOT IMPLEMENTED** (repeat interval + severity only; channels remain in `notify.json`) | - |
| Deployment | history, pre-state capture, post-deploy health verification | **COMPLETE** for Compose and Kubernetes | unit + **real** Compose and kind (after fix 1) |
| Deployment | manual rollback (refusals, current secrets kept, no purge, idempotent) | **COMPLETE** for Compose and Kubernetes | unit + **real**: failed upgrade -> rollback -> data intact -> password unchanged -> repeat refused as "nothing to do" |
| Deployment | native-host rollback and deploy verification | **IMPLEMENTED BUT NOT VERIFIED** (same code path; no real native host run) | unit only |
| Deployment | Podman | **IMPLEMENTED BUT NOT VERIFIED** (`runtime: podman` honoured; podman is not installed here; no test) | - |
| Deployment | automatic rollback on failure | **NOT IMPLEMENTED** (deliberate: rollback is explicit, confirmed, audited) | - |
| Web GIS | base map + layer control, WMS/tile overlays, scale bar, coordinates, zoom, reset, KA/EN, phone width | **COMPLETE** for what exists | real Chromium on the generated map (Leaflet is vendored: loads without internet; tiles 404 because no MapProxy runs in that test) |
| Web GIS | layer catalog, legend, identify, search, WFS, GeoJSON, fullscreen, metadata, CRS info | **NOT IMPLEMENTED** | - |
| GeoServer | version, health, validation, inventory (workspaces, datastores, layers, groups, styles), layer detail, capabilities, preview, workspace create/delete, reload | **COMPLETE** | unit + **real** GeoServer on Compose (28 tests) and kind (4 tests), re-run this phase |
| GeoServer | datastore/layer/style/layer-group editing, catalog backup, extension checks | **NOT IMPLEMENTED** | - |
| PostGIS | inventory, health, read-only query role, audited write mode | **COMPLETE** | unit + real PostGIS 17 (Compose, kind) |
| PostGIS | saved queries, EXPLAIN UI, index diagnostics beyond "has GiST", SRS overview, config recommendations | **NOT IMPLEMENTED** | - |
| Backup | native: create/verify/restore/rollback/retention/schedule/encryption of off-site copies | **COMPLETE** (native) | real bash/tar/PostgreSQL via `GISSTACK_TEST_DOCKER` (passed this phase) + unit |
| Backup | Compose / Podman / Kubernetes | **NOT IMPLEMENTED** (code refuses non-native); plan in section 7 | - |
| Backup | server-side encryption | **NOT IMPLEMENTED** | - |
| Security | auth, scoped tokens, CSRF header, rate limit, Host list, size limit, audit, 0600/0700 files, injection fuzz | **COMPLETE** | unit |
| Security | SSH host keys | **PARTIAL**: pinned/strict for saved server profiles; ad-hoc `--ssh` uses `accept-new` | real sshd for profiles |
| Security | CSP | **PARTIAL**: strict on `/dashboard`; classic page `/` still `unsafe-inline` | unit |
| Security | legacy `/api/*` | **PARTIAL**: 28 routes, 27 deprecated with headers, none removed | unit |
| Packaging | production WSGI server | **PARTIAL**: `--server waitress` exists; the packaged systemd unit and deb/rpm use the development server and run as root | - |

## 5. File references (new or changed in this phase)

* Defect fixes: `gisstack/providers/compose.py`, `gisstack/deployments.py` (`verify`), `gisstack/templates/k8s/apply.sh.j2`, `tests/golden/bundle_full.json`.
* New tests: `tests/integration/test_deploy_rollback.py` (real Compose), `tests/integration/test_deploy_rollback_k8s.py` (real kind), `tests/test_k8s_apply_script.py`, additions to `tests/test_deployments.py`, `tests/test_providers.py`, and `tests/e2e/test_browser.py` (Security page, Alert Rules, web map; 3 new browser tests).
* CI: `.github/workflows/integration.yml` runs both new integration tests (compose and kubernetes jobs). **These workflow changes have not run on GitHub yet.**
* Earlier work (unchanged here): `gisstack/security_report.py`, `gisstack/monitoring/rulestore.py`, `gisstack/deployments.py`, `gisstack/api/routes_*.py`, `.../views/{security,rules,deployments}.js`, docs `DEPLOYMENTS.md`, `MONITORING.md`, `AUDIT-2026-11.md`.

## 6. Exact test results (this phase)

Environment: Linux, Python 3.12 in a venv with Flask/Jinja2/PyYAML/Playwright, Docker 29.9.3 + Compose v5.7, kind v0.33.0 with Kubernetes v1.37.0 (single node), kubectl v1.37.2, Chromium via Playwright.

| Run | Result |
|---|---|
| `python -m unittest discover -s tests` (default, opt-in suites off) | **826 run: 765 passed, 61 skipped, 0 failed** |
| ruff / mypy (86 files) / bandit -ll / `node --test tests/js/*.mjs` | all passed (JS 10/10) |
| Browser: `GISSTACK_TEST_E2E=1 tests.e2e.test_browser` | **7/7 passed** |
| Docker-gated: `GISSTACK_TEST_DOCKER=1` `test_compose` + `test_backup_tool` + `test_deploy_rollback` | **31/31 passed** (the first run of the new Compose test FAILED and exposed defect 1) |
| Compose managers: `GISSTACK_TEST_INTEGRATION=1 test_compose_managers` | **28/28 passed** |
| Kubernetes: `GISSTACK_TEST_K8S=1 test_deploy_rollback_k8s` | **1/1 passed in 353 s** (the first run FAILED and exposed defect 2) |
| Kubernetes managers: `GISSTACK_TEST_K8S=1 test_k8s_managers` | **4/4 passed** |

**Skipped in the default run (61), not counted as passed:** 22 Compose-manager and 6 GeoServer-on-Compose and 5 Kubernetes and 7 browser tests and 1 Compose smoke and 1 rollback-on-Compose (all opt-in; the ones marked above were run separately and passed), 15 MapProxy script tests + 1 mapproxy import (mapproxy not installed here), 1 shellcheck (not installed), 2 installer-in-distro-container (`GISSTACK_TEST_INSTALLER`, not run).

**Not run:** installer in distro containers and deb/rpm package tests; MapProxy script tests; shellcheck/kubeconform/YAML gates; Python 3.9-3.13 matrix (changed modules only parse-checked against the 3.9 grammar); pip-audit; any test on Podman; any test on a real native host; the `integration` workflow on GitHub for these changes.

## 7. Backup and restore: technical plan (not implemented)

Existing native design to reuse: `gisstack-backup` writes `<id>.partial`, checksums every file, writes a manifest, renames on success; restore requires confirmation, takes a verified pre-restore backup, replaces by renaming, validates, rolls back on failure.

**Common design (new module `gisstack/backup_targets.py`, built on `targets.Place` which already runs commands in the right container/pod):**
1. *Create:* PostgreSQL `pg_dump -Fc` streamed out of the container/pod to the operator machine (or a server directory) through the existing transport; GeoServer data and MapProxy cache as tar streams (`docker compose exec ... tar` / `kubectl exec ... tar`); configuration = generated bundle files + a secrets-free manifest of settings. Write `.partial`, checksum, manifest (versions, component list, sizes), rename.
2. *Verify:* re-read and re-checksum; `pg_restore --list` on the dump; tar listing with path-traversal rejection (reuse the native allow-list rules).
3. *Restore (never in place first):* verify -> pre-restore backup -> restore the dump into a **scratch database** and run the health/inventory checks -> only then swap (rename databases) -> health verification (`deployments.verify`) -> automatic switch back on failure. Volumes: extract into a sibling directory/PVC copy first; replace by rename only after validation. Nothing is dropped automatically.

**Per target:**
* *Compose:* `docker compose exec -T postgis pg_dump`; volume files via a short-lived helper container mounting the named volume read-only; works with `runtime: podman` using the same code path (`Place.runtime`).
* *Podman:* identical commands through the podman CLI; rootless volume paths and `podman compose` differences must be tested on a machine with podman (not available here).
* *Kubernetes:* `kubectl exec statefulset/postgis -- pg_dump`; PVC data via `kubectl exec ... tar` (or VolumeSnapshots where the cluster supports them); the Secret/ConfigMap manifests exported without secret values in the manifest; scratch-database restore inside the same pod; cluster-wide operations stay out of scope.

**Verification plan with the environments now available:** extend `tests/integration` with real Compose and kind runs: create backup -> corrupt/delete data -> restore -> verify data and health; corrupted-archive refusal; interrupted restore leaves the original intact; repeat restore idempotent. **Why not implemented now:** a correct implementation is a multi-day feature (three transports, restore swap logic, tests for failure paths) and shipping it half-tested would contradict this phase's goal. It is task 2 below.

## 8. Security review (priorities)

Open, ordered:
1. **Production server:** `gisstack web` defaults to the Werkzeug development server; the packaged systemd unit (`packaging/gisstack-web.service`) uses it, runs as **root** with `NoNewPrivileges=false`, and the deb/rpm do not depend on `waitress`. Mitigations in place: 127.0.0.1 only, token, CSRF header, Host allow-list. Fix path: ship the unit with `--server waitress` and a `Recommends`/`Requires` on waitress, tested in the distro-container package tests.
2. **Classic page CSP:** `/` needs `unsafe-inline` (2 inline scripts, 1 style block, 13 `style=` attributes, 1 `onsubmit=`, inline styles inside JS-built HTML). Not removed: needs the page retired or fully migrated and browser-tested.
3. **Ad-hoc SSH** (`--ssh user@host`) trusts the host key on first use (`accept-new`); strict pinning only for saved profiles.
4. **Legacy `/api/*`:** 28 routes remain (27 deprecated with `Deprecation`/`Link` headers; `/api/logout` has no successor); the classic page uses 18 of them, so removal depends on item 2.
5. **Backups on the server are unencrypted** (they can hold live secrets); only off-site copies can be encrypted.
6. Rollback snapshots hold the applied configuration including secrets (0600, never returned by an API) - as sensitive as `~/.gisstack/secrets`.
7. No audit event on *completion* of a deployment job (start events and the deployment record exist).

Checked and no problem found: no secret in deployment records, audit lines, rule errors or API responses (asserted in tests); rule errors no longer contain server paths; rollback preserves current secrets (asserted on real Compose: same password after rollback).

## 9. Release readiness

* All tests that can run here pass; ruff, mypy, bandit, JS clean; browser 7/7; real Compose and kind runs pass.
* **Before a release:** commit and push this phase, run the `integration` workflow (`workflow_dispatch`) and require it green - in particular the two new rollback jobs, which have only run locally; add the package/installer suites to that run; then bump the version, write the CHANGELOG entry (the `[Unreleased]` section already describes the Security page, Alert Rules and rollback; add the two fixes above) and tag. **No release was created.**
* Candidate: 0.1.8 (new features, no breaking API change; the new `GISSTACK_VERIFY_SETTLE` is optional and defaults to 10 s, so a deployment now takes up to ~10 s longer to report healthy).

## 10. Recommended next 5 tasks

1. Push this phase and run the `integration` workflow; fix anything the CI environment shows (it has never run the new jobs).
2. Compose/Podman backup and restore with real restore verification (plan in section 7); then Kubernetes.
3. Run the new deploy/rollback test on a real **native** host (distro container with systemd, as the installer suite does) and on Podman where available.
4. Production serving: waitress in the packaged service and dependencies; consider dropping root where possible.
5. Retire the classic page (move deploy/clean/projects/existing-programs flows into the dashboard), then remove `unsafe-inline` and the legacy `/api/*` aliases in one documented release; in the same release make ad-hoc SSH pinning explicit.

*After those: web map features (legend, identify, search), GeoServer editing, PostGIS saved queries/EXPLAIN, per-rule notification channels.*
