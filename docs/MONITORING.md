# Monitoring and alerts

Feature flag: `export GISSTACK_FEATURES=monitoring` (add `prometheus` for the `/metrics` text export). Without the flag the commands
and endpoints answer "switched off". Agentless: nothing is installed on the monitored machine - every sample is one read-only script
run here or over SSH (about 4 s), exactly like `gisstack status`.

## What is measured (all from real sources)

| Area | Source | Metrics (examples) |
|---|---|---|
| Host | `/proc/loadavg`, `/proc/meminfo`, `/proc/stat` (two readings 1 s apart), `/proc/uptime`, `df` | `host_cpu_percent`, `host_mem_used_percent`, `host_load1/5/15`, `disk_used_percent{mount}` |
| Services (native) | systemd cgroup members' `/proc/<pid>/stat` + `status`, `systemctl show` | `service_up`, `service_cpu_percent` (of one core), `service_rss_bytes`, `service_restarts` |
| Services (Compose) | `docker stats --no-stream`, compose state | same names |
| PostgreSQL | `pg_stat_activity`, `pg_stat_database`, `pg_settings`, `pg_locks` | `pg_up`, `pg_connections_percent`, `pg_database_size_bytes{database}`, `pg_cache_hit_ratio`, `pg_longest_query_seconds`, `pg_waiting_locks` |
| Health checks | `curl` timings / TCP connect against the public and local endpoints | `probe_up{probe}`, `probe_seconds{probe}` |
| Backups | `gisstack-backup list` | `backup_count`, `backup_last_age_seconds` |
| The monitor itself | | `monitor_reachable`, `collect_seconds`, `collect_errors` |

Kubernetes: service state and restarts only (no host: the host is the cluster). **If a source is unavailable the metric is absent and
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

## Web API (needs the flag and the usual authentication)

Envelope `{data, error, meta}`; errors carry a `code`. `POST /api/v1/monitor/collect` (body: config, mode, ssh),
`GET /api/v1/metrics?project=&host=`, `GET /api/v1/metrics/history?project=&host=&name=&since=<seconds back>&step=&labels={"k":"v"}`,
`GET /api/v1/alerts?project=&host=`, and with `prometheus` also `GET /metrics?project=&host=` (Prometheus text, counters/gauges).
Reads never create a history. The graphical dashboard that shows all this is Phase 3.
