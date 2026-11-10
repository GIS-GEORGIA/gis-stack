# GIS Stack - implementation status and verification report

*Updated 2026-11-10 with the verified GitHub Actions results for commit `d1382ac` (Universal Backup & Restore, pushed to `main`). Two small local changes are **not committed and not pushed**: the retrying/diagnosable test-image builder (`tests/integration/sysd.py`, `tests/test_sysd_helper.py`) and this document. Newest release tag: `v0.1.7` (contains none of the work since).*

Statuses (used consistently):

* **COMPLETE** - implemented and verified at the level named.
* **PARTIAL** - partially implemented.
* **NOT IMPLEMENTED** - missing.
* **IMPLEMENTED BUT NOT VERIFIED** - code exists, testing is incomplete or only against stand-ins.
* **BROKEN** - implemented but failing. *Nothing is currently BROKEN.*

Verification levels: **unit** (fakes / local stand-ins), **real** (real Docker, kind, Chromium, PostgreSQL, GeoServer, gpg run in this phase), **CI** (GitHub Actions).

## 1. GitHub Actions results (verified, not assumed)

**Commit `696076f`** (previous): `tests` and `codeql` passed; the `integration` workflow failed only in `browser end-to-end` (`test_6_alert_rules_manager`), a race in my own test (it waited on the text "Default", which already matched the row's button). Fixed in `d1382ac`.

**Commit `d1382ac`** - push-triggered `tests` and `codeql`: **both succeeded** (verified with `gh run list --commit`). **`integration` workflow, dispatched once manually, run 42766456604, commit `d1382ac841bd...` (verified against the run's `headSha`) - first attempt: FAILURE, caused by exactly one job; after re-running only that job (`gh run rerun --job`, no new dispatch) the run is SUCCESS with all 9 jobs green:**

| Job | Result | What the log shows |
|---|---|---|
| gate | success | - |
| browser end-to-end (Playwright) | **success** | `test_browser.py` 7/7 ok, including `test_6_alert_rules_manager` (**the race fix works on CI**); `test_browser_backup.py` 1/1 ok (backup page: create, details, verify, export, restore with typed confirmation, delete) |
| compose (real Docker) | **success** | `test_compose` ok; `test_deploy_rollback` (deploy -> failed upgrade -> rollback) ok; `test_compose_managers` 28/28; **`test_stack_backup_compose` 10/10** (PostgreSQL 9 + GeoServer 1), 196 s |
| kubernetes (kind) | **success** | `test_k8s_managers` 4/4; `test_deploy_rollback_k8s` ok; **`test_stack_backup_k8s` 10/10** (PostgreSQL 9 + GeoServer 1), 212 s |
| installer (ubuntu-24.04) | **success** | installer test ok (522 s); deb/rpm package test ok (241 s) |
| installer (debian-12), (rocky-9), (alma-9) | **success** | installer tests ok |
| installer (ubuntu-22.04) | **FAILURE on attempt 1, SUCCESS on attempt 2** | `docker build` of the test image `gisstack-it-ubuntu-22.04:1` exited 1 after 51 s - **before any gis-stack code ran**. The step that failed is the image's `apt-get update/install` inside `docker build`. The helper discarded the build's stderr, so the log does not contain the actual apt error: **the root cause is not proven.** The same job passed on the previous run, nothing in `d1382ac` touches the installer or this helper, and a from-scratch local `docker build --no-cache` of the identical Dockerfile succeeded today (3 min 57 s), which is consistent with a transient mirror/network failure - but that remains an inference. The deb/rpm package step of attempt 1 was skipped because the installer step failed. **Attempt 2 (same commit, same code, only that job re-run): installer test ok and deb/rpm package test ok** (log lines verified). A same-code retry passing, the previous run passing and the local rebuild succeeding all point to a transient failure of the image build, but because the original error text was never captured, **the cause remains unproven**. |

**Podman:** no Podman job exists and Podman is not available on the runners used; nothing was run. **Kubernetes backup and restore: passed on CI. Docker Compose backup and restore: passed on CI. Ubuntu 24.04: passed.**

**Local follow-up (uncommitted):** `sysd.build()` retries a failing image build up to 3 times with back-off and raises an error that includes the build's own output (so the next failure explains itself); 4 unit tests in `tests/test_sysd_helper.py` cover retry, back-off, single build on success, the reported reason, and no rebuild of an existing image. This does not fix a cause that has not been identified; it removes the blind spot and tolerates transient mirror failures. The ubuntu-22.04 job was re-run after approval and passed (above); the retry helper itself is committed in the commit that follows `d1382ac`.

## 2. Backup and restore for Compose / Podman / Kubernetes (this phase)

### What was built
`gisstack/backups/` (`model.py`, `store.py`, `provider.py`), `gisstack/api/routes_stackbackup.py`, `gisstack/webgui/static/dashboard/js/views/stackbackups.js` (+ `backups.js` delegating for non-native targets, `api.js` `postFile`), CLI `gisstack stack-backup ...` in `gisstack/cli.py`, `targets.WORKLOADS` extended (`caddy`, `qgis_server`). Documentation: `docs/BACKUP-RESTORE.md`, `docs/BACKUP-RESTORE-ARCHITECTURE.md`. The native `gisstack-backup` tool and `gisstack backup` are unchanged (their tests, including the Docker-gated PostgreSQL round trip, were re-run and pass).

### Component status

| Component | Status | Evidence |
|---|---|---|
| Backup format, atomic sealing, manifest + SHA256SUMS, versioning (`schema`), verification, retention, restore state file | **COMPLETE** | unit (34+ tests) and real Compose + real kind |
| Archive safety (`scan_tar`: absolute names, `..`, devices, hard links outside, members below symlinks), re-scanned right before extraction | **COMPLETE** | unit |
| Optional encryption (gpg, passphrase or recipients; key never stored; verify without key) | **COMPLETE** | unit + real Compose + real kind |
| PostgreSQL/PostGIS backup, staged restore (scratch DB), reversible rename swap, undo, interrupted-restore recovery | **COMPLETE** on Compose and Kubernetes | real: 9 cases each (create/verify/inspect/no secrets, restore keeps the old data + health check + automatic pre-restore backup, corrupted/missing/extra files refused, unrestorable dump fails before the swap, failed health check undoes the swap, interrupted restore recovered and recovery idempotent, encrypted + wrong key, stopped workload, interrupted backup) |
| GeoServer data directory backup, staged restore, swap, restart, undo | **COMPLETE** on Compose and Kubernetes | real GeoServer 2.28.6: workspace created -> backup -> another workspace created -> restore -> original state back, replaced data kept, no plaintext admin password in the archive |
| Caddy data, QGIS projects, MapProxy configuration, MapProxy cache | **IMPLEMENTED BUT NOT VERIFIED** (same tar code path as GeoServer; no real Caddy/QGIS/MapProxy backup was run) | unit (local stand-in) only |
| Docker Compose provider | **PARTIAL** (PostgreSQL + GeoServer verified real; the rest above) | real Compose locally **and on GitHub Actions** (run 42766456604, 10/10) |
| Kubernetes provider | **PARTIAL** (same scope) | real kind v1.37 locally **and on GitHub Actions** (run 42766456604, 10/10) |
| Podman provider | **IMPLEMENTED BUT NOT VERIFIED** (Compose code with `podman`; Podman is not installed here) | unit (class selection, format) |
| `--mode ssh` (commands over SSH, backup stored locally) | **IMPLEMENTED BUT NOT VERIFIED** | not run |
| API `/api/v1/stack-backups/*` (list, inspect, verify, check, create, restore, recover, delete, prune, export), scopes, validation, audit, no secrets/paths in responses | **COMPLETE** | unit (7 tests, contract-checked, scope matrix test updated) |
| Dashboard Backups page (create, details, verify, export, restore with typed confirmation + live log, delete, retention, recover banner, KA/EN, phone width, console clean) | **COMPLETE** for UI behaviour | real Chromium against the real API with a local stand-in workload (`test_browser_backup.py`; passed locally and on GitHub Actions); UI + real containers together were **not** tested end to end |
| CLI `gisstack stack-backup ...` | **IMPLEMENTED BUT NOT VERIFIED** against a real stack | unit with the stand-in workload |
| Scheduled backups for container targets | **NOT IMPLEMENTED** (native has its systemd timer) | - |
| Off-site shipping / upload of container-target backups | **NOT IMPLEMENTED** (export download exists) | - |
| Server-side (at rest) encryption by default | **NOT IMPLEMENTED** (opt-in per backup) | - |
| Native target backup/restore (unchanged) | **COMPLETE** | unit + real (Docker-gated PostgreSQL round trip re-run: 61 tests OK) |

### What the real environments found (and what was fixed)

| # | Found by | Problem | Fix | Regression test |
|---|---|---|---|---|
| 1 | real Compose | `operation.log` created with mode 0664 (process umask) | created with `os.open(..., 0o600)`; `seal()` also forces 0600 on every payload | integration test 1 (modes of every file), `test_seal_is_atomic_and_private` |
| 2 | real Compose | a wrong decryption key surfaced a raw crypto error instead of a clean refusal | wrapped into `RestoreError` raised before anything is touched | integration test 7 |
| 3 | real Compose | leaked pipe handles (`ResourceWarning`) in the streaming capture | pipes closed in `finally`; suites run with `-W error::ResourceWarning` | all integration tests |
| 4 | real kind | with the workload scaled to 0, `kubectl exec` waits ~60 s for a pod and the backup died with `TimeoutExpired` | `running()` treats a timeout as "not running"; the Kubernetes provider checks `readyReplicas` first | integration test 8 + `test_running_is_false_when_the_command_hangs...` |
| 5 | unit | an empty volume produced a "valid" empty archive (only the `./` entry) | members are counted without the root entry; empty archive refused | `test_an_empty_volume_is_not_a_backup` |
| 6 | review | encrypted archives could not be scanned at verify time | archives are scanned again after decryption, right before extraction | `test_an_unsafe_archive_is_refused_at_extraction_time...` |
| 7 | test design | shared test cases hard-coded `compose` as the target; one counter compared the wrong sets | test fixes (no product change) | - |

One existing test was changed on purpose: `tests/test_targets.py::test_unknown_component_or_target_is_an_error` used `caddy` as its example of an unknown component; `caddy` is now a known workload, so the example is `webmap`. The test still asserts that an unknown component raises. The read-only POST allow-list in `tests/test_api.py` gained the three new read routes (list, inspect, verify).

### Security review of the backup system
* **File permissions:** store 0700, files 0600, asserted in real runs. **Keys:** never stored (not in manifest, log, archive; asserted). **Secret exclusion:** no password/token/Secret/role hash is archived; asserted on real PostgreSQL and GeoServer data; the GeoServer data directory holds GeoServer's own hashed credentials - documented, and encryption is recommended. **Access control:** `read`/`operate`/`admin`, every route in the scope matrix test. **Paths:** ids validated by a fixed pattern; delete resolves the path and refuses anything outside the store (symlink and `..` tests); no user-supplied file paths in the API. **Archives:** scanned before use and again before extraction. **Integrity:** SHA256SUMS + manifest hashes + file-set check. **Audit:** `stack_backup.create|restore|recover|delete|prune|export`, refusals logged.
* **Remaining:** passphrase encryption is CLI-only (by design); backups at rest are not encrypted unless asked; restore of PostgreSQL requires the roles to exist; GeoServer data is copied live (not quiesced); `--mode ssh` untested.

## 3. Everything else (unchanged this phase unless stated)

| Area | Status | Evidence |
|---|---|---|
| Security page, Alert Rules manager | **COMPLETE** | unit + real Chromium (browser suite 7/7 locally and on GitHub Actions run 42766456604; the `test_6` race fix is verified on CI) |
| Deployment records, health verification (with settle period), manual rollback | **COMPLETE** on Compose and Kubernetes | real (previous phase; CI integration jobs passed on GitHub for `696076f`) |
| Native-host deployment rollback | **IMPLEMENTED BUT NOT VERIFIED** | unit only |
| Podman deploy/rollback | **IMPLEMENTED BUT NOT VERIFIED** | - |
| Automatic rollback on failure | **NOT IMPLEMENTED** (deliberate) | - |
| Web map (scale, coordinates, layers, WMS/tiles, KA/EN) | **COMPLETE** for what exists; legend, identify, search, WFS, GeoJSON, fullscreen, CRS info **NOT IMPLEMENTED** | real Chromium |
| GeoServer manager (read + workspaces) | **COMPLETE**; datastore/layer/style/layer-group editing **NOT IMPLEMENTED** | unit + real |
| PostGIS manager (inventory, read-only SQL, audited writes) | **COMPLETE**; saved queries, EXPLAIN UI, index diagnostics, SRS overview, config recommendations **NOT IMPLEMENTED** | unit + real |
| Auth, scoped tokens, CSRF, rate limit, audit, 0600/0700 files | **COMPLETE** | unit |
| SSH host-key verification | **PARTIAL** (pinned for saved profiles; ad-hoc `--ssh` trusts on first use) | real sshd |
| CSP of the classic page `/` | **PARTIAL** (`unsafe-inline` remains) | unit |
| Legacy `/api/*` | **PARTIAL** (28 routes, 27 deprecated with headers, none removed) | unit |
| Production WSGI server | **PARTIAL** (`--server waitress` exists; the packaged service uses the development server as root) | - |

## 4. Exact test results (this phase)

Environment: Linux, Python 3.12 venv (Flask, Jinja2, PyYAML, Playwright), Docker 29.9.3 + Compose v5.7, kind v0.33.0 / Kubernetes v1.37.0 (single node, deleted afterwards), kubectl v1.37.2, Chromium via Playwright.

| Run | Result |
|---|---|
| Default suite `python -m unittest discover -s tests` | **888 run: 806 passed, 82 skipped, 0 failed** (an intermediate run had 1 failure - the `targets` test above - fixed and re-run) |
| ruff / mypy (91 files) / bandit -ll / `node --test tests/js/*.mjs` | passed / passed / 0 findings / 10 of 10 |
| Real Docker Compose: `test_stack_backup_compose` (`GISSTACK_TEST_INTEGRATION=1`, with `-W error::ResourceWarning`) | **10/10 passed** (PostgreSQL 9 + GeoServer 1); first run: 6/9 PostgreSQL + 1/1 GeoServer - exposed defects 1-3 and one test bug |
| Real kind: `test_stack_backup_k8s` (`GISSTACK_TEST_K8S=1`) | **10/10 passed** (PostgreSQL 9 + GeoServer 1, 383 s); first run: 8/10 - exposed defect 4 and one test bug |
| Browser (Chromium): `test_browser` + `test_browser_backup` (`GISSTACK_TEST_E2E=1`) | **8/8 passed** (7 existing + 1 new backup page flow) |
| Native backup, Docker-gated: `test_backup_tool`, `test_backup_manager`, `test_backup_crypto` (`GISSTACK_TEST_DOCKER=1`) | **61/61 passed** |
| New unit tests | `test_stack_backup.py` 34, `test_stack_backup_api.py` 7 (all pass) |

**Skipped in the default run (82), not counted as passed:** 22 Compose-manager + 6 GeoServer-on-Compose + 9 + 1 backup-on-Compose + 15 Kubernetes + 8 browser tests (all opt-in; the backup, browser and native-backup ones were run separately and passed, as listed above), 15 MapProxy script tests + 1 mapproxy import (mapproxy not installed), 1 shellcheck (not installed), 2 installer-in-distro-container, 1 Compose smoke, 1 deploy-rollback-on-Compose (opt-in).

## 5. What could not be tested, and why

* **Podman** - not installed in the development environment; backup, restore, deploy and rollback on Podman are untested.
* **`--mode ssh`** for backups - no SSH-to-Docker environment set up for this phase.
* **Caddy, QGIS and MapProxy** backup/restore against real workloads - not run (time); same code path as GeoServer.
* **A real native host** for deployment rollback; **Python 3.9-3.13** matrix; the **installer/package suites**, **MapProxy script tests** and **shellcheck** (tools/prerequisites missing locally).
* **Podman jobs in GitHub Actions** - none exist; the runners used have no Podman set up.

## 6. Remaining limitations and security issues

1. The one image-build failure of ubuntu-22.04 (section 1) went away on retry; its cause is unproven. The retry/diagnostics helper is meant to make a recurrence tolerated and self-explaining.
2. Container-target backups: no scheduling, no off-site shipping, not encrypted at rest unless requested; roles/passwords are not restored (by design); GeoServer data is copied live.
3. Classic page `/` still needs `unsafe-inline`; 28 legacy `/api/*` routes remain; ad-hoc SSH trusts the host key on first use; the packaged web service uses the development server as root.
4. Native backups on the server are unencrypted; rollback snapshots hold secrets (0600, never returned by an API).
5. Web map, GeoServer editing and PostGIS tooling gaps listed in section 3.

## 7. Release readiness

No release was created. The new Compose/kind/browser backup jobs pass on GitHub and run 42766456604 is fully green after the single re-run. Before proposing 0.1.8: run the `integration` workflow once more on the final commit (the push does not trigger it), then bump the version and tag. No release was created.

## 8. Recommended next 5 tasks

1. Run the `integration` workflow once on the commit that contains the image-builder retry to confirm it stays green without a manual re-run.
2. Real-environment tests for Caddy, QGIS and MapProxy components and for `--mode ssh`; run the whole backup suite on Podman where available.
3. Scheduled backups and off-site shipping (encrypted) for container targets, reusing the store/retention code.
4. Production serving: waitress in the packaged service and dependencies; consider dropping root.
5. Retire the classic page, then remove `unsafe-inline` and the legacy `/api/*` aliases in one documented release; make ad-hoc SSH pinning explicit.
