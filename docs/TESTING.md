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
| `tests/integration/test_installer.py` | `GISSTACK_TEST_INSTALLER=1`, `GISSTACK_TEST_DISTROS=ubuntu-22.04,ubuntu-24.04,debian-12,rocky-9,alma-9` (or `all`; default `ubuntu-24.04`) | generated `install.sh --only postgis` in a systemd container; gis-stack's own `ops.status` and PostGIS manager against that installation; re-run is harmless; uninstall keeps data, `--purge` removes it |
| `tests/integration/test_packages.py` | same | the `.deb` / `.rpm` install with apt / dnf, `gisstack --version`, `generate`, the web unit, removal (a format whose builder `dpkg-deb`/`rpmbuild` is missing is skipped) |
| `tests/integration/test_compose.py` | `GISSTACK_TEST_DOCKER=1` | a real Compose PostGIS stack: up, healthy, status, restart through gis-stack's control script, down |
| `tests/e2e/test_browser.py` | `GISSTACK_TEST_E2E=1`, `pip install playwright && playwright install chromium` | real `gisstack web` + Chromium: token authentication, configure, preview, bundle download, dashboard pages, language switch |
| `tests/integration/test_compose_managers.py` | `GISSTACK_TEST_INTEGRATION=1` | PostGIS, GeoServer and MapProxy managers against real Compose stacks (MapProxy image is built from the generated Dockerfile: needs network once); the same managers through a **`kubectl` shim** that forwards to those containers (fast check of the `kubectl exec` commands; the real cluster test is below) |
| `tests/integration/test_k8s_managers.py` | `GISSTACK_TEST_K8S=1` + `kubectl` with a working context | the generated Kubernetes bundle (PostGIS, GeoServer, MapProxy) applied into a throw-away namespace, then the three managers through real `kubectl exec` (run locally on kind v0.33 / Kubernetes v1.37 and in the `kubernetes` CI job). **Never point it at a cluster that matters.** |
| CI job `kubernetes` | CI only | PostGIS manifests applied to a kind cluster, then the manager test above |

Containers are privileged and use the host cgroup namespace (needed by systemd); images are cached as `gisstack-it-<distro>:1`.

## What is not covered

The `kind` job and the rpm package test have only run in CI, not on the author's machine. The browser flow stops at the bundle download: a real deployment is the installer suite's job. Only PostGIS is installed by the installer suite; GeoServer, MapProxy, Caddy and QGIS Server steps are covered by their own manager tests against real software but not by a full `install.sh` run.
