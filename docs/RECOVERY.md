# Backup, restore and disaster-recovery runbook

Applies to a **native** installation (Compose and Kubernetes are not covered yet). The work is done on the server by
`/usr/local/sbin/gisstack-backup` (installed by `install.sh`, or automatically by the first `gisstack backup ...` command).

## What a backup contains

| Component | Content | Notes |
|---|---|---|
| `config` | `/etc/gisstack` (stack.env, **credentials.txt - secrets**), Caddyfile, Tomcat `server.xml`/`setenv.sh`, systemd units, web map | symlinks (e.g. `/opt/tomcat`) are stored as the real files |
| `postgres` | `pg_dump -Fc` of the stack database + `pg_dumpall --globals-only` (roles, password hashes) | consistent snapshot per database |
| `geoserver` | the GeoServer data directory | copied while running (changed files are re-read once; a warning is recorded if they still change). `GS_BACKUP_QUIESCE=1` stops Tomcat during the copy for a fully quiet copy |
| `qgis` | the QGIS projects directory | |
| `cache` | MapProxy tile cache | only with `--with-cache` (can be huge, can be rebuilt) |

Stored in `/var/backups/gisstack/stack/<id>/` (mode 0700, files 0600): the archives, `SHA256SUMS`, `MANIFEST.json` (id, kind, time,
host, gisstack version, database, PostgreSQL version, per-file size and SHA-256, warnings). **A backup contains secrets - protect
the directory and any copy you download.** A backup becomes visible only after every file was re-read and checksummed
(`<id>.partial` -> `<id>`); a failure is kept as `<id>.FAILED` and never counts as a backup.

## Everyday commands

```bash
gisstack backup create   --mode ssh --ssh root@server [--components config,postgres] [--with-cache] [--download DIR]
gisstack backup list     --mode ssh --ssh root@server
gisstack backup verify   ID ...                         # re-reads every file and checks all checksums
gisstack backup prune    --keep 14 [--days 30] [--keep-pre 5] [--dry-run]
gisstack backup download ID [--dest DIR]                # verified copy to this machine, never overwrites
gisstack backup schedule enable --calendar daily --keep 14 --days 30     # systemd timer on the server (disable / status)
```

Retention: the newest `--keep` backups are kept; with `--days` older ones are removed only when they are also older than that.
The newest backup is never deleted. Pre-restore backups have their own limit (`--keep-pre`). Unfinished `.partial` (> 1 day) and
`.FAILED` (> 7 days) leftovers are cleaned up.

## Encrypted copies

`gisstack backup download ID --passphrase-file FILE` (or `--encrypt` to be asked, or `--encrypt-to KEY` for a public key; `backup create --download` takes the same options) writes `ID.tar.gz.gpg` instead of `ID.tar.gz`. Encryption is done locally with `gpg` after the archive was verified; the passphrase never goes on a command line (file with mode 600, `GISSTACK_BACKUP_PASSPHRASE`, or a prompt, at least 12 characters). A passphrase copy is decrypted and verified once before the command succeeds; a public-key copy is checked as a well-formed authenticated OpenPGP message (the private key may be offline). `gisstack backup decrypt FILE.gpg` decrypts on this machine; `gisstack backup upload FILE.gpg` decrypts locally and sends the plaintext archive over SSH. The API route `/backups/offsite` accepts `encrypt_to` (public keys only) so BackupProvider plugins receive the encrypted file. Backups stored on the server are not encrypted. Lose the passphrase or the private key and the copy cannot be recovered.

## Restoring

```bash
gisstack backup restore ID --mode ssh --ssh root@server [--only postgres,geoserver]   # shows the plan, asks, then restores
gisstack backup restore ID ... --dry-run          # only the plan (also what the web API does by default)
gisstack backup restore ID ... --staging          # unpack next to the live data; PostgreSQL goes to a new database "gis_staging_..."
```

What an in-place restore does, in order:

1. verifies the backup (checksums, manifest, dump readable) - a damaged backup is refused and **nothing is touched**;
2. checks disk space and that archives cannot write outside their allowed directories (no `..`, no absolute paths, no escaping links);
3. takes a **verified pre-restore backup** of the current state (`pre-...`) - if that fails the restore is refused;
4. replaces data **by renaming**: the old database becomes `gis_old_<stamp>`, the old directories become `*.pre-restore-<stamp>`;
5. starts the services again and runs **recovery validation** (database answers, GeoServer answers);
6. if anything fails it **rolls back automatically** (renames everything back) and prints `RESTORE_FAILED_ROLLED_BACK`.

After a successful restore the old data is deliberately kept. Remove it when you are satisfied:
`DROP DATABASE gis_old_<stamp>;` and `rm -rf /var/lib/gisstack/geoserver_data.pre-restore-<stamp>`.

To undo a restore: `gisstack backup restore pre-<id> --yes` (the pre-restore backup is a normal backup).

## Disaster recovery on a new server

1. Install the stack on the new machine with the same configuration (`gisstack deploy ...`), or at least the native install steps.
2. `gisstack backup upload <id>.tar.gz --mode ssh --ssh root@newserver` (verifies the archive here, refuses to overwrite).
3. `gisstack backup restore <id> --mode ssh --ssh root@newserver`.
4. The PostgreSQL role password lives in `config` (stack.env) and the globals dump; if the new server uses different passwords restore
   `config` too, or rotate afterwards with `gisstack rotate-credentials`.

## If something looks wrong

* `RESTORE_FAILED_NO_ROLLBACK` (only with `--no-rollback`): the old data is in `gis_old_*` / `*.pre-restore-*`; rename them back by hand.
* The server rebooted in the middle of a restore: the old data is still next to the new one (see above); run `gisstack-backup list`
  and `status` and decide - nothing is deleted automatically.
* `ERROR: another gisstack-backup operation is running` (exit 75): wait, or look for a stuck `gisstack-backup` process.
