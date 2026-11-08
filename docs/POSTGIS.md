# PostGIS manager (Phase 5, feature flag `postgis_manager`)

`GISSTACK_FEATURES=postgis_manager gisstack web` -> dashboard page **PostGIS manager** (`/dashboard#/postgis`) and `POST /api/v1/postgis/*`.
Native target only for now (Compose/Kubernetes give a clear error).

## What you get
* **Inventory and health:** databases and sizes, schemas, tables (size, row estimate), spatial columns (type, SRID, dimensions, **spatial index present?**),
  extensions installed/available, connections by state, buffer-cache hit ratio, `pg_stat_statements` top queries when installed. Findings only state facts
  they rest on (e.g. a geometry column in a table of >= 1000 rows without a GiST/SP-GiST index; tiny or empty tables only get one info line).
* **SQL editor** (read-only): ONE statement at a time; `SELECT/WITH/VALUES/TABLE` are paged (limit 1..5000, offset, "next/previous"), everything else
  (EXPLAIN, SHOW ...) is size-capped; results are limited to 4 MB; NULL and empty string stay different.
* **Write mode:** explicit, time-boxed (max 15 min, default 5), per caller + project + machine; the dashboard asks you to type `WRITE`; every write is in
  the journal and the audit log (principal, statement hash, first 120 characters, outcome).

## The safety boundary is the database, not the parser
The statement splitter only gives friendly messages (one statement, no `\` commands). What actually protects the server:

| Layer | Read-only editor | Write mode |
|---|---|---|
| Database role | `gisstack_ro`: LOGIN, no superuser/createdb/createrole/replication, `CONNECTION LIMIT 3`, `default_transaction_read_only=on`, `statement_timeout` (default 15 s), SELECT on existing and future tables of the stack's owner role, `pg_read_all_stats` | the stack's own database role (never a superuser) |
| Transaction | every statement is prefixed `SET TRANSACTION READ ONLY;` | normal |
| Password | root-only file on the server (`/var/lib/gisstack/postgis-ro.pw`, 0600); never sent to the web application or the browser | from the stack's configuration, passed to the script on stdin |
| OS user running `psql` | `nobody` | `nobody` |
| Statement transport | base64 on stdin; one `psql -c` string that ALWAYS starts with a fixed `SET ...;` | same |

Why the fixed prefix: `psql -c '\! cmd'` executes a shell command; with the prefix every backslash is a server syntax error (tested against a real
server, together with `COPY ... PROGRAM`, `pg_read_file`, `lo_import`, `pg_authid`, DDL, DML, role creation and session-setting tricks - all refused).

Set the role up once with **Set up the read-only role** in the editor (or `POST /api/v1/postgis/editor/setup`, admin). It is idempotent and also refreshes
the grants for schemas created later; the generated password is kept if it already works.

Known limits (documented, not hidden): statement text is visible in the server's process list for the duration of a statement (local administrators
only); the editor can read every table the owner role can (use a separate database for data that must stay private); a user can `SET` session variables
inside a statement, but the role and the already-open read-only transaction still decide what is possible; extensions such as `dblink`, if installed by you
with public EXECUTE, are outside this model.

## Scopes
read: `inventory`, `health` - operate: `query` - admin: `editor/setup`, `elevate`, `elevate/revoke`, `execute`.

## Tests
`tests/test_postgis.py`: splitter, inventory/health logic, paging/limits, error mapping, elevation binding and expiry, script construction (no secrets in
command lines), API scopes and audit; and **the real shell scripts against a real PostGIS 17 container** (`GISSTACK_TEST_DOCKER=1` pulls the image; CI does
on one matrix entry): read/paging/byte cap, 25 write/escape attempts, shell-escape attempts, statement timeout, wrong passwords, elevated writes.
