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

## 3. Encrypt the copies

A backup holds passwords, so a copy that leaves the server should be encrypted. This needs `gpg` on your machine. Encryption happens on **your** machine after the download has been verified; the server never sees your passphrase or key.

```bash
echo 'a long passphrase you will not forget' > ~/.gisstack-backup.pw && chmod 600 ~/.gisstack-backup.pw
gisstack backup download ID --dest ~/gis-backups --passphrase-file ~/.gisstack-backup.pw --mode ssh --ssh root@server
gisstack backup download ID --dest ~/gis-backups --encrypt-to backup-admin@example.org --mode ssh --ssh root@server
gisstack backup decrypt ~/gis-backups/<host>/ID.tar.gz.gpg --passphrase-file ~/.gisstack-backup.pw
gisstack backup upload ~/gis-backups/<host>/ID.tar.gz.gpg --passphrase-file ~/.gisstack-backup.pw --mode ssh --ssh root@newserver
```

* **Passphrase** (AES-256): at least 12 characters, read from a `chmod 600` file, from `GISSTACK_BACKUP_PASSPHRASE`, or asked for twice. It is never put on a command line. Lose it and the backup is gone.
* **Public key** (`--encrypt-to`): only the owner of the private key can decrypt; the key must already be in your keyring.
* The result is `ID.tar.gz.gpg`; the plaintext copy is removed. A passphrase copy is decrypted and verified once before the command succeeds. Modified files do not decrypt.
* `upload` decrypts locally and sends the server the plaintext archive over SSH. Backups kept on the server itself are not encrypted: the live secrets are on the same machine.

## 4. Automate it

```bash
gisstack backup schedule enable --calendar daily --keep 14 --days 30 --mode ssh --ssh root@server
```

This installs a systemd timer on the server. The newest backup is never pruned.

## 5. Restore

```bash
gisstack backup restore ID --mode ssh --ssh root@server --dry-run     # only show the plan
gisstack backup restore ID --mode ssh --ssh root@server               # asks, then restores
```

A restore verifies the backup, takes a verified pre-restore backup, swaps data by renaming, validates the result and rolls back by itself on failure. Old data is kept until you remove it.

## 6. Practise on a spare machine

Once in a while restore into a new server (`gisstack backup upload`, then `restore`) or into a staging database (`--staging`). A backup you have never restored is a hope, not a plan.
