# GeoServer manager (Phase 4, feature flag `geoserver_manager`)

`GISSTACK_FEATURES=geoserver_manager gisstack web` -> dashboard page **GeoServer manager** (`/dashboard#/geoserver`) and the API below.

**How it talks to GeoServer.** Official REST API (`/geoserver/rest`) and OGC endpoints only. Requests are made *from the server GeoServer runs on*
(`http://127.0.0.1:<tomcat port>/geoserver`, locally or over SSH) with `curl`; the admin user/password come from the stack's configuration and travel on
curl's stdin, so they are not in a process list, a log, an API request or an answer, and never reach the browser. GeoServer's own security decides what is
allowed: its 401/403 answers become `403 forbidden` ("check geoserver.admin_user / admin_password").

**Scope of this release:** all targets (see below). Read: health, inventory (workspaces, datastores, layers, layer groups,
styles), layer detail, WMS/WFS/WMTS capabilities, validation findings, layer preview (WMS GetMap proxied; PNG/JPEG, 16-2048 px, the answer must really
be an image). Change: create workspace (`admin`), delete workspace (`admin`; recursive delete needs `confirm=true`, the dashboard asks you to type the
name), reload catalog (`operate`). Not yet: creating datastores/layers/styles/layer groups, security-rule editing, a visual layer browser with endpoint
links - follow-ups.

| Endpoint (`POST /api/v1/geoserver/...`) | Scope |
|---|---|
| `inventory`, `health`, `validate`, `capabilities`, `layers/detail`, `preview` | read |
| `reload` | operate |
| `workspaces`, `workspaces/delete` | admin |

All take the usual `{mode, config?, ssh?}` machine fields; see `docs/API.md`.

**Safety.** Names are validated before any request (`[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}`, optional `workspace:layer`); capabilities XML containing a DTD or
entities is refused; one failing listing never hides the others; the preview never passes an error document or HTML off as an image.

**Tests.** `tests/test_geoserver.py`: fake gateway (JSON quirks, error mapping, hostile XML, parameter validation), the real curl script against a loopback
HTTP server (credentials, binary bodies, special characters, failures), API scopes/contracts, and **a real GeoServer container**
(the default-channel image, `docker.osgeo.org/geoserver:3.0.2`; the tests use the cached image or pull it with bounded retries; with `GISSTACK_REQUIRE_IMAGES=1`, as in CI, an unobtainable image FAILS the test instead of skipping it - see `docs/TESTING.md`).
GeoServer 3.0 is not covered by an automated test yet.

## Targets (native, Compose, Kubernetes)

The manager works on all three targets; the target comes from the configuration (`deploy_target`) like everywhere else. Native: scripts run on the
server as before. Compose and Kubernetes: the same scripts reach the program *inside* its container/pod (`docker exec -i` / `kubectl exec -i`, see
`gisstack/targets.py`) with the operator's own docker / kubectl rights (no `sudo`); secrets and payloads still travel on stdin. Compose containers are found
through Compose's own labels (project + service), pods as `TYPE/NAME` in the namespace from `k8s.namespace`; a missing container or workload is a clear error
before anything runs. `kubectl exec` cannot choose the OS user or pass environment variables, so on Kubernetes the pod's own user is used.

GeoServer is called from inside its own container on `127.0.0.1:8080`; the curl configuration with the admin password goes to `exec -i` on stdin.
