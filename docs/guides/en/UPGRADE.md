# Upgrade guide

Updating gis-stack itself, and moving the installed stack to newer versions. Georgian version: [../ka/UPGRADE.md](../ka/UPGRADE.md).

## 1. Update gis-stack

```bash
gisstack update --check          # exit code 10 when a newer release exists
gisstack update                  # installs it when gis-stack was installed by install.sh
gisstack update --download ~/Downloads     # for deb/rpm installs: fetch the verified package, then install it yourself
```

Nothing is checked in the background; the command contacts GitHub only when you run it.

### What is verified

1. The download must come from this repository's release assets.
2. The file's SHA-256 must match the release's `SHA256SUMS`.
3. `SHA256SUMS` itself must carry a valid [Sigstore](https://www.sigstore.dev/) signature made by this repository's release workflow. This needs the `cosign` program on the machine. Without it the update continues after a warning, because the checksum comes from the same place as the file. To make a missing or unverifiable signature a hard failure:

```bash
export GISSTACK_REQUIRE_SIGNATURE=1
```

You can also verify by hand:

```bash
cosign verify-blob --bundle SHA256SUMS.sigstore.json \
  --certificate-identity-regexp '^https://github\.com/GIS-GEORGIA/gis-stack/\.github/workflows/release\.yml@refs/tags/v[0-9.]+$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing
```

Releases made before signing was introduced have no signature file; they are reported as unsigned.

## 2. Before upgrading the stack

1. Read the release notes (`CHANGELOG.md`, also shown by `gisstack update`). Changes that need action from you are listed there.
2. Take a verified backup: `gisstack backup create --mode ssh --ssh root@server` (see the [backup guide](BACKUP.md)).
3. Run `gisstack preflight` against the server.

## 3. Upgrade the stack

Re-running the installer upgrades packages and rewrites generated files. Your saved configuration and generated passwords are kept.

```bash
gisstack deploy -c stack.json --mode ssh --ssh root@server       # everything
gisstack apply  -c stack.json mapproxy                           # just one program
```

A PostgreSQL **major** upgrade is not done for you: change the version only together with a dump/restore plan (take a backup, install the new version side by side, restore, switch).

## 4. If it goes wrong

Restore the backup from step 2 (`gisstack backup restore ID`), which rolls back automatically when the restore fails. gis-stack's own virtualenv can be pinned back with `pip install gisstack-installer==<old version>` in the same environment.
