# GIS-STACK — Technical Audit (Phase 0)

*Status: Phase 0 only. No code was changed for this audit. Audited revision: `main` after commit "Ask before installing Tkinter for the desktop GUI" (package version 0.1.2 + unreleased work).*
*Related: [ARCHITECTURE.md](ARCHITECTURE.md) · [ROADMAP.md](ROADMAP.md)*

> **ქართულად, მოკლედ:** აუდიტმა იპოვა 2 კრიტიკული (P0) საკითხი: (1) კონფიგურაციის მნიშვნელობები უსაფრთხოდ არ გადადის გენერირებულ shell სკრიპტებში და root-ით გაშვებულ `install.sh`-ში (დადასტურებულია), (2) არსებული პროგრამების წაშლისას ბექაფის ჩავარდნა წაშლას არ აჩერებს. დანარჩენი დეტალები და პრიორიტეტები ქვემოთაა.

## Remediation status

| ID | Status | Notes |
|---|---|---|
| S-01 | **Fixed (Phase 1a)** | `validation.py`, quoting filters in every shell/YAML template, fuzz test (mutation-checked). |
| S-02 | **Fixed (Phase 1a)** | Fail-closed backup script, SHA-256 manifest, archive/dump verification, exclusive creation, confirm token for `+purge`. Restore command: Phase 7. |
| S-05 | **Fixed (Phase 1a)** | `fsutil`, 0600/0700 at creation, atomic replace, remote `umask 077`. |
| discover | **Fixed (found while testing Phase 1a)** | A foreign process on a port of one of *our own* services (nginx on :80) was not reported as a conflict. |
| T-01, T-04, T-05, T-07 | **Fixed (Phase 11a)** | CI gates (ruff, mypy, shellcheck, YAML/Compose/Kubernetes validation, pip-audit, bandit, CodeQL, Dependabot), Python 3.9–3.13 matrix, `cli.py`/`deploy.py` characterisation tests (coverage 59 % → 74 %), package build/install checks. Found and fixed: a Python 3.9 import regression, undefined-name bugs in Tk callbacks, a leaked pipe in `deploy._stream`. |
| T-02, T-03, T-06 | Open | Integration, security-regression (partly done in 1a) and end-to-end tests: Phase 11b. |
| S-03, S-04 | **Fixed (Phase 1b, part 1)** | Secrets via stdin (`psql` hex `\set`, `curl --config -`); pinned SHA-256 for GeoServer, extensions and Caddy; `pip --require-hashes` for the MapProxy venv; Tomcat checksum from Apache's own host. Verified on Ubuntu 24.04. Rocky not re-run. |
| S-08 | Mostly fixed | SSH keep-alive; native bundle removed from the server after deployment. Host-key pinning remains (Phase 9). |
| S-06, S-10, R-01, R-02, R-04, R-05 | **Fixed (Phase 1b, part 2)** | Runner (deadline/idle/cancel/process-group kill/redaction), journal + locks, pre-flight, web sessions/rate limit/Host list/size limit/audit log/bounded jobs, `schema_version`, secret providers, credential rotation. See SECURITY.md. |
| A-01, A-02, A-04, A-05, A-06, A-09 (paths) | **Fixed (Phase 2)** | `transport` module; `providers/{native,compose,kubernetes}` behind one `Provider` interface (ops/deploy are thin fronts); `ops.py` 625 → 140 lines; `core` package with one error hierarchy (stable `code`s), `paths`, `events`, redacting `log`; Tk translation patching is explicit and reversible. Enforced by `tests/test_architecture.py`; behaviour pinned by golden tests (`tests/golden/`). |
| R-03 | **Fixed for native (Phase 7)** | `gisstack-backup`: verified pre-restore backup, rename-based replacement, recovery validation, automatic rollback; `existing.run` still uses its own backup flow for foreign programs. Compose/Kubernetes: open. |
| A-03 | Partly fixed | `services.py` holds the deploy rules shared by CLI, web and Tk (pre-flight, conflicts, existing-programs flow). The UIs still render separately; the versioned API is Phase 10. |
| A-07, A-08 | Open | Shell logic is still in Jinja; the config is still `dict[str, Any]` (typed model planned). mypy is now strict for the new modules (`disallow_untyped_defs`). |
| S-07, S-09, S-11, S-12, R-03, R-06, R-07 | Open | S-07/S-09 need decisions (UI assets, signing tool); R-03 is Phase 7; the rest Phase 2. |

## 1. Method

| What | How |
|---|---|
| Source review | Every Python module, Jinja2 template, shell script, packaging file, CI workflow and document was read. |
| Dynamic checks | Behaviours below marked **[verified]** were reproduced locally (generation of bundles from crafted configs, sourcing of generated files in a scratch directory, inspection of generated scripts). Nothing was run against a real server for this audit. |
| Static analysis | `ruff` (default and extended rule sets), `mypy --ignore-missing-imports`, `bandit`, `shellcheck -S warning` on generated and repository scripts, `pip-audit` on declared dependencies. |
| Coverage | `coverage run -m unittest tests.test_gisstack` with Flask and MapProxy installed (all 61 tests execute; 8 are skipped in minimal environments without Flask/MapProxy/Tk). |
| Earlier manual validation | During development the native installer, cleanup, diagnostics and existing-program flows were exercised on throw-away **Ubuntu 24.04** and **Rocky 9.8** VMs, Docker Compose, and a **kind** cluster. That evidence is *manual*, is **not reproduced by CI**, and is therefore listed as a testing gap. |

Priority scale: **P0** fix before any further feature work (data loss, code execution, security boundary) · **P1** high (reliability / significant security hardening) · **P2** medium · **P3** low / hygiene.

## 2. Snapshot

| Metric | Value |
|---|---|
| Python code | ≈ 5,300 lines in `gisstack/` (largest: `desktop.py` 1,110; `ops.py` 627; `i18n.py` 476 (mostly data); `webgui/app.py` 475; `cli.py` 422; `existing.py` 386; `generate.py` 347; `discover.py` 346) |
| Web front-end | one 928-line `index.html` (HTML + CSS + vanilla JS, no build step) |
| Templates | 42 Jinja2 files (native 8, compose 5, k8s 10, common 6, webmap 4, mobile 9) |
| HTTP API | 24 routes in `webgui/app.py`; every route except `/healthz` requires the token; every state-changing route requires the `X-GisStack` header |
| Tests | 61 tests in 13 classes (`unittest`), one file |
| Statement coverage | **59 %** overall — `cli.py` **0 %**, `deploy.py` 39 %, `desktop.py` 43 %, `webgui/app.py` 56 %, `existing.py` 65 %, `updater.py` 75 %, `ops.py` 84 %, `discover.py` 83 %, `generate.py` 93 %, `config.py` 88 % |
| `ruff` (default rules, `gisstack/` + `tests/`) | 51 findings (27 auto-fixable; mostly style in tests) |
| `mypy` | 24 errors in 8 files (`desktop.py` 14, `discover.py` 5, `webgui/app.py` 3, others 1–2) |
| `bandit` | 1 high (B701 Jinja `autoescape=False` — intentional for shell/YAML output, see S-11), 1 medium (B104 `0.0.0.0` string literal, not a bind — false positive), 21 low (subprocess usage, asserts, empty-string "passwords" — expected) |
| `pip-audit` | no known vulnerabilities in `Flask>=3.0`, `Jinja2>=3.1`, `PyYAML>=6.0` |
| `shellcheck -S warning` | generated `install.sh`/`uninstall.sh`: 1 warning each (unused loop variable); `up.sh`, `apply.sh`, repository scripts: clean |
| CI | `tests.yml` (unit tests, Python 3.10 and 3.12) and `release.yml` (tag → sdist, deb, rpm, `SHA256SUMS`, GitHub Release) |

## 3. Existing capabilities (preserve all of these)

- Generate and run a GIS stack (PostgreSQL/PostGIS, Tomcat, GeoServer, QGIS Server, MapProxy, Caddy, bilingual Leaflet web map, React Native and Flutter templates) for **native** (apt/dnf, systemd), **Docker/Podman Compose** and **Kubernetes** (kustomize).
- Interfaces: Flask web GUI, Tkinter desktop GUI, CLI.
- Local or SSH deployment; per-program save/apply; saved projects; generated-secret persistence.
- Cleanup of single programs or the whole stack (with optional data purge).
- Diagnostics: live status with bound IP:port and real health probes; start/stop/restart/enable/disable; logs (follow + cancel).
- Pre-install detection of existing programs; backup, remove, adopt (PostgreSQL) and re-use (databases, GeoServer data, QGIS projects, Caddy site blocks).
- Update checker (GitHub Releases, SHA-256 verified download, in-place upgrade for venv installs).
- Georgian/English UI, light/dark themes, shared settings; Tkinter installed only after consent.
- Packaging: `install.sh`, `.deb`, `.rpm`, release workflow.

## 4. Findings

Evidence references use `file:line` at the audited revision.

### 4.1 Security

| ID | Pri | Finding | Evidence | Recommendation |
|---|---|---|---|---|
| **S-01** | **P0** | **Configuration values reach generated shell scripts, `stack.env`, Caddyfile, pg_hba and YAML without validation or escaping.** `stack.env` is `source`d by `install.sh` as root on the target. A value such as `qgis.projects_dir = "/srv/q$(cmd)"` executes `cmd` when `stack.env` is sourced **[verified]**. `k8s.namespace = "x; cmd #"` produces `NS=x; cmd #` in `apply.sh` **[verified]**. `email` containing a newline injects Caddy directives **[verified]**. `postgres.allowed_cidr = "0.0.0.0/0 trust # "` is written into a `pg_hba.conf` line **[verified]**. Image names, `k8s.storage_class` and titles are rendered unvalidated into Compose/K8s YAML. Threat: shared project files (`-c`, "load project"), copy-pasted configs, compromised templates repositories. Even for a trusted operator it turns typos into root-level breakage. | `config.py` (`_merge`, `resolve`: only a subset of fields validated), `templates/native/stack.env.j2`, `templates/k8s/apply.sh.j2:5`, `templates/common/Caddyfile.j2:4-6`, `install.sh.j2:27` | Strict schema (types, ranges, patterns, `additionalProperties: false`), a single shell-quoting filter applied to **every** value placed in shell contexts, ban of control characters, and a regression test that fuzzes each config field into every generated file. |
| **S-02** | **P0** | **Backup is fail-open before destructive steps.** `backup_script` prints `BACKUP_DIR=` unconditionally; individual `pg_dump`/`tar` failures only print `FAILED`/`(no files)`. `existing.run` continues to removal when the marker is present, so with `reuse`/`remove(+purge)` a failed dump can be followed by deletion of the data it was meant to protect **[verified from script text]**. No integrity check (checksums), only "file size > 0" on the downloaded copy; no restore verification; backup directories are named to the second (`mkdir -p`, no collision check). | `existing.py` `backup_script`, `run` (marker check), `fetch_backup` | Fail closed: abort on any backup error; write and verify a manifest with SHA-256 per artefact; verify the downloaded archive; refuse to reuse an existing backup directory; require an explicit confirmation token for any data-destroying step. |
| S-03 | P1 | **Secrets appear in process arguments on the server** during installation: `psql -v dbpass="$DB_PASSWORD"`, `curl -u admin:<password>`, `curl -d '{"newPassword":…}'` — readable via `/proc/<pid>/cmdline` by other local users for the duration. | `install.sh.j2:168, 267-274` | Pass secrets through stdin / `PGPASSFILE` / `curl --config -` / `-d @file` with mode 600 temporary files. |
| S-04 | P1 | **Unverified downloads executed as root.** GeoServer WAR and extension zips: no checksum. Caddy binary from `caddyserver.com/api/download`: no checksum or signature. `pip install mapproxy gunicorn pyproj shapely`: unpinned, no hashes. Only Tomcat is verified (and only against a checksum from the same host). | `install.sh.j2:209-211, 245-255, 337, 359` | Pin versions and checksums in the catalog (`catalog.py`), verify before use; prefer distro/vendor packages with signatures where possible; hash-pinned requirements for the MapProxy venv. |
| S-05 | P1 | **Secret files are written before their permissions are tightened** (`write_text` then `chmod`), and the web GUI applies `0700` to the bundle directory only *after* generation. Secrets therefore exist briefly with `0644` inside a `0755` directory. `~/.gisstack` itself is created `0755`. Same write-then-chmod pattern for the token, settings, secrets store and project files. | `generate.py:_write`, `webgui/app.py:163`, `deploy.home_dir`, `config.py:232`, `webgui/app.py:40` | `umask(0o077)` around writes or `os.open(..., 0o600)`; create `home_dir` as `0700`; create bundle directories `0700` before writing. |
| S-06 | P1 | **Web GUI authentication/session model is minimal.** One static, never-rotated token persisted in `web.token`; the cookie *is* the token (no session id, expiry, logout or rotation, no `Max-Age`); token is printed in a URL; no rate limiting; no `Host` header allow-list; no `MAX_CONTENT_LENGTH`; **no audit log** of administrative actions; runs on the Werkzeug development server. The packaged service runs as **root** with `NoNewPrivileges=false`. Positives: constant-time comparison (`hmac.compare_digest`), `SameSite=Strict`, `HttpOnly`, CSRF header on all state-changing routes, default bind `127.0.0.1`, query token is stripped by redirect. | `webgui/app.py:59-100, 466-475`, `packaging/gisstack-web.service` | Session tokens derived from the static secret with expiry; rotate-on-demand; `Host` allow-list; size limits; rate limit; audit log; production WSGI server; document and reduce service privileges (separate unprivileged UI from privileged executor). |
| S-07 | P2 | **CSP allows `'unsafe-inline'` scripts** (the whole app is one inline script). The page renders data that originates on remote servers (unit names, scan results) with `innerHTML` + `esc()`; a single missed escape would give script execution in a session that can run root-level actions. | `webgui/app.py` `headers()`, `index.html` | Move JS/CSS to static files and use a nonce/hash CSP; prefer `textContent`/DOM construction over `innerHTML`. |
| S-08 | P2 | **SSH trust and residue.** `StrictHostKeyChecking=accept-new` (trust on first use; writes the user's `known_hosts`); no `ServerAliveInterval`. The uploaded bundle (including `stack.env`/`.env`, mode 600) stays in `~/gisstack-<project>/` on the server after deployment. Positive: SSH private-key *paths* are not persisted in project files. | `deploy.py:69-70, 135-156` | Per-server pinned host keys (Phase 9), keep-alive options, remove the uploaded bundle after success (or place it in a 0700 temp dir). |
| S-09 | P2 | **Update trust.** `SHA256SUMS` comes from the same release as the artefact (integrity, not authenticity). In-place upgrade runs `pip install <sdist>`, which executes the build backend. | `updater.py` | Sign releases (Sigstore/cosign or minisign) and verify before install; keep `--download` as the default for packages. |
| S-10 | P2 | Unbounded in-memory job registry (`JOBS`, `Job.lines`) without TTL. | `webgui/app.py:45-57` | Ring buffer + expiry; persist operation history (also needed for Phase 9). |
| S-11 | P3 | Jinja2 `autoescape=False` is correct for shell/YAML output but relies on every template remembering a context-appropriate filter. A shell-quoting filter (`shq`) exists but is used in only **2** places in the templates. | `generate.py`, `bandit B701` | Covered by S-01 (mandatory quoting filters + fuzz test). |
| S-12 | P3 | `SECURITY.md` has a reporting policy but no threat model or supported-deployment guidance (token handling, tunnelling, root usage). | `SECURITY.md` | Update in Phase 1. |

### 4.2 Reliability and deployment safety

| ID | Pri | Finding | Evidence | Recommendation |
|---|---|---|---|---|
| R-01 | P1 | **Long operations cannot be cancelled or time out.** `deploy._stream` has no overall timeout; SSH has `ConnectTimeout` only. A stalled session keeps the web `DEPLOY_LOCK` forever. Only log-following jobs support cancel. | `deploy.py:_stream`, `webgui/app.py:_plain_job/_start_job` | Central runner with deadlines, `ServerAliveInterval`, cancellation events and process-group termination for all jobs. |
| R-02 | P1 | **Mutual exclusion is per process.** The lock is a `threading.Lock` in one web process; two GUIs, or GUI + CLI, can run conflicting operations on the same server. | `webgui/app.py:63` | Remote lock (`flock` on the server) + local operation registry. |
| R-03 | P1 | **No rollback.** `existing.run`: back up → remove → install → import. If the install fails after removal nothing restores the removed program; partial `install.sh` states were observed during testing (idempotent re-run recovers, but nothing guides the user). | `existing.py:run` | Operation journal with per-step undo/restore hints; automatic restore offer using the verified backup (needs S-02 first). |
| R-04 | P2 | Preflight checks are minimal (`root`, tool present). Missing: free disk, RAM, DNS/port reachability, OS/arch support, time sync (TLS), package-repo reachability, existing firewall state. | `deploy.preflight` | Dedicated preflight module returning structured results (Phase 1). |
| R-05 | P2 | **No deployment history or state.** There is no record of what was deployed, when, by whom, with which config hash. | — | Operation journal (SQLite/JSON-lines) shared by Phases 1, 3, 9. |
| R-06 | P2 | Config files have **no `schema_version`**, so compatible evolution depends on key presence. | `config.default_config` | Add `schema_version` and migrations (backwards compatible: missing = 1). |
| R-07 | P3 | Status refresh opens a new SSH connection each time (≈1 s measured). Fine for one server; poor for fleets. | `ops._run` | Connection reuse (`ControlMaster`) or a small persistent agent for Phase 8/9. |

### 4.3 Input validation gaps (`config.resolve`) — all **[verified]**

Validated well: project name, domain pattern, password alphabet/length, extension IDs, GeoServer channel, PostgreSQL version, grids, cache type/format, layer names, targets.

Not validated: **unknown keys (silently ignored — typos go unnoticed)**; value *types* for numeric fields (`postgres.port = 99999` accepted, also `ram_gb`, `max_connections`, heap sizes, `qgis.processes`, `mapproxy.workers`, seed levels/bbox, web-map centre/zoom); CIDR syntax; e-mail; URLs/image references; Kubernetes names (`namespace`, `storage_class`); filesystem paths (`qgis.projects_dir`); free-text titles. See S-01.

### 4.4 Architecture and technical debt

| ID | Pri | Finding | Evidence | Recommendation |
|---|---|---|---|---|
| A-01 | P1 | **Private transport API is shared across modules.** `discover` and `existing` call `ops._run`/`ops._argv`; `ops` calls `deploy._ssh_base`. Transport is not its own concept, so timeouts/cancel/connection reuse cannot be added in one place. | `existing.py`, `discover.py`, `ops.py` | `transport` module (`Host`, `Runner`: local/SSH, timeouts, cancel, redaction). |
| A-02 | P1 | **Provider logic is spread across `if target == …` branches** in `ops` (status/control/logs), `deploy` (argument builders), `generate._plan`, `existing` (native only). Adding a provider or a feature touches four files. | `ops.py`, `deploy.py`, `generate.py` | `Provider` interface (deploy, status, control, logs, clean, apply, backup hooks) with `native`, `compose`, `kubernetes` implementations; migrate incrementally behind the current function names. |
| A-03 | P1 | **Two UIs implement the same flows** (≈930 lines of JavaScript, ≈1,100 lines of Tk). Business rules (plans, cleanup, diagnostics, project save) are duplicated and drift. | `index.html`, `desktop.py` | Service layer returning plain data; web API is the single contract; Tk calls the same services in-process. |
| A-04 | P2 | **`desktop.py` monkeypatches `ttk`, `tk.Wm` and `messagebox` at import time** (global side effects; not reload-safe — a test needed a workaround). | `desktop.py` top | Explicit translating widget factory / helper functions; no global patching. |
| A-05 | P2 | **`ops.py` mixes concerns** (transport, three parsers, script builders, console formatting): 627 lines. | `ops.py` | Split into `transport`, `status/<provider>`, `control`, `render`. |
| A-06 | P2 | **No logging, events or error taxonomy.** No use of the `logging` module; progress is ad-hoc strings; `DeployError` is used for validation, transport and plan errors alike. | whole package | `core.logging` (structured, redaction), `core.events`, exception hierarchy mapped to API error codes. |
| A-07 | P2 | **Shell logic embedded in Jinja** (`install.sh.j2` ≈ 430 lines) is only testable by `bash -n` and manual VM runs. | `templates/native/install.sh.j2` | Keep templates thin; move logic to a static, shellcheck-ed library with unit-style tests (bats) plus container-based integration tests. |
| A-08 | P2 | Typing is partial (`dict[str, Any]` configuration everywhere; mypy 24 errors). | mypy report | Typed config model (dataclasses/TypedDict) generated from the schema. |
| A-09 | P3 | Small coupling smells: `settings` imports `deploy` only for `home_dir`; global compiled regex in `i18n`; `i18n.py` holds 380 translations as code. | `settings.py`, `i18n.py` | `core.paths`; optional JSON catalogs (keep the coverage test). |
| A-10 | P3 | `pyproject` uses a deprecated `license = { text = … }` table (setuptools warns). | `pyproject.toml` | SPDX expression `license = "MIT"`. |

### 4.5 Performance

No algorithmic bottlenecks were found at current scale. Observed costs: one SSH round trip per status refresh (~1 s); full bundle regeneration for every preview; whole `stdout` buffering in `ops._run`; MutationObserver translation uses one large alternation regex per text node (acceptable). The real scaling limits appear with *many servers* and *metric history* (Phases 8–9), not today.

### 4.6 Testing and CI

| ID | Pri | Gap |
|---|---|---|
| T-01 | P1 | `cli.py` has **0 %** coverage; `deploy.py` 39 % (SSH/tar/`_run_entry` paths untested); `webgui/app.py` 56 % (job routes, update/apply, deploy path); `desktop.py` 43 %. |
| T-02 | P1 | **No integration tests in CI.** Native installer, Compose, Kubernetes, cleanup, existing-program flows were validated manually; nothing re-runs them. |
| T-03 | P1 | No security regression tests for S-01…S-06 (config fuzzing, permissions, auth/session, size limits). |
| T-04 | P2 | CI matrix is Python 3.10/3.12 only although `requires-python >= 3.9` and Rocky/RHEL 9 ships 3.9 (the installer was manually verified there; the test-suite was not run on 3.9). |
| T-05 | P2 | CI has **no lint, type check, shell lint, YAML/K8s validation, dependency or code scanning**; release workflow builds deb/rpm but does not install-test them. |
| T-06 | P2 | No end-to-end tests of web workflows (only Flask test-client checks); no browser tests. |
| T-07 | P3 | `ruff` 51 findings and `mypy` 24 errors are unmanaged (no baseline, no gate). |

### 4.7 Documentation and release quality

User documentation (`README.md`, `docs/USAGE.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, `RELEASING.md`) is current and bilingual in the places that matter. Missing: architecture and API documentation (this audit adds the first), upgrade guide, backup/recovery guide, troubleshooting guide as a standalone document, threat model. Unreleased work (existing-program management, bilingual UI, themes, Tkinter prompt) is on `main` but not in the latest release.

## 5. What is already good (keep)

Constant-time token check and CSRF header; localhost-only default; no `shell=True`/`eval`/`pickle`; remote script inputs are regex-validated and `shlex`-quoted in `ops`/`existing`; update downloads are restricted to this repository's release URLs and verified; no known vulnerable dependencies; secret files are `0600` after writing; project files never contain SSH key paths; backups precede removals; installer idempotency verified on two distributions; translation-coverage test; deterministic template generation covered by tests.

## 6. Prioritised summaries

### 6.1 Top 20 improvement opportunities

1. **Strict config schema + mandatory shell/YAML/Caddy quoting (S-01).**
2. **Fail-closed, verified backups; never delete after a failed backup (S-02).**
3. Remove secrets from process arguments on the server (S-03).
4. Pin and verify every download executed as root; hash-pinned Python requirements (S-04).
5. Create secret files and directories with restrictive permissions atomically (S-05).
6. Web session model: expiring sessions, rate limiting, `Host` allow-list, size limits, audit log (S-06).
7. Central command runner: timeouts, cancellation, keep-alive, redaction (R-01, A-01).
8. Remote operation lock and operation journal / deployment history (R-02, R-05).
9. Rollback/restore path using verified backups (R-03) and a real **restore** command.
10. CI quality gates: ruff, mypy (baseline → strict), shellcheck, YAML/kubeconform, pip-audit, CodeQL, Python 3.9–3.13 (T-04, T-05).
11. Characterisation tests for `cli.py` and `deploy.py` before refactoring (T-01).
12. Integration tests in CI: containerised PostgreSQL/PostGIS, GeoServer, MapProxy; Compose `config`; kind/kubeconform; installer in distro containers (T-02).
13. Provider interface (native/compose/kubernetes) (A-02).
14. Service layer shared by web, Tk and CLI; single API contract (A-03).
15. Production WSGI server and privilege separation for the packaged service (S-06).
16. Static JS/CSS and nonce-based CSP (S-07).
17. Structured logging/events and an error taxonomy (A-06).
18. Release signing and verified upgrade path (S-09).
19. Configuration `schema_version` with migrations (R-06).
20. Documentation set: threat model, upgrade, backup/recovery, troubleshooting, API reference (Phase 12).

### 6.2 Top 10 security concerns

| # | Concern | Pri |
|---|---|---|
| 1 | Config → root shell/Caddy/pg_hba injection (S-01) | **P0** |
| 2 | Destructive steps proceed after failed backup (S-02) | **P0** |
| 3 | Secrets in server process arguments (S-03) | P1 |
| 4 | Unverified binaries/packages installed as root (S-04) | P1 |
| 5 | Secret files briefly world-readable; `~/.gisstack` 0755 (S-05) | P1 |
| 6 | Static long-lived token as session; no rate limit/audit/Host check; root service on dev server (S-06) | P1 |
| 7 | CSP permits inline script; `innerHTML` with remote-derived data (S-07) | P2 |
| 8 | SSH TOFU, no keep-alive, secrets left in remote home (S-08) | P2 |
| 9 | Update trust limited to same-origin checksum (S-09) | P2 |
| 10 | Unbounded job memory, no operation history (S-10) | P2 |

### 6.3 Top 10 architectural improvements

1. Extract a **transport/runner** layer (A-01).
2. Introduce a **Provider** interface (A-02).
3. Build a **service layer** + typed result objects; UIs become thin (A-03).
4. **Typed configuration model** generated from one schema; validation in one place (S-01, A-08).
5. **Operation journal and event bus** (R-05, A-06).
6. Split `ops.py` and isolate per-provider parsers (A-05).
7. Remove import-time monkeypatching from `desktop.py` (A-04).
8. Move shell logic out of Jinja into tested libraries (A-07).
9. Static front-end assets + versioned JSON API (A-03, S-07).
10. `core.paths` and settings/state locations in one module (A-09).

### 6.4 Testing gaps (summary)

CLI untested; deploy/SSH untested; no integration or end-to-end tests in CI; no security regression tests; no Python 3.9 run; no lint/type/security gates; no restore tests (restore does not exist yet); manual VM evidence not reproducible.

### 6.5 Recommended implementation order and complexity

Complexity uses *person-days of focused work* with wide uncertainty (±50 %), assuming the existing test-suite stays green.

| Order | Phase | Complexity | Notes |
|---|---|---|---|
| 1 | **1a — P0 fixes** (S-01, S-02, S-05 quick wins) | M (3–5 d) | Tiny, independent PRs; unblocks everything else. |
| 2 | **11a — Quality gates and characterisation tests** (CI lint/type/shell/YAML/audit, Python 3.9–3.13, CLI/deploy tests) | M (4–6 d) | Protects the refactors that follow. |
| 3 | **1b — Remaining security/reliability** (S-03, S-04, S-06, R-01…R-04) | L (10–15 d) | Includes runner with timeouts/cancel, preflight, audit log. |
| 4 | **2 — Architecture (incremental)** | L (10–15 d) | Transport → providers → service layer, behind existing function names. |
| 5 | **7 — Backup & restore** | XL (15–25 d) | Builds on S-02 and the runner; restore wizard is the largest piece. |
| 6 | **8 — Monitoring core** (collectors, history, alerts, JSON endpoint) | L (10–15 d) | Real metrics only; SQLite ring storage. |
| 7 | **10 — Versioned API + auth** | L (8–12 d) | Needed before the dashboard so UI and API share one contract. |
| 8 | **3 — Dashboard** | XL (20–30 d) | Largest UI effort; feature-flagged. |
| 9 | **4 / 5 / 6 — GeoServer, PostGIS, MapProxy managers** | L / L / M (10–15 / 10–15 / 6–10 d) | PostGIS SQL editor needs the strictest permission model. |
| 10 | **9 — Multi-server** | XL (15–25 d) | Reuses runner, journal and API. |
| 11 | **11b / 12 — Integration/E2E tests and documentation** | L / M (8–12 / 5–8 d) | Integration tests are added continuously from step 2; this is the consolidation. |

Total order-of-magnitude estimate: **≈ 12–19 person-months** for all phases.

## 7. Open questions for the owner

1. Minimum supported Python (keep 3.9 for RHEL 9, or raise to 3.10+ and ship a bundled interpreter)?
2. Front-end direction for the dashboard: keep vanilla JS + static assets (recommended), or adopt a framework/build step?
3. Is a release-signing tool (Sigstore/cosign or minisign) acceptable for the project?
4. Should the web GUI keep running as root, or should privileged actions move to a separate local helper?
5. Monitoring storage: bundled SQLite (recommended) vs optional external time-series back-ends?
