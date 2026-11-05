# GeoServer manager (Phase 4, feature flag `geoserver_manager`)

`GISSTACK_FEATURES=geoserver_manager gisstack web` -> dashboard page **GeoServer manager** (`/dashboard#/geoserver`) and the API below.

**How it talks to GeoServer.** Official REST API (`/geoserver/rest`) and OGC endpoints only. Requests are made *from the server GeoServer runs on*
(`http://127.0.0.1:<tomcat port>/geoserver`, locally or over SSH) with `curl`; the admin user/password come from the stack's configuration and travel on
curl's stdin, so they are not in a process list, a log, an API request or an answer, and never reach the browser. GeoServer's own security decides what is
allowed: its 401/403 answers become `403 forbidden` ("check geoserver.admin_user / admin_password").

**Scope of this release:** native target only (Compose/Kubernetes: a clear error). Read: health, inventory (workspaces, datastores, layers, layer groups,
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
(`docker.osgeo.org/geoserver:2.28.6`; runs when the image is present, `GISSTACK_TEST_DOCKER=1` pulls it - CI does on one matrix entry).
GeoServer 3.0 is not covered by an automated test yet.
