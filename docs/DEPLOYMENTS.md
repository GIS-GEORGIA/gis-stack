# Deployment records, health verification and rollback

## What is recorded
Every deployment (`gisstack deploy`, the dashboard, the Tk GUI and the API) writes one record to `~/.gisstack/deployments/<id>.json` (0600, directory 0700):

| Field | Meaning |
|---|---|
| `outcome` | `ok` (ran and every program healthy afterwards), `unhealthy` (ran, but a program or check is failing), `unverified` (ran; the machine could not be asked, or `GISSTACK_VERIFY=0`), `failed` (the script failed), `aborted` (cancelled), `running`, `incomplete` (started, never finished, older than 6 h) |
| `before` / `after` | per program: state, ok, failing checks - read from the real machine, never invented |
| `digest` | fingerprint of the configuration with secret values masked out |
| `kind`, `rollback_of` | `deploy` or `rollback` and the record it went back to |

The record holds **no secrets**. The configuration that was applied is kept in a separate 0600 file (`<id>.config`) for the last 20 deployments; it is used only by a rollback and is never returned by the API. At most 200 records are kept; only records and snapshots are ever pruned - never data or backups.

After the installer finishes, gis-stack waits up to `GISSTACK_VERIFY_TIMEOUT` seconds (default 60) for every program to be `running` with all its checks passing, and to **stay** that way for `GISSTACK_VERIFY_SETTLE` seconds (default 10) - a failed upgrade that is up for a second and then crash-loops is not reported healthy. A deployment that ran but is not healthy is **reported** (`!!` lines, outcome `unhealthy`), not turned into an error: the script itself succeeded.

## What "rollback" is - and is not
gis-stack cannot un-install packages or undo a database migration. The recovery path it can verify is: **re-apply the configuration of an earlier deployment that was healthy afterwards**, using the same idempotent deploy as always.

* It never purges, never deletes or restores data, volumes or backups.
* It does not remove programs, containers or objects that only the newer configuration added (native: packages/files; Compose: containers/volumes; Kubernetes: objects are not pruned). The plan lists these limitations.
* Passwords, tokens and secrets keep their **current** values - a password rotated since the earlier deployment is not reverted.
* It is refused (HTTP 409 `not_ready`, with the reason) when there is no earlier deployment with outcome `ok` and a stored configuration, when the chosen record was not healthy, when a deployment is still running, or when the machine already runs that configuration healthily (so repeating a rollback is a harmless no-op).
* It ends with the same health verification. If the machine is not healthy afterwards the job **fails** and nothing further is attempted.

| Target | Re-applied with | Verified how |
|---|---|---|
| native | `install.sh` | systemd state + HTTP/TCP checks |
| compose | `up.sh up` | container state/health + checks |
| k8s | `kubectl apply` | pod/deployment readiness + checks |

## Use
Dashboard: *Operations -> Deployments and rollback* (plan, typed confirmation `ROLLBACK`, live log). API: `GET /api/v1/deployments/history` (read), `POST /api/v1/deployments/rollback/plan` (operate), `POST /api/v1/deployments/rollback` (admin, `confirm: true`, a job). Audit events: `deploy.start`, `deploy.rollback` (refusals and starts).

## Verification status
Unit tested with fake installers/status (all three targets, failures, refusals, idempotency, secrets, permissions). **Real-environment tests** (`tests/integration/test_deploy_rollback.py` on Docker Compose, `test_deploy_rollback_k8s.py` on kind): fresh deploy verified healthy, a failed PostgreSQL upgrade detected, rollback restores the earlier version with data and password intact, repeating it is a no-op. **Not verified**: a real native host and Podman. On Kubernetes, `apply.sh` deletes a StatefulSet pod that never became ready on an outdated revision so that a corrected template can roll out (the volume is kept).
