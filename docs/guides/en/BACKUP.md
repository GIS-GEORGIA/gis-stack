# Backup and recovery guide

A short path through backing up and restoring. Georgian version: [../ka/BACKUP.md](../ka/BACKUP.md). The complete runbook with every option is [docs/RECOVERY.md](../../RECOVERY.md). Backups apply to the **native** installation.

## 1. Make a backup

```bash
gisstack backup create --mode ssh --ssh root@server
gisstack backup list   --mode ssh --ssh root@server
gisstack backup verify ID --mode ssh --ssh root@server
```

A backup holds the configuration (including `credentials.txt`), a consistent PostgreSQL dump with roles, and the GeoServer and QGIS data. The MapProxy tile cache is left out unless you add `--with-cache` (it can be rebuilt). **A backup contains secrets: protect the directory and every copy.**

## 2. Keep a copy somewhere else

```bash
gisstack backup download ID --dest ~/gis-backups --mode ssh --ssh root@server
```

The copy is checksum-verified and never overwrites an existing file. A backup that lives only on the server it protects is not a backup.

## 3. Automate it

```bash
gisstack backup schedule enable --calendar daily --keep 14 --days 30 --mode ssh --ssh root@server
```

This installs a systemd timer on the server. The newest backup is never pruned.

## 4. Restore

```bash
gisstack backup restore ID --mode ssh --ssh root@server --dry-run     # only show the plan
gisstack backup restore ID --mode ssh --ssh root@server               # asks, then restores
```

A restore verifies the backup, takes a verified pre-restore backup, swaps data by renaming, validates the result and rolls back by itself on failure. Old data is kept until you remove it.

## 5. Practise on a spare machine

Once in a while restore into a new server (`gisstack backup upload`, then `restore`) or into a staging database (`--staging`). A backup you have never restored is a hope, not a plan.
