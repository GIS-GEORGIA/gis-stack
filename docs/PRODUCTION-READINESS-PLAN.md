# Production readiness - prioritized plan

Written 2026-11-10 from the code and `docs/IMPLEMENTATION-STATUS.md`. Nothing here is assumed production-ready without evidence; each item has the evidence required before it may be called COMPLETE. Status per item is kept in `docs/IMPLEMENTATION-STATUS.md` (section 00000), not here.

| # | Stage | Why this priority | Evidence required for COMPLETE |
|---|---|---|---|
| 1 | **SSH: no silent trust-on-first-use by default**; explicit trust command; changed-key diagnostics | the weakest remaining trust decision: a first connection to an unknown (possibly spoofed) host was accepted silently and the host then receives root commands and secrets | real `sshd` tests for unknown / changed / trusted keys; CLI trust flow tested; existing flows still work with the documented opt-out |
| 2 | **Podman reliability**: compose-provider diagnostics (no silent switching), Podman rollback, Podman monitoring script | three items marked NOT VERIFIED; the provider problem already cost two red CI runs | real Podman tests (rootful and rootless); clear failure text asserted by a test |
| 3 | **GIS backup coverage**: Caddy, QGIS Server, MapProxy (config + cache) | the only components still implemented-but-never-run on a real engine | real engine backup **and** restore for each, data-level assertions; components that cannot be restored in place stay explicitly PARTIAL |
| 4 | **Legacy API**: replacement for `/api/logout`, caller inventory, migration text | `/api/logout` is the only legacy route without a v1 successor | tests for the new route; document unchanged for routes still used by the classic page |
| 5 | **Classic page CSP** (`unsafe-inline`) | needs the classic page's script/styles moved out and every flow re-tested in Chromium | zero CSP violations in Chromium across the page's flows; otherwise documented as an exception |
| 6 | **Monitoring/alerting verification** (backup failure, disk space, database health, Podman) | alerts exist but were verified only with fixtures | real collection on real engines; alert rules lifecycle already browser-tested |
| 7 | **Packaged service**: upgrade path, logging, health endpoint | the unprivileged service is verified for fresh installs on 5 distributions | upgrade test from the previous package |
| 8 | **Full real-environment run** of every suite, docs, final report | closes the loop | exact counts from this machine and from GitHub |

Order of work = order above. Stages that cannot be finished with real evidence in the available time are reported as PARTIAL / NOT VERIFIED, never as COMPLETE.

## Follow-up audit (2026-11-10) - outcome

Stages 1-8 above were executed (results: `docs/IMPLEMENTATION-STATUS.md`, sections 00000 - 000000). The follow-up audit of `b4213fb` (section 0000000, findings in `docs/SECURITY-REVIEW-2026-11.md`) closed the "never verified" list: rootless Podman lifecycle (verified, with the linger / `podman-restart.service` requirement), packaged-service upgrade (deb), alert delivery and disk alerts, Caddy ACME backup/restore (Pebble), `--mode ssh`. It also found and fixed two high-impact defects (lost alert notifications; Podman start/stop/restart under podman-compose). Still open before an externally exposed deployment: S-1 (token after upgrade), S-2 (services published on all interfaces without Caddy), S-3 (custom `HTTPS_PORT`), S-4 (container hardening), S-5, S-7.

## Hardening phase (2026-11-13) - outcome

Closed (locally verified, not yet on GitHub): S-1 token rotation on upgrade (deb), S-2 network exposure (`network.bind`), S-3 Caddy ports, S-5 snapshot secrets, S-7 cookie `Secure`, S-16 restore under dropped capabilities, and two real-MapProxy GIS defects (tile-source layers in non-Web-Mercator grids; GLOBAL_GEODETIC over WMTS). S-4 container hardening is **partial** (capabilities, privilege gain, seccomp; no read-only root filesystems, no resource limits). Open: S-12 (Pillow on Python 3.10), rpm upgrade untested, new tests not yet in CI. Release decision: not before the working tree is committed and `tests`, `codeql`, `podman` and `integration` are green on it - see `docs/IMPLEMENTATION-STATUS.md`, section 000000000000.
