# Web dashboard

Feature flag: `export GISSTACK_FEATURES=dashboard` (add `monitoring` for metrics, history and alerts; `all` switches everything on), then
`gisstack web` and open `/dashboard` (the link printed by `gisstack web` works with `/dashboard` in place of `/`). The classic installer
page at `/` stays exactly as it was - deploy, clean, projects and the existing-programs workflows are still done there (the dashboard
links to it), until the dashboard has taken over every one of them.

## Pages

| Page | What it shows / does |
|---|---|
| Overview | health banner, a card per service (state, address, checks, CPU/RAM), gauges (CPU, memory, disk, PostgreSQL connections, GeoServer availability, MapProxy cache size, newest backup), recent operations, firing alerts |
| Infrastructure | every program with state, autostart, PID, addresses and health checks; start / stop / restart one or all (stop needs confirmation); live log of the action |
| PostgreSQL/PostGIS, GeoServer, QGIS Server, MapProxy, Web maps | one page per program: state, configuration summary (never passwords), checks, controls, resource history |
| Operations | the operation journal (deployments, cleanups, backups, restores, rotations), filter by errors / running |
| Monitoring | history charts for the chosen range (1 h - 7 d), availability, alerts and events, "take a sample now" |
| Backups | list, create, verify, prune (preview first), restore wizard (plan -> typed `RESTORE` -> run, or "unpack next to live data") |
| Logs | a program's log: last N lines or live follow, filter, save as a file; "Stop" cancels the remote tail |
| Alert rules | all alert rules (built-in and custom) with metric, condition, severity, status, description, last modification time and revision; admins create, edit, enable/disable, reset and delete (with confirmation); read-only users only view |
| Security | owner-only permission checks of this computer's gis-stack data (tokens, server profiles, pinned host keys, generated passwords, audit log), the update-signature policy, and the newest audit-log events with a filter; needs an `admin` token; never shows secret values |
| Settings | language, theme, connection, feature flags |

**Connection.** The chip in the header chooses the project (saved configuration), deployment target and where it runs (this computer / SSH).
It is kept in the browser tab only (`sessionStorage`) - an SSH key *path* is never written to disk by the browser.

**Refresh.** Manual button, or automatic every 15 / 30 / 60 s (paused while the tab is hidden).

**Honesty.** A number is shown only if a real source produced it. Without monitoring history the gauges say so (and how to start it); a
failing source shows an error in its own card and never blanks the rest of the page.

## Security

* No inline script, no inline style, no event-handler attributes, no `innerHTML`/`eval` anywhere: the dashboard is served with
  `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; ...` (no `unsafe-inline`), `Referrer-Policy: no-referrer`.
  All text from servers is inserted as text nodes. Tests scan the shipped files for violations.
* Same authentication as the rest (session cookie), the CSRF header on every state-changing call, destructive actions behind
  explicit confirmation (typed word for restore / delete).
* It uses the existing endpoints plus `/api/v1/dashboard/{config,summary}` and `/api/v1/journal`.

## Accessibility and responsiveness

Skip link, landmarks (`header`, `nav`, `main`), one `h1` per page that receives focus after navigation, `aria-current` on the active
page, labelled controls, visible focus, state shown by icon *and* text (never colour alone), charts operable with the arrow keys and
available as tables, `prefers-reduced-motion`, light/dark themes with AA contrast tokens, layout down to phone width (the menu becomes a
drawer). Georgian is the source language; English comes from the same translation table as the other interfaces (`/api/i18n`).

## Development

Static files live in `gisstack/webgui/static/dashboard/` (plain ES modules, no build step). Pure logic is tested with
`node --test tests/js/pure.test.mjs` (also run by `python -m unittest tests.test_dashboard`).
New Georgian text needs an entry in `gisstack/i18n.py` (a test fails otherwise).
