# Testing

## Everyday suite

```bash
python -m unittest discover -s tests
```

Unit, contract, golden-file, architecture and node tests. Real PostGIS/GeoServer/MapProxy tests are opt-in with `GISSTACK_TEST_DOCKER=1`. Runs in CI on Python 3.9-3.13.

## Integration and end-to-end suites (Phase 11b)

These install real software in throw-away containers, so they are opt-in and run in CI nightly, on demand and on pull requests labelled `integration` (`.github/workflows/integration.yml`). They never touch a real server.

| Suite | Switch | What it proves |
| --- | --- | --- |
| `tests/integration/test_pg_ready.py` | `GISSTACK_TEST_INTEGRATION=1` (Docker) | the generated native PostgreSQL readiness function against a real postgis container: running = open, closed port = closed, a listener that accepts TCP but never answers = closed (a port check calls it healthy) |
| `tests/integration/test_installer.py` | `GISSTACK_TEST_INSTALLER=1`, `GISSTACK_TEST_DISTROS=ubuntu-22.04,ubuntu-24.04,debian-12,rocky-9,alma-9` (or `all`; default `ubuntu-24.04`) | generated `install.sh --only postgis` in a systemd container; gis-stack's own `ops.status` and PostGIS manager against that installation; re-run is harmless; uninstall keeps data, `--purge` removes it |
| `tests/integration/test_packages.py` | same | the `.deb` / `.rpm` install with apt / dnf, `gisstack --version`, `generate`, the web unit, removal (a format whose builder `dpkg-deb`/`rpmbuild` is missing is skipped) |
| `tests/integration/test_compose.py` | `GISSTACK_TEST_DOCKER=1` | a real Compose PostGIS stack: up, healthy, status, restart through gis-stack's control script, down |
| `tests/e2e/test_browser.py` | `GISSTACK_TEST_E2E=1`, `pip install playwright && playwright install chromium` | real `gisstack web` + Chromium: token authentication, configure, preview, bundle download, dashboard pages, language switch |
| `tests/integration/test_compose_managers.py` | `GISSTACK_TEST_INTEGRATION=1` | PostGIS, GeoServer and MapProxy managers against real Compose stacks (MapProxy image is built from the generated Dockerfile: needs network once); the same managers through a **`kubectl` shim** that forwards to those containers (fast check of the `kubectl exec` commands; the real cluster test is below) |
| `tests/integration/test_k8s_managers.py` | `GISSTACK_TEST_K8S=1` + `kubectl` with a working context | the generated Kubernetes bundle (PostGIS, GeoServer, MapProxy) applied into a throw-away namespace, then the three managers through real `kubectl exec` (run locally on kind v0.33 / Kubernetes v1.37 and in the `kubernetes` CI job). **Never point it at a cluster that matters.** |
| `tests/integration/test_stack_backup_compose.py` | `GISSTACK_TEST_INTEGRATION=1` | backup / restore of a real Compose stack (PostgreSQL + GeoServer data): create, verify, restore with staging and swap, corrupted and incomplete backups, failed health check, interrupted restore, encryption, stopped workload |
| `tests/integration/test_stack_backup_k8s.py`, `test_deploy_rollback_k8s.py` | `GISSTACK_TEST_K8S=1` | the same backup cases and deploy / failed upgrade / rollback on a real kind cluster |
| `tests/integration/test_stack_backup_podman.py` | `GISSTACK_TEST_PODMAN=1`, `podman` + a compose provider (`podman-compose`) | gis-stack's own deployment with `runtime: podman`, status, and the same backup cases on a **real Podman engine**. Healthchecks need systemd. No Podman? `tests/integration/run_podman_tests.sh` runs them against a rootful Podman 5 in a privileged systemd container (needs Docker). CI: `.github/workflows/podman.yml` (rootful and rootless jobs) |
| `tests/e2e/test_browser_backup.py` | `GISSTACK_TEST_E2E=1` | the Backups page in Chromium: create, details with operation log, verify, export, restore with typed confirmation and live log, delete, KA/EN, phone width, console clean |
| `tests/integration/test_packages.py` (service part) | same as above | the packaged `gisstack-web` service starts, is **not root**, is served by waitress, keeps its token out of the journal and restarts after `kill -9` |
| `tests/integration/test_stack_backup_gis.py` | `GISSTACK_TEST_INTEGRATION=1` (Caddy test needs free ports 80/443) | Caddy local-CA backup/restore (byte-identical `root.crt`, HTTPS verifies), MapProxy cache restore + config refusal, QGIS project backup + refused restore, on real Compose |
| `tests/integration/test_podman_reliability.py` | `GISSTACK_TEST_PODMAN=1` (+ `PODMAN_COMPOSE_PROVIDER=podman-compose` where the Docker Compose plugin is also installed) | real Podman: failed upgrade -> detection -> rollback with data and secrets intact; monitoring collection incl. PostgreSQL health and the backup-failure alert lifecycle; compose-provider diagnostic (runs only where `podman compose` resolves to Docker Compose without the Podman socket) |
| `tests/e2e/test_browser_csp.py` | `GISSTACK_TEST_E2E=1` | the classic page under the strict CSP in Chromium: zero violations/console errors/failed requests over its flows, computed styles of the former inline styles, enforcement proof |
| `tests/integration/test_stack_backup_ssh.py` | `GISSTACK_TEST_INTEGRATION=1` + `sshd` | `--mode ssh` against a real sshd: unknown/wrong-fingerprint/changed host keys, backup + restore, killed transfer, tampered backup |
| `tests/integration/test_k8s_clean_keeps_data.py` | `GISSTACK_TEST_K8S=1` | `deploy.clean` without `--purge` on a real kind cluster keeps the namespace, the PostGIS volume and its data (the stack comes back with the row); `--purge` removes them |
| `tests/integration/test_tomcat_manager_block.py` | `GISSTACK_TEST_INTEGRATION=1` (Docker; `caddy:2`, `tomcat:11.0-jre17`) | the generated Caddyfile in front of a real Tomcat with the manager deployed: every spelling of `/apps/.../manager` (incl. `..;`) is 403; a control Caddyfile without the rule reaches the manager (401) |
| `tests/integration/test_caddy_acme.py` | `GISSTACK_TEST_INTEGRATION=1`, free ports 80/443 | Caddy ACME certificate + account backup/restore against Pebble: no key text in logs, same certificate and no re-issuance after restore, owners/modes preserved |
| `tests/integration/test_package_upgrade.py` | `GISSTACK_TEST_INSTALLER=1`, `dpkg-deb`, the git ref of the previous release (`GISSTACK_UPGRADE_FROM`, default `v0.1.7`) | packaged-service upgrade (deb): state, token, ownership, failed upgrade, rollback, token rotation. rpm: not available here |
| `tests/integration/test_monitoring_disk.py` | `GISSTACK_TEST_INSTALLER=1` | a real disk-capacity alert on a container tmpfs, delivered to a real HTTP receiver, resolved, not duplicated |
| `tests/test_monitoring_delivery.py` | none (loopback sockets) | alert delivery with a real HTTP receiver that is down / slow / returning 500: outbox, order, back-off, restart, drop |
| `tests/test_package_scripts.py` | none | the one-time token rotation script (temp dirs, fake systemctl) |
| `tests/test_network_exposure.py`, `tests/test_hardening.py`, `tests/test_public_port_probe.py`, `tests/test_session_cookie.py`, `tests/test_deployment_secrets.py` | none | S-2 / S-4 / S-3 / S-7 / S-5 generated output and logic |
| `tests/integration/test_compose_exposure.py` | `GISSTACK_TEST_INTEGRATION=1` | what Docker really publishes and who can reach it (loopback, explicit 0.0.0.0, Caddy only) |
| `tests/integration/test_hardening.py` | `GISSTACK_TEST_INTEGRATION=1` (+ the GeoServer, QGIS, PostGIS, Caddy images) | a fully hardened Compose stack: gis-stack's own health check, routes through Caddy, kernel CapEff/NoNewPrivs, data across hardening off/on, backup/restore |
| `tests/integration/test_web_behind_proxy.py` | `GISSTACK_TEST_INTEGRATION=1` | session cookie `Secure` behind a real Caddy + waitress, trusted vs untrusted proxy |
| `tests/integration/test_podman_reliability.py` (`PodmanExposure`, `PodmanHardening`, `PodmanCaddyCustomPorts`) | `GISSTACK_TEST_PODMAN=1` | the same on real Podman, rootful and rootless |
| CI job `kubernetes` | CI only | PostGIS manifests applied to a kind cluster, then the manager test above |

Containers are privileged and use the host cgroup namespace (needed by systemd); images are cached as `gisstack-it-<distro>:1`.

## Container images and registry outages

`tests/integration/images.py` decides whether a test that needs a registry image can run, and says why not:

```bash
python -m tests.integration.images [--engine docker|podman] [--expect-digest sha256:...] IMAGE...
# exit 0 ok | 10 REGISTRY UNAVAILABLE | 11 IMAGE PULL FAILED | 12 IMAGE DIGEST MISMATCH
```

An image already present locally is used without touching the network; otherwise it is pulled with bounded retries (`GISSTACK_IMAGE_PULL_ATTEMPTS`, default 3, back-off `GISSTACK_IMAGE_PULL_BACKOFF`, default 5 s). On a developer machine an unavailable image **skips** the dependent tests (the reason is printed); with `GISSTACK_REQUIRE_IMAGES=1` (CI) it **fails** them with the classification, so an outage is never a green run. The GeoServer image guarded by the tests is the one a default stack deploys (`images.geoserver_image()`), not a hand-written tag. Digests of the image used for a verification run are recorded in `docs/IMPLEMENTATION-STATUS.md`.

## What is not covered

Podman: on GitHub the rootful job of `podman.yml` passed twice (Podman 4.9.3, 11 tests). The rootless job failed twice: first on rootless Podman 4.9's pod-cgroup limitation (fixed by `x-podman: {in_pod: false}` in the generated Compose file), then because `podman compose` preferred the Docker Compose plugin, which needs the Podman API socket a rootless session does not run. The workflow now pins `PODMAN_COMPOSE_PROVIDER=podman-compose` for that job (verified locally, not yet on GitHub). On a machine with both Docker Compose and podman-compose installed, set `PODMAN_COMPOSE_PROVIDER=podman-compose` (or run `systemctl --user enable --now podman.socket`) before using `runtime: podman`.


The `kind` job and the rpm package test have only run in CI, not on the author's machine. The browser flow stops at the bundle download: a real deployment is the installer suite's job. Only PostGIS is installed by the installer suite; GeoServer, MapProxy, Caddy and QGIS Server steps are covered by their own manager tests against real software but not by a full `install.sh` run.
