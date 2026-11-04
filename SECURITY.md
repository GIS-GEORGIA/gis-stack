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

**Not covered yet (see `docs/ROADMAP.md`):** release artefacts are not signed (S-09, needs a decision on the signing tool), the CSP
still allows inline scripts (S-07), SSH host keys are trust-on-first-use (S-08, Phase 9), rollback after a failed upgrade and a
`restore` command (R-03, Phase 7), and the Tk desktop GUI is not part of the automated coverage figure.
