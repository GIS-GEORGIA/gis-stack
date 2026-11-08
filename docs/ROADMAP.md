# GIS-STACK — Roadmap

*Derived from [TECHNICAL_AUDIT.md](TECHNICAL_AUDIT.md) and [ARCHITECTURE.md](ARCHITECTURE.md). Status: proposal awaiting review — **nothing below is implemented yet**.*

## 1. Principles and guardrails

1. **Evolve, do not rewrite.** Every phase lands as small, independently reviewable pull requests; the existing public functions (`ops.status`, `deploy.deploy`, `existing.run`, …) keep their signatures until a deprecation release.
2. **Backward compatibility.** Existing `stack.json`/project files and generated bundles keep working. New config fields are optional; stricter validation first runs in *warn* mode for one release (`GISSTACK_STRICT_CONFIG=warn|error`, default `warn`→`error` at the release after) except for the P0 injection cases, which are rejected immediately because the previous behaviour is unsafe.
3. **Three targets stay green.** Native, Compose and Kubernetes are covered by CI before and after every refactor.
4. **Feature flags** for large features (`GISSTACK_FEATURES=dashboard,geoserver_manager,postgis_manager,mapproxy_manager,backups,monitoring,multi_server,api,plugins`), listed in `docs/USAGE.md`. Flags default **off** until the phase is accepted.
5. **Georgian and English** for every new user-visible string (enforced by the existing i18n coverage test).
6. **Real data only.** No simulated metrics; every collector is tested against real system output captured as fixtures *and* against real services in integration tests.
7. **Safe by default.** No destructive operation without a verified backup and an explicit confirmation; read-only first for database access.
8. **Each phase ends with a report**: tests added/passed/failed, behaviour not verified, open issues — before the next phase starts.

## 2. Recommended order (and why)

| # | Phase | Why here |
|---|---|---|
| 1 | **1a P0 fixes** | Code-execution and data-loss risks must not be carried into new features. |
| 2 | **11a Quality gates + characterisation tests** | Safety net for every refactor that follows (CLI/deploy are currently untested). |
| 3 | **1b Security and reliability** | Needs the gates; introduces the runner/journal reused later. |
| 4 | **2 Architecture (incremental)** | Transport, providers, service layer — required by 3–10. |
| 5 | **7 Backup & restore** | Safety prerequisite for any manager that changes data. |
| 6 | **8 Monitoring core** | Dashboard needs real metrics and history. |
| 7 | **10 API & extensibility** | One contract for UI and automation before building screens on it. |
| 8 | **3 Dashboard** | Built on the API. |
| 9 | **4, 5, 6 Managers** (GeoServer → MapProxy → PostGIS) | Independent modules on the API; PostGIS editor last (strictest permissions). |
| 10 | **9 Multi-server** | Reuses runner, journal, API, credentials model. |
| 11 | **11b / 12** | Consolidate integration/E2E tests and documentation (both grow continuously from step 2). |

Suggested release train: **0.2** (1a+11a) · **0.3** (1b+2) · **0.4** (7) · **0.5** (8+10) · **0.6** (3) · **0.7** (4–6) · **0.8** (9) · **1.0** (stabilisation, signed releases, docs).

Complexity is expressed as **S** (≤3 d), **M** (4–8 d), **L** (9–15 d), **XL** (>15 d) of focused work, ±50 %.

## 3. Phases

### Phase 1a — P0 fixes · **M** — ✅ implemented (see CHANGELOG *Unreleased* and `SECURITY.md`)
- **Scope:** S-01, S-02, S-05 (quick wins).
- **Work:** (1) `core/validation`: strict schema for the *whole* config (types, ranges, patterns, CIDR, e-mail, URL/image, K8s names, absolute paths, no control characters, `additionalProperties: false` with a clear migration message); (2) apply `shq`/YAML/Caddy-safe filters to **every** value in templates; (3) fail-closed backups: manifest + SHA-256, abort on any error, verify downloaded archive, never reuse a backup directory, explicit confirmation token before data-destroying actions; (4) atomic secret writes (`0600`/`0700`), `~/.gisstack` `0700`.
- **Tests:** a *config fuzz* test injects metacharacters (`$()`, backticks, `;`, newline, `|`, quotes, `../`) into every string field and asserts either rejection or inert output in every generated file (and sources `stack.env` in a sandbox); backup tests with a failing `pg_dump` must never reach removal; permission tests.
- **Exit criteria:** the three verified injection cases are rejected; failed backup aborts before any removal; full suite green.
- **Risks:** stricter validation may reject unusual-but-legitimate values → warn mode + documented allowed alphabets.

### Phase 11a — Quality gates and characterisation tests · **M** — ✅ implemented
- **Work:** CI jobs for `ruff` (baseline then enforce), `mypy` (baseline file, ratchet), `shellcheck` on templates rendered in CI and repository scripts, YAML lint, `kubeconform` on rendered manifests, `docker compose config`, `pip-audit`, CodeQL, Dependabot; Python matrix 3.9–3.13; coverage report with a ratchet (start at 59 %); characterisation tests for `cli.py` and `deploy.py` (stubbed `ssh`/`tar`).
- **Exit criteria:** CI blocks on lint/type/audit regressions; `cli.py` and `deploy.py` ≥ 70 %.

### Phase 1b — Security and reliability · **L** — ✅ implemented (S-03, S-04, S-06, S-08 mostly, S-10, R-01, R-02, R-04, R-05; rotation, schema_version, secret providers). Left: S-07, S-09, R-03 (Phase 7), R-06/R-07 (Phase 2)
- **Scope:** S-03, S-04, S-06, S-08, S-10, R-01…R-04, R-06.
- **Work:** central **runner** (deadlines, SSH keep-alive, cancellation, process-group kill, secret redaction) used by deploy/ops/existing; operation **journal**; remote `flock` lock; **preflight** module (disk, RAM, ports, DNS, repositories, time sync, OS/arch) with structured results; secrets via stdin/`PGPASSFILE`/temp files; pinned, verified downloads in `catalog.py`; hash-pinned MapProxy requirements; web: expiring sessions, rate limit, `Host` allow-list, request-size limit, audit log, production WSGI server option, documented privilege model; `schema_version` with migrations; credential **rotation** command (PostgreSQL role, GeoServer admin, Tomcat manager) and environment-variable/secret-file inputs; optional **secret-provider interface** (env/file first, Vault/SOPS adapters later).
- **Tests:** timeouts/cancel with a hung fake `ssh`; lock contention; audit-log content (no secrets); session expiry; rotation round-trip in containers.
- **Exit criteria:** no secret appears in any `ps`/`/proc` argument in the installer; every download is verified; operations are cancellable and bounded.

### Phase 2 — Architecture (incremental) · **L** — ✅ implemented (core, transport, providers, services, Tk patching). Left for later: per-service modules, typed config model, shell logic out of Jinja
- **Order of extraction** (each step keeps old import paths working through thin re-exports): `core/paths`, `core/exceptions` + `core/logging` + `core/events` → `core/transport` (from `ops`/`deploy`) → `providers/{native,compose,kubernetes}` behind `Provider` (`deploy/status/control/logs/clean/apply/backup`) → service modules (`services/postgis|geoserver|qgis_server|mapproxy|caddy`) holding per-service knowledge (ports, health probes, backup paths, import rules) → **service layer** used by web, Tk and CLI → remove import-time monkeypatching in `desktop.py` → typed config model generated from the schema.
- **Evaluate first, move second:** modules that already have sound boundaries (`catalog`, `discover` parsers, `projects`, `i18n`) stay where they are; the proposed tree in the brief is a target, not a mandate.
- **Exit criteria:** no cross-module imports of private functions; behaviour unchanged (golden tests on generated bundles and parser fixtures); mypy ratchet advanced.

### Phase 7 — Backup and restore · **XL** — ✅ implemented for the native target (Compose/Kubernetes, encryption, base backups, GUI wizard: later)
- **Scope:** PostgreSQL (logical dumps, optional base backups), GeoServer data dir, MapProxy cache (opt-in, may be large), QGIS projects, GIS-STACK configuration, deployment metadata.
- **Work:** `backup/manager` (consistent snapshots: `pg_dump` with snapshot, GeoServer data-dir quiesce or copy with checksums), `scheduler` (systemd timer or cron generated per server), history + **retention policies**, manifest with checksums, **integrity checks**, `restore` (wizard: select → **preview/dry-run** → validate prerequisites → restore to staging or in place with automatic pre-restore backup → **recovery validation** with health probes), no silent overwrite, progress events, documented recovery runbook.
- **Tests:** round-trip in containers (PostgreSQL/PostGIS + GeoServer data dir), corrupted-archive detection, interrupted restore, retention pruning, permission checks.
- **Exit criteria:** restore reproduces the original state in an automated test; failure paths leave the system recoverable.

### Phase 8 — Monitoring and alerts · **L** — ✅ implemented for native/Compose (Kubernetes: service state only; dashboard = Phase 3)
- **Work:** collectors with real sources (`/proc`, `statvfs`, `ss`, systemd/compose/k8s state, PostgreSQL `pg_stat_*`, GeoServer REST/health, MapProxy/Caddy/QGIS HTTP probes, response times), SQLite ring-buffer history (no external dependency), health/error history, threshold alerts (webhook/e-mail as providers), `/api/v1/metrics` JSON endpoint, optional Prometheus text export behind a flag.
- **Tests:** collector parsers against captured fixtures, alert evaluation, retention; integration with real services in CI containers.
- **Exit criteria:** no synthetic data path exists; unit and integration tests cover each collector.

### Phase 10 — API and extensibility · **L** — ✅ implemented (`/api/v1` for every operation incl. dashboard; scoped tokens; generated+tested OpenAPI/API.md; five plugin kinds with loader isolation and conformance kit; legacy `/api/*` deprecated, to be removed next release)
- **Work:** `/api/v1` with versioned, schema-validated requests and consistent envelope `{data, error, meta}`; token scopes (read / operate / admin) and audit; OpenAPI document generated from schemas; existing `/api/*` routes remain as compatibility aliases for one release. **Plugin interfaces** (not a marketplace): `ServiceProvider`, `MonitoringProvider`, `BackupProvider`, `AuthProvider`, `DataSource` as Python entry points with a small, versioned contract and a conformance test kit.
- **Exit criteria:** UI uses only the API; API docs are generated and tested.

### Phase 3 — Professional web dashboard · **XL** (feature flag `dashboard`) — ✅ first release implemented (read/operate pages; deploy/clean/projects/existing-programs workflows are still in the classic page; GeoServer/PostGIS/MapProxy managers are Phases 4-6)
- **Approach:** static assets (separate JS/CSS, nonce-based CSP), componentised vanilla JS (no build step) unless the owner decides otherwise; responsive layout, accessibility (labels, focus, ARIA, contrast in both themes), loading/empty/error states, KA/EN via the existing catalog.
- **Navigation:** Dashboard · Infrastructure · PostgreSQL/PostGIS · GeoServer · QGIS Server · MapProxy · Web Maps · Deployments · Monitoring · Backups · Logs · Settings.
- **Dashboard:** health, service cards, CPU/RAM/disk, PostgreSQL connections, GeoServer availability, MapProxy cache usage, recent deployments/errors/warnings — all from Phase 8 collectors, with auto/manual refresh.
- **Infrastructure:** version, installation type, state, autostart, health, listen address/port, start/stop/restart, enable/disable, logs — a re-skin of the current diagnostics on the service layer.
- **Exit criteria:** every current web workflow is reachable in the new UI before the old page is retired.

### Phase 4 — GeoServer manager · **L** (flag `geoserver_manager`) — ✅ first release (native target; health, inventory, capabilities, preview, validation, workspace create/delete, reload). Left: datastore/layer/style/layer-group editing, security rules, Compose/Kubernetes, GeoServer 3.0 container test; see `docs/GEOSERVER.md`
- Official REST API only (`/rest`), server-side credentials (never sent to the browser), honour GeoServer roles; workspaces, datastores, layers, layer groups, styles, WMS/WFS/WMTS capabilities, layer preview (WMS GetMap proxied), health checks, configuration validation, visual layer browser with endpoints.
- **Tests:** against a real GeoServer container; error mapping; permission handling.

### Phase 5 — PostGIS manager · **L** (flag `postgis_manager`) — ✅ first release (native target; inventory, health, read-only SQL editor with dedicated role, time-boxed write mode with audit). Left: Compose/Kubernetes, query history/saved queries, EXPLAIN visualisation, data import/export; see `docs/POSTGIS.md`
- Inventory: databases, schemas, tables, spatial columns, geometry types, SRIDs, spatial indexes, sizes, connections, `pg_stat_statements` overview, extension availability, health.
- **SQL editor:** read-only transaction by default (`SET TRANSACTION READ ONLY`), statement timeout, row/size limits with pagination, dedicated least-privilege role, writes only after an explicit per-session elevation with confirmation and audit log; server-side parsing is *not* trusted as a security boundary — the database role and read-only transaction are.
- **Tests:** attempts to write in read-only mode, long queries, large results, permission failures — against a real PostGIS container.

### Phase 6 — MapProxy manager · **M** (flag `mapproxy_manager`)
- Cache statistics and size, grid and layer management (validated config edits with automatic backup), seed management and progress, cache cleanup with prominent destructive-operation warnings, tile preview, configuration validation, health checks.
- **Tests:** real MapProxy 7 in CI (the repository already tests config loading against it).

### Phase 9 — Multi-server management · **XL** (flag `multi_server`)
- Server profiles with environment tags (production/development/testing); credentials stored outside project files (OS keyring or `0600` secrets file; SSH keys referenced by path/agent only, never embedded); pinned host keys; remote health, deployment and service management through the existing runner/providers; deployment history per server (journal); server comparison (config drift, versions, health).
- **Exit criteria:** two servers can be operated from one UI with independent credentials; no key material in any config file.

### Phase 11b — Integration and end-to-end tests · **L**
- Containerised integration suites (PostgreSQL/PostGIS, GeoServer, MapProxy, Compose, kind), installer tests in distro containers (Ubuntu 22.04/24.04, Debian 12, Rocky/Alma 9) using systemd-enabled images, deb/rpm install tests, browser E2E (Playwright) for authentication → configure → preview → deploy (dry-run/container) → manage services → backup/restore. Ordinary CI never touches production infrastructure; privileged/long jobs run nightly or on label.

### Phase 12 — Documentation and release quality · **M**
- Installation, upgrade, backup/recovery, troubleshooting guides; API reference (generated); architecture diagrams kept in `docs/ARCHITECTURE.md`; threat model in `SECURITY.md`; synchronized Georgian/English versions of user-facing guides; signed releases (Sigstore/cosign or minisign) with verification in `gisstack update`.

## 4. Cross-cutting acceptance checklist (per phase)

- [ ] Plan and affected files listed in the PR description.
- [ ] Unit tests added; complete existing suite green; coverage did not drop.
- [ ] Georgian/English strings added.
- [ ] Docs and `CHANGELOG.md` updated; migration notes if config changed.
- [ ] Manual or automated check on native (Ubuntu + Rocky), Compose, Kubernetes for anything touching them.
- [ ] Phase report: tests passed/failed, unverified areas, open issues.

## 5. Decisions needed before Phase 1b

1. Minimum Python: keep 3.9 (RHEL 9) or raise to 3.10+.
2. Front-end approach for the dashboard (vanilla + static assets recommended).
3. Release signing tool.
4. Privilege model for the web GUI (root service vs separate privileged helper).
5. Monitoring storage (bundled SQLite recommended).
6. Whether strict config validation should reject unknown keys immediately or warn for one release.
