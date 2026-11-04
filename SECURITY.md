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

**Not covered yet (see `docs/ROADMAP.md`, Phase 1b-ii):** the web GUI uses a single static token (no session expiry, rate limit,
`Host` allow-list or audit log), commands have no global deadline or cancellation guarantees, the uploaded bundle stays in the
server user's home after deployment, and release artefacts are not signed.
