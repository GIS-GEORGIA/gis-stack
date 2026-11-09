# Backup and restore - architecture

Status: design + implementation notes for the container targets (Docker Compose, Podman, Kubernetes). The **native** target keeps its own, separate, already-verified tool (`gisstack-backup`, see `gisstack/backup.py`); nothing here changes it.

## 1. What exists today (inventory)

| Piece | Where | Covers |
|---|---|---|
| `gisstack-backup` (bash, runs on the server) | `gisstack/templates/native/gisstack-backup.sh`, driver `gisstack/backup.py` | native only: config, PostgreSQL, GeoServer, QGIS, cache; checksums, retention, schedule, restore with pre-restore backup and rollback |
| `backup_crypto` | `gisstack/backup_crypto.py` | gpg encryption of *local copies* (passphrase file / env / recipient); keys never stored |
| `targets.Place` | `gisstack/targets.py` | runs a command inside the right container (`docker|podman exec`) or pod (`kubectl exec`), stdin passed through |
| `deployments.verify` | `gisstack/deployments.py` | health verification with a settle period (used after every restore) |
| journal / audit log | `gisstack/journal.py`, `webgui/security.AuditLog` | operation history; administrative audit trail |

`backup.py` explicitly refuses non-native targets, so Compose, Podman and Kubernetes had **no backup at all**.

## 2. What has to be protected, per target

| Data | Compose / Podman | Kubernetes | Backed up as |
|---|---|---|---|
| PostgreSQL/PostGIS databases | named volume `pgdata` | PVC `data-postgis-0` (StatefulSet) | `pg_dump -Fc` of the stack database, taken from *inside* the running database (consistent snapshot) |
| GeoServer data dir (workspaces, layers, styles, config) | volume `geoserver_data` | PVC `geoserver-data` | tar.gz of `/opt/geoserver_data` |
| MapProxy configuration | bind mount `./mapproxy` (read-only in the container) | ConfigMap | tar.gz of `/etc/mapproxy` (backup only; restoring it is a redeploy) |
| MapProxy cache | volume `mapproxy_cache` | PVC `mapproxy-cache` | tar.gz of `/var/lib/mapproxy`, **opt-in** (large, rebuildable) |
| Caddy certificates and state | volumes `caddy_data`, `caddy_config` | PVC `caddy-data` | tar.gz of `/data` |
| QGIS projects | bind mount `./qgis/projects` (read-only) | PVC `qgis-projects` | tar.gz of `/etc/qgisserver` |
| Stack configuration and secrets | generated files + `.env` / Secret | Secret + ConfigMaps | **not in the backup**: secrets are never archived. The configuration is re-created by `deploy` / `rollback` from the project config and `~/.gisstack/secrets` |
| Uploaded GIS files | inside the GeoServer data dir / QGIS projects | same PVCs | covered by the two rows above |

PostgreSQL roles and password hashes are not part of `pg_dump` of one database, so no credential material is in any archive. A restore therefore targets a stack whose roles already exist (same stack, or a stack freshly deployed from the same project).

## 3. Layers

```
api/routes_stackbackup.py   /api/v1/stack-backups/*        (scopes: read / operate / admin, audit)
        |
backups/service.py          provider_for(cfg), policy, orchestration of jobs
        |
backups/provider.py         BackupProvider  <-  ComposeBackupProvider
                                            <-  PodmanBackupProvider   (Compose with runtime=podman)
                                            <-  KubernetesBackupProvider
        |                       provider-specific: how to run in a workload, restart it, check it is running
backups/store.py            local store: layout, atomic sealing, manifest, checksums, verification, retention
backups/model.py           manifest schema, ids, component table, errors, tar safety
        |
targets.Place + transport   containers / pods, here or over SSH (stdin free for payloads)
backup_crypto               optional encryption of each payload file
```

Provider-specific code is limited to three tiny methods (`place`, `restart`, `running`); everything else - format, verification, restore swap, retention - is shared, so the three providers cannot drift apart.

## 4. Backup format (version 1)

A backup is one directory below `~/.gisstack/stack-backups/<project>-<target>/<id>/` (owner-only, 0700; files 0600):

```
manifest.json     schema, id, created, kind, project, target, gisstack version, component list with file name/bytes/sha256,
                  server versions, encryption method (never the key), note
SHA256SUMS        sha256 of every file incl. manifest.json (usable with `sha256sum -c`)
postgres.dump     pg_dump custom format (compressed)            [.gpg when encrypted]
geoserver.tar.gz  ...                                            [.gpg when encrypted]
operation.log     redacted log of the operation
```

* **Atomic:** written to `<id>.partial`, every file re-read and checksummed, the archive structure checked, then renamed to `<id>`. A failure leaves a `.FAILED`/`.partial` directory that is never listed as a backup.
* **Compression:** custom-format dump (zlib level 6) and gzip tar.
* **Encryption (optional):** each payload is encrypted with gpg (passphrase file/env or recipients) *after* it is written; checksums cover the stored (encrypted) bytes, so integrity can be verified without the key. The key or passphrase is never stored in the backup or manifest.
* **Versioning:** `schema` in the manifest; an unknown newer schema is refused.
* **No secrets:** the manifest contains names, sizes, hashes and versions only; log lines pass through the redactor.

## 5. Create

For each selected component, run the dump/tar inside the workload and stream its stdout into the `.partial` file while hashing. Then: re-hash from disk (must equal the streamed hash), `pg_restore --list` through the container for the dump, a streaming structure check of every tar (safe member names, no absolute paths or `..`, link targets inside, no device files), seal. Failure of any component fails the whole backup (nothing is sealed).

## 6. Restore (designed around failure)

Nothing is replaced until the replacement has been proven.

1. **Validate:** full verification (checksums, structure), compatibility (same target family, restorable components present and running, restoring PostgreSQL requires a server of the same or newer major version than the dump), explicit `confirm`.
2. **Protect:** automatic verified *pre-restore backup* of everything about to be replaced (kind `pre-restore`).
3. **Stage:**
   * PostgreSQL: restore into a **scratch database** (`pg_restore --single-transaction --exit-on-error`), then validate it (queries answer, PostGIS functions work when the dump uses PostGIS).
   * Volume components: extract into a hidden staging directory inside the same volume (same filesystem), validate (non-empty, expected marker file where one exists).
4. **Swap (reversible):** PostgreSQL: terminate sessions, rename live -> `<db>_pre_<id>`, scratch -> live. Volumes: move current entries into `.gs-pre-<id>`, move staged entries up. The previous data stays next to the new one.
5. **Verify:** restart the workload where needed, then `deployments.verify` health check. On any failure at any step the swap is reversed automatically and the failure is reported; the previous data is never deleted by gis-stack (the `_pre_` database / `.gs-pre-` directory are left for the operator, listed by `inspect`, removed only by an explicit action).
6. **Interruption:** every phase is written to a state file before it happens; the next restore refuses to start until `recover` has finished or rolled back the interrupted one.

## 7. Security

Owner-only storage; ids and component names validated by fixed patterns (no user-supplied paths); archives are checked before they are read by anything and extraction happens inside the target container only; encryption keys are never stored; manifests and logs hold no secrets; `admin` scope for restore/delete, `operate` for create/verify, `read` for list/inspect; every operation is journalled and audited.

## 8. Limits (stated honestly)

* GeoServer data is copied from the live directory (not quiesced); PostgreSQL is consistent, volume components are file-level consistent only.
* Restoring configuration-mounted components (MapProxy config, Caddyfile, QGIS projects on Compose) is a redeploy, not an in-place restore.
* Roles/passwords are outside the backup by design.
* Podman is supported by the shared code path; it is verified only if a Podman installation is available (it was not in the development environment).
* Backups are stored on the machine that runs gis-stack; shipping them off-site is the operator's choice (use the encrypted option).
