# Backup and restore - Docker Compose, Podman, Kubernetes

For the **native** (apt/dnf) target use `gisstack backup ...` (see `docs/guides/en/BACKUP.md`). This page covers the container targets. Design: [BACKUP-RESTORE-ARCHITECTURE.md](BACKUP-RESTORE-ARCHITECTURE.md).

## What is backed up

| Component | Content | Restorable in place |
|---|---|---|
| `postgres` | `pg_dump -Fc` of the stack database (consistent snapshot, taken from inside the running database) | yes |
| `geoserver` | the GeoServer data directory (workspaces, layers, styles, configuration) | yes |
| `caddy` | `/data` (ACME account, issued certificates and keys, the local CA of `tls: internal`) | yes |
| `qgis` | QGIS projects | Kubernetes only (a read-only bind mount on Compose) |
| `mapproxy_config` | MapProxy configuration | no - it is configuration: redeploy |
| `mapproxy_cache` | tile cache, only with `--with-cache` (can be very large, rebuildable) | yes |

**Not in a backup, by design:** passwords, tokens, `.env`, Kubernetes Secrets, PostgreSQL roles and their password hashes. A restore targets a stack whose roles already exist (the same stack, or one freshly deployed from the same project). **Note:** the GeoServer data directory contains GeoServer's *own* credential hashes - a backup that holds it is sensitive; use encryption and keep the folder private.

Backups are stored on the machine that runs gis-stack: `~/.gisstack/stack-backups/<project>-<compose|podman|k8s>/<id>/` (owner-only, mode 0700/0600). Compose and Podman backups are interchangeable; Kubernetes backups are not restorable on Compose (and vice versa).

## Requirements

* The workloads must be running (a stopped component fails the backup - nothing is half-saved).
* Docker/Podman rights or `kubectl` access for the user who runs gis-stack (the same as for deployment). Over SSH (`--mode ssh`) the commands run on the server, the backup is stored locally.
* Optional encryption needs `gpg`.

## Commands

```bash
gisstack stack-backup create  -c stack.json [--components postgres,geoserver] [--with-cache] [--note TEXT]
gisstack stack-backup list    -c stack.json
gisstack stack-backup inspect ID -c stack.json
gisstack stack-backup verify  ID -c stack.json        # re-reads every file: checksums, file set, archive structure
gisstack stack-backup check   ID -c stack.json        # pre-restore validation, changes nothing
gisstack stack-backup restore ID -c stack.json        # asks you to type RESTORE
gisstack stack-backup recover -c stack.json           # after an interrupted restore
gisstack stack-backup prune   -c stack.json --keep 14 [--days 30] [--dry-run]
gisstack stack-backup delete  ID -c stack.json
```

The dashboard (**Backups**, for non-native targets) and the API (`/api/v1/stack-backups/*`, see `docs/API.md`) offer the same operations: list with integrity status, details, verify, create (optionally encrypted with a public key), restore with a typed confirmation and a live log, export (download as `.tar`), delete, retention preview, and a banner with a *Recover* button when a restore was interrupted. Scopes: `read` (list, inspect, verify), `operate` (create, check), `admin` (restore, recover, delete, prune, export). Every state-changing call is audited (`stack_backup.*`).

## A backup counts only when it is verified

`create` writes to `<id>.partial`, re-reads and checksums every file, checks every archive (members must not be absolute, contain `..`, be devices, hard-link outside, or sit below a symbolic link), asks `pg_restore --list` to read the dump, writes `manifest.json` + `SHA256SUMS`, and only then renames the directory. A failure leaves `<id>.FAILED` (with the reason) and never a backup. `list` shows partial/failed/damaged entries with that status.

## Encryption

* `--encrypt-to KEY` (public key in your keyring) or `--encrypt` / `--passphrase-file FILE` (passphrase; at least 12 characters; the file must be mode 600). In the dashboard/API only public keys are accepted (`encrypt_to`).
* Each payload is encrypted with gpg (AES-256) after it is written; the checksums cover the stored bytes, so `verify` works **without** the key.
* The key or passphrase is never stored in the backup, manifest or log. Restoring an encrypted backup needs the passphrase (`--passphrase-file` or `GISSTACK_BACKUP_PASSPHRASE`) or the private key (gpg-agent). Lose the key and the backup is unreadable - there is no recovery.

## Restore - what happens

1. **Validate** (`check`): integrity, compatibility (same family; PostgreSQL of the same or newer major version than the dump), the workloads are running, key available. Any problem refuses the restore before anything changes.
2. **Confirm:** you type `RESTORE` (API: `confirm: true`).
3. **Protect:** an automatic, verified *pre-restore backup* of everything that will be replaced.
4. **Stage:** PostgreSQL is restored into a scratch database (`pg_restore --single-transaction --exit-on-error`) and validated; volume data is extracted into a hidden directory inside the same volume and validated.
5. **Swap** (reversible): the live database is renamed `<db>_pre_<id>` and the scratch database becomes live; volume entries are moved to `.gs-pre-<id>`. The previous data stays next to the new one.
6. **Verify:** workloads that hold volume data are restarted, then the same health verification as after a deployment runs. If anything fails at any step the swap is **reversed automatically**; nothing is deleted.
7. **Cleanup is yours:** the retained previous data (`<db>_pre_<id>` database, `.gs-pre-<id>` directory in the volume, and after an undo `.gs-failed-<id>`) is printed in the restore log. gis-stack never deletes it by itself; remove it by hand when you are satisfied (`DROP DATABASE "<db>_pre_<id>"`, `rm -rf <volume>/.gs-pre-<id>`). The provider method `discard_previous` does the same with safety checks (confirm required, only names a restore created).

If the process is killed mid-restore, a state file remains; new restores refuse to start until `gisstack stack-backup recover` has put the previous data back (safe to repeat).

## Retention

`prune --keep N` keeps the newest N regular backups and `--keep-pre` pre-restore backups (default 5); `--days D` additionally only removes backups older than D days. The newest *intact* backup is never removed, whatever the policy says. Partial/failed leftovers older than a day are removed. Use `--dry-run` first.

## Troubleshooting

| Message | Meaning / action |
|---|---|
| `the workload is not running - nothing was backed up` | start the component (`gisstack start ...`) and retry |
| `checksum mismatch` / `missing file` / `unexpected file` | the backup was changed or damaged after it was made; use another backup (`list`), do not edit backup folders |
| `PostgreSQL N cannot load a dump made by PostgreSQL M` | restore into a stack with the same or a newer PostgreSQL major version |
| `the backup is encrypted: a passphrase ... is needed` | pass `--passphrase-file` / set `GISSTACK_BACKUP_PASSPHRASE` / unlock the private key |
| `X cannot be restored in place on ... (it is configuration: redeploy instead)` | MapProxy configuration and (on Compose) QGIS projects are redeployed, not restored in place |
| `an interrupted restore is still recorded: run recover first` | `gisstack stack-backup recover`, then retry |
| `restore failed, the previous data was put back` | the restore was undone; the message says why; nothing was lost |
| `restore failed AND the automatic undo failed` | the previous data is still on disk (`<db>_pre_*`, `.gs-pre-*`); run `recover`, or restore it by hand |

## Disaster recovery (new machine)

1. Install gis-stack and deploy the **same project** (`gisstack deploy`) so the databases' roles, volumes and workloads exist and are healthy.
2. Copy the backup folder (or the exported `.tar`) into `~/.gisstack/stack-backups/<project>-<target>/`.
3. `gisstack stack-backup verify ID`, then `check ID`, then `restore ID`.
4. Check the application (dashboard health, a GeoServer layer, a PostGIS query). Secrets are the ones of the new deployment - they are not part of the backup.

## Verification status (what is tested, honestly)

| Provider | Unit | Real Docker Compose | Real Kubernetes (kind) | Real Podman |
|---|---|---|---|---|
| Compose | yes | yes (PostgreSQL + GeoServer 10 tests; Caddy/MapProxy/QGIS 4; Caddy ACME 1; `--mode ssh` 1) | - | - |
| Kubernetes | yes | - | yes, in CI (PostgreSQL + GeoServer 10 tests; Caddy, MapProxy, QGIS 3 + monitoring 1) | - |
| Podman | yes (shared code path, Podman status/log/control scripts, qualified image names) | - | - | yes: PostgreSQL + GeoServer + gis-stack deployment/status, **rootful Podman 5.8.9 with podman-compose 1.6.0 in a privileged systemd container**; rootless Podman: PostgreSQL + GeoServer backup/restore 11/11 on GitHub (rootful and rootless jobs, `55bc033` and `5ea1c29`) |

On Podman the generated Compose files use fully qualified image names (`docker.io/...`) because Podman refuses short names without a terminal when several registries are configured; status, restart policy and logs read the Podman engine directly (the `podman-compose` provider has no `ps -a`). Docker bundles are unchanged.

### Caddy, QGIS Server, MapProxy - backup and restore verified separately (real Docker Compose)

`tests/integration/test_stack_backup_gis.py`, each against a real throw-away Compose project:

| Component | Backup | Restore | Status |
|---|---|---|---|
| **Caddy** `/data` | archive contains the CA and certificates; verified | the local CA (`root.crt`) is destroyed (`rm -rf /data/caddy`, Caddy then creates a *different* CA); restore brings back the **original `root.crt` byte for byte**, the post-restore health check passes and HTTPS verifies against the restored CA; the replaced state is kept (`.gs-pre-*`) | **COMPLETE** (Compose; the same code path runs on Podman and Kubernetes, which are exercised for PostgreSQL / GeoServer only) |
| **MapProxy cache** | opt-in (`--with-cache`); a sentinel tile is in the backup | sentinel and an extra tile are changed, restore brings the sentinel back and removes the extra tile, previous cache kept; no pre-restore backup is made for a rebuildable cache (by design) | **COMPLETE** (Compose) |
| **MapProxy configuration** | default component, verified | refused *by design* ("it is configuration: redeploy") - the test asserts the refusal | backup **COMPLETE**; in-place restore **NOT APPLICABLE** (redeploy the bundle) |
| **QGIS Server projects** | the project file is inside the verified archive | on Compose the project directory is a read-only bind mount: `check`/`restore` refuse with a reason and nothing is touched (asserted); in-place restore exists only for Kubernetes PVCs | backup **COMPLETE**; restore **PARTIAL**: refused on Compose by design; on Kubernetes (a PVC) it is verified on kind in CI |

Not backed up, on purpose: the Caddyfile and the MapProxy configuration (they are generated: redeploy), Caddy's `/config` volume (only an auto-saved copy of the Caddyfile). Limitations found by the tests: gis-stack's health probe of Caddy uses the standard HTTPS port 443, so a custom `HTTPS_PORT` in `.env` is verified by the container state only, not by the public URL probe (the Caddy test therefore runs on 80/443).

### Caddy ACME certificates (real ACME server)

`tests/integration/test_caddy_acme.py` runs gis-stack's Caddy next to **Pebble** (Let's Encrypt's test CA; no public CA is contacted). Caddy obtains a real certificate for `gis.example.com` by HTTP-01. Verified: the certificate, its key and the ACME account are in the verified backup; **no console line, operation log, manifest or checksum file contains key text**; backup files are 0600 in a 0700 folder; after the state is destroyed Caddy re-issues (control: Pebble's issuance count rises), and after the restore the **same certificate** is served again, Pebble is **not** asked for another one, and owners, groups and modes of the restored tree are identical to the original. The archive itself contains the private key, so it is sensitive: use `--encrypt` / `--encrypt-to` and keep copies private.

### `--mode ssh` (commands run on a server over SSH, the backup is stored on the machine that runs gis-stack)

`tests/integration/test_stack_backup_ssh.py` uses a real `sshd` (own host key, key-only login) in front of a real Compose stack. Verified:

* an **unknown** host is refused before anything runs (`hostkey_unknown`), no valid backup is produced (a `FAILED` entry is recorded and raises the `backup_failed` alert); a **wrong** fingerprint is refused by `gisstack ssh-trust`; the right one establishes trust;
* backup and restore over ssh work, are verified, and the restored data is correct; local backup files are 0600 / 0700; the database password is not in any metadata;
* a **changed** host key is refused with `hostkey_changed` (before this review it was reported as "workload is not running"); nothing runs and nothing is overwritten;
* a connection **killed mid-transfer** leaves no valid backup, no `.partial` directory, and no `pg_dump` keeps running on the server;
* a **tampered** backup fails `verify` and is refused by `restore`; the database is unchanged.

Not covered: very large transfers over slow links, an SSH server that drops idle connections during a long restore, SSH over a jump host.

### Custom `HTTPS_PORT`

The post-restore health check of Caddy probes the standard HTTPS port. On a stack whose `.env` sets another `HTTPS_PORT` the check reports Caddy unhealthy and the restore is reversed automatically (safe, but Caddy cannot be restored there). Keep the standard ports, or restore Caddy's `/data` by hand.

Not covered by real-environment tests: restore over a very large database, concurrent writes during a backup, Caddy / MapProxy cache restore on **Podman**. Verified on a real kind cluster in CI (integration run 42864612991, commit `5ea1c29`): Caddy local-CA backup/restore, MapProxy cache restore and config refusal, QGIS projects restore on the PVC.
