# Troubleshooting guide

Georgian version: [../ka/TROUBLESHOOTING.md](../ka/TROUBLESHOOTING.md). First look at what is actually running:

```bash
gisstack status -c stack.json
gisstack logs -c stack.json geoserver -n 200
```

## The web GUI says "unauthorized" or will not open

The GUI listens on `127.0.0.1` only and needs the token from the link `gisstack web` prints. On a server, tunnel the port (`ssh -L 8765:127.0.0.1:8765 user@server`) and open the link on your own machine. The token file is `/var/lib/gisstack-installer/web.token`. The dashboard page needs `GISSTACK_FEATURES=dashboard`.

## Deploy stops at the preflight check

Read the message: it names the missing resource (disk, memory, a busy port, a wrong clock). `gisstack scan` shows which program already owns a port such as 80 or 443. Decide per program with `--existing postgis=adopt,caddy=reuse,nginx=disable`, or free the port. Skipping the check is possible (`--skip-preflight`) but you then own the consequences.

## SSH problems

* `Permission denied`: gis-stack never asks for a password. Use a key (`--key`) or an agent.
* A non-root user needs passwordless `sudo` (`sudo -n true` must succeed).
* `Host key verification failed` with a saved server profile: the server's key no longer matches the pinned one. Either the server was rebuilt or someone is intercepting the connection. Compare the fingerprint on the server console, then pin again (Servers page, or `replace=true` in the API).
* Connection hangs: gis-stack gives up after about a minute without a response; check firewalls and the port.

## A program is not running

`gisstack status` shows state and port. Then `gisstack logs -c stack.json <program> -f`. Common causes: a port taken by another program, too little memory for the GeoServer/Tomcat heap (lower it in the configuration), or a configuration file edited by hand and rejected (the MapProxy page validates with MapProxy itself and keeps backups). Restart with `gisstack restart -c stack.json <program>`.

## RPM systems (Rocky / Alma / Fedora)

Enable EPEL and CRB (the installer does it), and note that `pg_isready` is not on `PATH`: use `runuser -u postgres -- psql -c 'select 1'` to test the database. SELinux stays enforcing; do not disable it to make a service start, read `ausearch -m avc -ts recent` instead.

## Update fails

* "SHA-256 does not match": the download was damaged or tampered with; the file is deleted. Retry; if it repeats, report it.
* "signature is invalid": the release's `SHA256SUMS` was not signed by this repository's release workflow. Do not install; report it.
* "no signature" or "cosign is not installed" is a warning unless `GISSTACK_REQUIRE_SIGNATURE=1` is set.

## Where the evidence is

| What | Where |
| --- | --- |
| Operations (deploy, restart, edits) | `~/.gisstack/journal.jsonl` |
| Who changed what through the web/API | `~/.gisstack/audit.log` |
| gis-stack's own log | `~/.gisstack/gisstack.log` (`--log-level debug`) |
| Generated passwords on the server | `/etc/gisstack/credentials.txt` (root) |
| Backups on the server | `/var/backups/gisstack/stack/` |

When you report a problem, include `gisstack --version`, the command, and the relevant journal lines. Never paste `credentials.txt` or tokens.
