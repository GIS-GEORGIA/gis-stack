# უსაფრთხოება / Security Policy

ეს ხელსაწყო სერვერებზე ააყენებს სერვისებს root უფლებებით და მართავს პაროლებს, ამიტომ უსაფრთხოების
შეტყობინებებს სერიოზულად ვუყურებთ. / This tool installs services as root and handles credentials, so we take
security reports seriously.

## დაუცველობის შეტყობინება / Reporting a vulnerability

**ნუ გახსნით ღია issue-ს.** გამოიყენეთ GitHub-ის კერძო არხი:
**Security → [Report a vulnerability](https://github.com/GIS-GEORGIA/gis-stack/security/advisories/new)**.

მიუთითეთ: ვერსია, დაზარალებული ფუნქცია, გამეორების ნაბიჯები და შესაძლო გავლენა. შეძლებისდაგვარად
სწრაფად გიპასუხებთ და გამოსწორების შემდეგ მადლობას გადაგიხდით (თუ ინებებთ — სახელის მითითებით).

## მხარდაჭერილი ვერსიები / Supported versions

მხოლოდ უახლესი გამოშვება და `main`.

## ფარგლები / Scope

მაგ.: შაბლონებიდან გენერირებული სკრიპტების ინექცია, ვებ GUI-ის ავტორიზაციის/CSRF-ის გვერდის ავლა,
პაროლების გაჟონვა ლოგებში ან ფაილებში, არასწორი ფაილების უფლებები. მესამე მხარის პროგრამების
(GeoServer, PostgreSQL და სხვ.) დაუცველობა — შეატყობინეთ მათ ავტორებს.

## Security model and guarantees (Phase 1a)

**Trust model.** The operator machine and the operator's own configuration are trusted. A configuration file received
from somebody else is **not** trusted: values are validated before they are written into root shell scripts, `stack.env`,
a Caddyfile, `pg_hba.conf`, systemd units or YAML.

**Configuration validation** (`gisstack/validation.py`): every field has a type, a range or a pattern; control characters
(newline, NUL, ...) are rejected in every string; paths must be absolute without `..`; CIDRs, e-mail addresses, image
references, Kubernetes names and URLs are checked; list sizes are bounded. Unknown keys are reported as warnings
(typos no longer disappear silently); set `GISSTACK_STRICT_CONFIG=error` to make them errors. Malformed files
(bad JSON, wrong types, files over 1 MB) produce a clear error and exit code 2, never a traceback. Templates quote every
value for the context it is written into (`shq`, `shdq`, `shdw`, `yq`); a fuzz test injects hostile text into every
string field and proves the generated files stay inert.

**Backups before destructive steps fail closed.** A backup must pass every check (archives readable, dumps restorable,
SHA-256 sums recorded and re-verified, the downloaded copy re-verified) before any program is removed. A failed backup is
kept as `<id>.FAILED` for inspection and **nothing is deleted**. Backup ids contain a random suffix, existing backup
directories and local backup files are never overwritten, and backups are created owner-only (`0700`/`0600`).
Actions that delete data (`+purge`) additionally require an explicit confirmation (`--confirm-data-loss`,
`confirm_data_loss` in the API, a warning dialog in the GUIs).

**Files with secrets** (`stack.env`, `.env`, `secret.yaml`, `stack.json`, tokens, saved projects, settings, backups) are
created with their final restrictive mode from the first byte and replaced atomically; `~/.gisstack` and generated bundle
directories are `0700`; the uploaded bundle on a server is created under `umask 077`.

**Secrets in process arguments (S-03).** `install.sh` never puts a password on a command line, where any local user could read it
from `/proc/<pid>/cmdline`: the PostgreSQL role password reaches `psql` as hex on stdin and is decoded server-side, and the
GeoServer admin credentials and new password go to `curl --config -` on stdin. Checked by tests (helpers run in real bash against
a real HTTP server; stub `curl` records its argv) and on an Ubuntu 24.04 VM by sampling `/proc/*/cmdline` during a full install
(sampling every ~20 ms, so a very short-lived process could in principle be missed; the structural tests are the guarantee).

**Downloads executed as root (S-04).** GeoServer (3.0.2 and 2.28.6) and its extensions and Caddy are verified against SHA-256 values
pinned in `gisstack/pins.py` and shipped as `config/pins.sha256`; a mismatch aborts, and so does a download without a pin (override:
`GS_ALLOW_UNVERIFIED=1`, e.g. for a custom `GS_VERSION`). Caddy comes from its GitHub release at a pinned version instead of an
unverified API download. The MapProxy virtualenv is installed with `pip --require-hashes` from a fully pinned list
(`templates/native/mapproxy-requirements.txt`, scanned by `pip-audit` in CI). Tomcat is checked against the checksum on Apache's
own distribution hosts. GeoServer publishes no checksums of its own: the pins were computed from one download on 2026-11-04, so
they protect against later tampering and mirror corruption, not against a file that was already bad on that day.

SSH connections now use keep-alive (`ServerAliveInterval=15`, `ServerAliveCountMax=4`), so a dead link ends an operation in about
a minute instead of hanging.

**Operations are bounded, serialised and recorded (R-01, R-02, R-04, R-05).** Every external command runs through `gisstack/runner.py`:
an overall deadline (default 1 h, `GISSTACK_DEADLINE`) and an idle timeout (15 min without output, `GISSTACK_IDLE_TIMEOUT`; `0` = no
limit), cancellation, and termination of the whole process group (SIGTERM, then SIGKILL) so a hung `ssh` or installer child never
survives; secret values (every `*password*`/`*token*`/`*secret*` in the config) are masked in all output. Operations take a lock
(`flock` on this machine, `flock -n` on the server; a second one is refused with a clear message) and are journalled to
`~/.gisstack/journal.jsonl` (start/end, outcome, duration; no commands, no secrets). The native bundle (it contains `stack.env`) is
removed from the server's home directory after an SSH deployment, successful or not (compose/k8s bundles stay because the running
stack mounts them). `gisstack preflight` (also run before `deploy`; `--skip-preflight` overrides) checks OS, architecture, memory,
disk, clock, reachability of the download hosts and DNS before anything is changed.

**API tokens and plugins (Phase 10).** Automation uses `gisstack token create --scope read|operate|admin` (`gsk_<id>_<secret>`); only a SHA-256
of the secret is stored (file mode 0600), tokens can carry an expiry (`--ttl-days`; default: none) and can be revoked, and they are accepted on `/api/v1` only. Every request is
checked against the route's required scope (`403 forbidden` otherwise) and the audit log records principal, scope and denials, never the token. Failed
authentication is rate limited. Plugins execute third-party code in-process, so they are loaded only with `GISSTACK_FEATURES=plugins`; every call into them
is exception-guarded, and plugin tokens are accepted on `/api/v1` only. Install only plugins you trust.

**Web GUI (S-06, S-10).** The access token is exchanged once for a random server-side session cookie (HttpOnly, SameSite=Strict) that
expires after 2 h idle / 12 h total and can be revoked (`POST /api/logout`); `gisstack web --rotate-token` replaces the token. After
10 failed attempts a client address gets `429` for a minute. Only `localhost`/`127.0.0.1`/`[::1]` plus names given with `--allow-host`
(or `GISSTACK_ALLOWED_HOSTS`) are accepted as `Host` (DNS-rebinding protection); request bodies are limited to 2 MB; every
state-changing request, login and denial is appended to `~/.gisstack/audit.log` (0600; method, path, status, client address, session
prefix and a few non-secret facts such as project, mode, SSH host; never passwords, keys, plans or file contents). The job registry
keeps at most 50 jobs, 50 000 lines each, and forgets finished jobs after an hour. `gisstack web --server waitress` serves with a
production WSGI server (`pip install gisstack[prod]`); the default is Flask's development server.
*Privilege model:* the web GUI runs as the user who starts it and executes installer scripts as that user (with `sudo -n` on remote
servers). The packaged service runs as root on purpose, listens on `127.0.0.1` only and is reached through an SSH tunnel; do not
expose it to a network. Splitting the UI from a privileged helper is planned (Phase 10).

**Configuration and secrets.** Config files carry `schema_version` (older files are migrated, files from a newer gisstack are
refused). Passwords may be supplied through `GISSTACK_POSTGRES_PASSWORD`, `GISSTACK_GEOSERVER_ADMIN_PASSWORD`,
`GISSTACK_TOMCAT_MANAGER_PASSWORD` or the same names with `_FILE` (a file readable only by you), or by a custom provider registered
in `gisstack/secretsrc.py` (Vault/SOPS adapters can plug in there); such values are validated like any other and never written to
the secret store. `gisstack rotate-credentials postgres|geoserver` changes the PostgreSQL role password or the GeoServer admin
password on an installed *native* server: the new password goes over stdin, is verified against the running service before
anything is written, is rolled back on failure, then updates `/etc/gisstack/stack.env`, `credentials.txt` and the local secret
store. Containers and Kubernetes are not supported by rotation yet.

**Backups and restores (R-03).** `gisstack-backup` (see `docs/RECOVERY.md`) writes sealed, checksummed backups (0700/0600), refuses
to restore a damaged backup, takes a verified pre-restore backup first, replaces data by renaming, validates recovery and rolls back
on failure. Restore refuses archives with `..`, absolute paths, escaping links or members outside the allowed directories. Backups
contain secrets (`credentials.txt`, role password hashes): they are not encrypted yet - keep the directory and downloaded copies private.

**Signed releases (S-09).** The release workflow signs `SHA256SUMS` keylessly with Sigstore (cosign, bound to the workflow's GitHub identity, so there
is no private key to steal) and attaches `SHA256SUMS.sigstore.json`; the workflow verifies the signature itself before publishing. `gisstack update`
checks the signature (needs the `cosign` program; the accepted identity is only this repository's `release.yml` run for a version tag), then the
file's SHA-256. Without cosign, or for releases that predate signing, it warns and relies on the checksum; `GISSTACK_REQUIRE_SIGNATURE=1` makes that a
refusal. A signature that is present and wrong always refuses. The signing step has only run in CI once a tag is pushed - see `docs/guides/en/UPGRADE.md`
for manual verification.

## Threat model

| Asset | Who could attack it | How | What stops it | Residual risk |
| --- | --- | --- | --- | --- |
| Root on the target servers | A malicious configuration file; a malicious download; a man in the middle on SSH | injection into root scripts; tampered installer assets; a fake host | validated and quoted configuration (fuzz-tested); pinned SHA-256 for every download run as root; server profiles pin host keys (`StrictHostKeyChecking=yes`) | without a profile SSH is trust-on-first-use; distro repositories are trusted as the distro's own signing makes them |
| The operator machine and `~/.gisstack` (secrets, projects, backups, tokens) | Other local users; a web page in the operator's browser | file reads; cross-site requests to the local GUI | `0700`/`0600`, atomic writes; token + CSRF header + Host allow-list; loopback only by default; scoped API tokens stored as SHA-256 only | anyone who is the operator's user, or root, on that machine |
| Database content (PostGIS manager) | A user of the dashboard/API | arbitrary SQL | read-only role with limits, read-only transaction, statement timeout; write access needs a time-boxed, audited elevation | a user with `admin` scope can elevate by design |
| GeoServer/MapProxy configuration and caches | A user of the dashboard/API; hostile XML/YAML | destructive edits; XML entity attacks | validation by the programs themselves, automatic backups, dry-run defaults, DTD/entity rejection, typed confirmations | `admin` scope can change configuration by design |
| Releases and updates | A compromised download path or release asset | replaced files | HTTPS from this repository only, SHA-256, Sigstore signature of `SHA256SUMS` | a compromise of the GitHub repository or of its release workflow itself; unsigned old releases |
| Backups | Anyone with the files | reading secrets inside | owner-only permissions, checksums | backups are not encrypted yet |
| Plugins (flag `plugins`) | Third-party packages | arbitrary code in-process | off by default; contract checks, error isolation | a plugin runs with the program's privileges: install only what you trust |

Out of scope: a compromised operator machine, a compromised root account on the target, and denial of service against the local GUI.

**Not covered yet (see `docs/ROADMAP.md`):** the CSP
still allows inline scripts (S-07), SSH host keys are trust-on-first-use unless you use a server profile with a pinned host key (S-08; `docs/SERVERS.md`, flag `multi_server`), rollback after a failed upgrade and a
`restore` command (R-03, Phase 7; native backups are done), and the Tk desktop GUI is not part of the automated coverage figure.
