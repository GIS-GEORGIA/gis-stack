# Multi-server management (flag `multi_server`)

Operate several servers from one dashboard, each with its own credentials and its own pinned host key.

```bash
export GISSTACK_FEATURES=dashboard,multi_server
gisstack web
```

Open **Servers** in the dashboard.

## Profiles

A profile stores: id, display name, **environment** (`production` / `development` / `testing`), host, port, user, the **path** of an SSH key (empty = use `ssh-agent`), notes and the pinned host key. Profiles live in `~/.gisstack/servers.json` and `~/.gisstack/known_hosts/<id>`, both `0600` in a `0700` directory.

What is **never** stored: private key contents, passwords, passphrases. A key field that contains key material, a newline or a relative path is rejected. Privilege escalation is `sudo -n` (passwordless sudo) only; there is no password prompt.

Changing host or port drops the pinned host key, because the key belongs to that address.

## Pinned host keys

Every operation through a profile runs `ssh` with `StrictHostKeyChecking=yes`, the profile's own `UserKnownHostsFile` and `GlobalKnownHostsFile=/dev/null`. An unpinned server is refused before any connection; a changed key stops the connection.

1. **Pin host key** scans what the server presents and lists SHA256 fingerprints. Nothing is trusted yet.
2. Compare a fingerprint with the server console (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`) and type it. gis-stack pins the key whose fingerprint equals what you typed.
3. Replacing an existing pin needs `replace=true` (the UI asks you to type `REPLACE`): a changed key is either a rebuilt server or an attack, and you have to say which.

The scan and the pin are two separate connections; the typed fingerprint is what protects against an attacker who answers only one of them.

## Using a server

**Use** makes the dashboard talk to that server: every request carries `"server": "<id>"` and the profile's credentials replace the `ssh` fields. Over the API, any route that takes `mode`/`ssh` also accepts `server` (with `mode: "ssh"`); the `ssh` object sent in the same body is ignored. Without `server` the behaviour is unchanged (`ssh` with `accept-new`).

## Health, comparison, history

- **Check** (`POST /servers/{id}/probe`): reachability, OS, kernel, sudo, load, free disk. A failure is a normal result, not an HTTP error.
- **Compare** (`POST /servers/compare`, 2-6 servers): OS, kernel, arch, PostgreSQL/Caddy/MapProxy/GeoServer versions, service states and SHA-256 of `mapproxy.yaml`, `seed.yaml` and the Caddyfile. Hashes show *that* configuration differs, never its content. An unreachable server is listed with its error and marks the result incomplete.
- **History** (`GET /servers/{id}/history`): journal rows (deploy, service control, edits) recorded against the server's host. Two profiles for the same host share one history.

## API (`/api/v1`)

| Route | Scope |
| --- | --- |
| `GET /servers` | read |
| `PUT /servers/{id}`, `DELETE /servers/{id}` | admin |
| `POST /servers/{id}/hostkey/scan`, `.../hostkey/pin` | admin |
| `POST /servers/{id}/probe`, `POST /servers/compare`, `GET /servers/{id}/history` | read |

Changes and pins are written to the audit log (`servers.save`, `servers.delete`, `servers.pin`).

## Not covered yet

Fleet-wide deploys, drift remediation, keyring-backed passphrases or sudo passwords, Compose/Kubernetes contexts as servers. SSH paths were tested against a real unprivileged `sshd` on 127.0.0.1, not against remote hosts or other OpenSSH versions.
