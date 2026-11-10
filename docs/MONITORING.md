# Monitoring and alerts

Feature flag: `export GISSTACK_FEATURES=monitoring` (add `prometheus` for the `/metrics` text export). Without the flag the commands
and endpoints answer "switched off". Agentless: nothing is installed on the monitored machine - every sample is one read-only script
run here or over SSH (about 4 s), exactly like `gisstack status`.

## What is measured (all from real sources)

| Area | Source | Metrics (examples) |
|---|---|---|
| Host | `/proc/loadavg`, `/proc/meminfo`, `/proc/stat` (two readings 1 s apart), `/proc/uptime`, `df` | `host_cpu_percent`, `host_mem_used_percent`, `host_load1/5/15`, `disk_used_percent{mount}` |
| Services (native) | systemd cgroup members' `/proc/<pid>/stat` + `status`, `systemctl show` | `service_up`, `service_cpu_percent` (of one core), `service_rss_bytes`, `service_restarts` |
| Services (Compose / Podman) | engine state (`compose ps` / `podman ps`) and `docker|podman stats --no-stream` | `service_up` always; `service_cpu_percent` / `service_rss_bytes` only when the engine returns usage rows (see below) |
| PostgreSQL | `pg_stat_activity`, `pg_stat_database`, `pg_settings`, `pg_locks` | `pg_up`, `pg_connections_percent`, `pg_database_size_bytes{database}`, `pg_cache_hit_ratio`, `pg_longest_query_seconds`, `pg_waiting_locks` |
| Health checks | `curl` timings / TCP connect against the public and local endpoints | `probe_up{probe}`, `probe_seconds{probe}` |
| Backups (native) | `gisstack-backup list` | `backup_count`, `backup_last_age_seconds`, `backup_failed_count` |
| PostgreSQL + backups (Compose / Podman / Kubernetes) | `psql` through the same engine access the backups use; the local stack-backup store | `pg_up`, `pg_connections_total/percent`, `pg_longest_query_seconds`, `pg_waiting_locks`; `backup_count`, `backup_last_age_seconds`, `backup_failed_count` |
| The monitor itself | | `monitor_reachable`, `collect_seconds`, `collect_errors` |

Kubernetes: service state and restarts, PostgreSQL health and backups (no host metrics: the host is the cluster).

**Backup alerts:** `backup_failed` (critical) fires when a backup attempt left a `.partial` / `.FAILED` / damaged entry newer than the newest good backup, and resolves by itself after the next good backup; `backup_old` fires when the newest good backup is older than 36 h. A stack that has *never* been backed up has no age and therefore raises no `backup_old` alert - schedule a first backup.

**Podman note:** per-service CPU / memory come from `podman stats`; some nested or containerised engines return no rows, then those two metrics are simply absent (verified on rootful Podman 5.8.9 inside a privileged systemd container). **If a source is unavailable the metric is absent and
`collect_errors` says so - nothing is ever estimated or made up.** An unreachable machine is recorded as `monitor_reachable = 0`.

## Commands

```bash
gisstack monitor collect --mode ssh --ssh root@server           # one sample now (stored, alerts evaluated)
gisstack monitor watch   --mode ssh --ssh root@server --interval 30     # continuous; run it as a service
gisstack monitor show | alerts | history NAME --since 6h [--step 300] [--label service=caddy] | prometheus | rules | prune
```

Run `watch` permanently, for example as a systemd unit on the machine that has the SSH access:

```ini
[Service]
Environment=GISSTACK_FEATURES=monitoring GISSTACK_HOME=/var/lib/gisstack-installer
ExecStart=/usr/bin/gisstack monitor watch -c /etc/gisstack/stack.json --mode ssh --ssh root@server --key /root/.ssh/id_ed25519 --interval 30
Restart=on-failure
```

## History

SQLite, one file per machine: `~/.gisstack/monitor/<project>-<host>.sqlite` (0600). Ring buffer: metrics older than 14 days
(`GISSTACK_MONITOR_RETENTION_DAYS`) and anything beyond 2 000 000 rows (`GISSTACK_MONITOR_MAX_ROWS`) are removed, oldest first; the
newest sample is never removed; the health/alert event history is kept 30 days longer.

## Alerts

A rule watches one metric; every series (one disk, one service, one probe) has its own state. It fires after `for` consecutive
samples beyond the threshold and resolves on the first good sample. A series missing from a sample is "no data": it neither fires
nor resolves anything. Defaults (`gisstack monitor rules`): CPU > 90 % for 5, memory > 90 % / 97 %, swap > 50 %, disk > 85 % / 95 %,
service down (2), health check failing (3) or slow (> 5 s), PostgreSQL down / connections > 80 % / query > 5 min / waiting locks,
newest backup older than 36 h, machine unreachable (3), metric sources unavailable (3).

**One outage, one page.** A failing health check is *explained* by its component's service being down (every `:public` check goes
through Caddy); PostgreSQL symptoms by the PostgreSQL service being down. Explained alerts are recorded as `alert.suppressed`, not
sent, and resolve silently; if the cause is fixed but a symptom persists, that symptom is then reported.

Override in `~/.gisstack/monitor/rules.json` (strictly validated - unknown rules/keys/values are errors):

```json
{"rules": {"cpu_high": {"threshold": 80, "for": 3}, "swap_high": {"enabled": false}, "backup_old": {"threshold": 100000, "level": "crit", "renotify": 3600}}}
```

### Alert rules manager (dashboard "Alert rules", API `/api/v1/monitor/rules`)

Built-in rules always exist: tune (`threshold`, `for`, `level`, `renotify`), disable, or reset them - they cannot be deleted. **Custom rules**
can be created, edited and deleted. A custom rule needs `id` (`a-z0-9_`, 3-41 characters, not a built-in id), `metric` (one of
`alerts.KNOWN_METRICS` - metrics the collector really produces), `op`, `threshold`, `for` (1-1000 samples), `level` (`warn`/`crit`) and
`text` (a template using only `{value}` and `{subject}`, e.g. `{value:.0f}`; attribute access and conversions are rejected); optional
`description`, `enabled`, `renotify` (seconds, 0 = never repeat, at most 30 days). Notification *channels* (webhook / e-mail) stay in
`notify.json` and are never returned by the API.

* Reading needs `read`; create / update / reset / delete need `admin` and are written to the audit log (`monitor.rule.create|update|reset|delete`,
  rule id, changed field names, revision, success or error code - never values of notifier secrets).
* Every change carries the `revision` returned by `GET` (a hash of the file). A stale revision is `409 conflict`; changes are made under an
  exclusive file lock and replace the file atomically (0600), so two administrators cannot overwrite each other and a crash cannot leave a half-written file.
* The result is validated by the same strict loader the monitor uses before anything is written; a damaged `rules.json` is reported and never overwritten.
* Values equal to a built-in default are not stored. The file keeps the original `{"rules": {...}}` shape and adds `"custom": {...}`; files written before this feature load unchanged.
* A running `gisstack monitor watch` reads the rules at start: restart it after a change.

## Notifications

`~/.gisstack/monitor/notify.json`:

```json
{"webhooks": [{"url": "https://chat.example.ge/hooks/XXXX"}],
 "email": {"host": "smtp.example.ge", "port": 587, "from": "gis@example.ge", "to": ["ops@example.ge"], "user": "gis"}}
```

* Webhook: `POST {"text": "...", "alert": {...}}` (works with Slack/Mattermost/Rocket.Chat style receivers). https is required (http only
  for localhost, or `"allow_insecure_webhooks": true`); redirects are never followed; failures are retried with back-off; the URL (a
  credential) never appears in logs or events.
* E-mail: STARTTLS by default for remote hosts. The password is **not** accepted in the file: set `GISSTACK_SMTP_PASSWORD` or
  `GISSTACK_SMTP_PASSWORD_FILE` (a file readable only by you).
* A failing receiver becomes a `notify.failed` event; it never stops monitoring.

### Delivery guarantees (verified against a real HTTP receiver)

A notification that could not be delivered is **not lost**: it is kept in an outbox next to the history (`~/.gisstack/monitor/<project>-<host>.outbox.json`, mode 0600, alert texts only - never a secret) and retried by the following monitoring rounds.

| Behaviour | Value |
|---|---|
| in-call retries | webhook: 2 retries with 0.5 s / 1 s back-off (HTTP 4xx other than 429 is not retried in-call) |
| outbox retry back-off | 60 s, doubling, at most 30 min |
| order | per receiver, strictly in the original order (a `resolved` never overtakes its `firing`); new notices queue behind older undelivered ones |
| duplicates | none: an entry leaves the outbox only after a successful delivery; no notice is re-sent once delivered |
| restart of the monitor | the outbox survives it |
| give up | after 24 h the notice is dropped with a warning event `notify.dropped` (a day-old "firing" is no longer news) |
| events | `notify.failed` (each failed attempt), `notify.retried` (delivered late, with how long it waited), `notify.dropped` |
| slow receiver | bounded by the per-request timeout (10 s default); it cannot stall the loop |

Disk capacity: the `disk_high` (85 %) / `disk_critical` (95 %) rules were verified on a real machine (a systemd container whose `/var/lib/gisstack` is a 24 MB tmpfs): the collector reported the mount, the alert fired once, reached the receiver, produced no duplicate while the disk stayed full, and resolved when the space was freed.

## Web API (needs the flag and the usual authentication)

Envelope `{data, error, meta}`; errors carry a `code`. `POST /api/v1/monitor/collect` (body: config, mode, ssh),
`GET /api/v1/metrics?project=&host=`, `GET /api/v1/metrics/history?project=&host=&name=&since=<seconds back>&step=&labels={"k":"v"}`,
`GET /api/v1/alerts?project=&host=`, and with `prometheus` also `GET /metrics?project=&host=` (Prometheus text, counters/gauges).
Reads never create a history. The graphical dashboard that shows all this is Phase 3.
