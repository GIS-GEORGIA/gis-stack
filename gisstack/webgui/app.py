"""Web installer GUI (Flask).

Security model: the app can run commands as the user it runs as (root, when it
installs a server), so it
  * binds to 127.0.0.1 by default (reach it through `ssh -L 8765:127.0.0.1:8765 server`),
  * requires a secret token (?token= once, or Bearer) and exchanges it for a random, expiring, revocable session cookie,
  * only accepts state-changing requests that carry the custom X-GisStack header
    with a SameSite=Strict cookie (blocks cross-site requests),
  * accepts only the Host names the operator allowed (blocks DNS rebinding), limits request size and failed logins,
  * writes an audit log of every state-changing request (~/.gisstack/audit.log, no secrets).
"""
from __future__ import annotations

import contextlib
import hmac
import io
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, make_response, redirect, render_template, request, send_file

from . import security
from ..core import paths
from ..api import mount as api_mount
from ..api.auth import Principal, TokenStore
from ..api.registry import SCOPES, ApiError, registry as api_registry
from ..core import features
from .. import __version__, plugins, backup as backup_mod, catalog, config, deploy as deploy_mod, discover as discover_mod, existing as existing_mod, fsutil, generate, ops as ops_mod, preflight as preflight_mod, projects as projects_mod, services, updater as updater_mod, settings as settings_mod, i18n as i18n_mod

MASK = "••••••••"
MAX_PREVIEW_BYTES = 120_000


def load_or_create_token(path: Path | None = None) -> str:
    env = os.environ.get("GISSTACK_TOKEN")
    if env:
        return env
    path = path or paths.token_file()
    if path.exists():
        return path.read_text().strip()
    tok = secrets.token_urlsafe(24)
    fsutil.write_private(path, tok)
    return tok


def rotate_token(path: Path | None = None) -> str:
    """Replace the stored access token with a new one (all existing browser sessions become invalid on restart)."""
    path = path or paths.token_file()
    tok = secrets.token_urlsafe(24)
    fsutil.write_private(path, tok)
    return tok


from ..api.jobs import JOB_TTL, MAX_JOB_LINES, MAX_JOBS, Job  # noqa: E402,F401 - re-exported: tests and plugins use these names


def create_app(token: str | None = None, allowed_hosts: tuple[str, ...] | None = None) -> Flask:
    app = Flask(__name__)
    app.config["TOKEN"] = token or load_or_create_token()
    app.config["JOBS"] = {}
    app.config["DEPLOY_LOCK"] = threading.Lock()
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024           # the largest legitimate body is a config + plan
    app.config["SESSIONS"] = sessions = security.Sessions()
    app.config["LIMITER"] = limiter = security.RateLimiter()
    app.config["AUDIT"] = audit = security.AuditLog(paths.audit_log)
    hosts = tuple(allowed_hosts) if allowed_hosts is not None else security.allowed_hosts_from_env()
    app.config["ALLOWED_HOSTS"] = hosts
    app.config["TOKENS"] = TokenStore()
    COOKIE = "gs_session"

    def client_ip() -> str:
        return request.remote_addr or "?"

    def token_ok(cand: str) -> bool:
        return bool(cand) and hmac.compare_digest(cand, app.config["TOKEN"])

    def principal_of() -> Principal | None:
        """Who is calling: the web access token and a browser session are full administrators; API tokens carry a scope and only work
        on /api/v1 (they are never accepted by the legacy /api routes)."""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            cand = auth[7:]
            if token_ok(cand):
                return Principal("static", "web access token", "admin", "static")
            if cand.startswith("gsk_") and request.path.startswith("/api/v1/"):
                mine = app.config["TOKENS"].verify(cand)
                if mine:
                    return mine
            if request.path.startswith("/api/v1/") and len(cand) <= 4096:
                for plug in plugins.get("auth_provider"):                    # SSO / gateway tokens; a faulty provider simply does not recognise it
                    who = plugins.safe_call(plug, "authenticate", cand)
                    if isinstance(who, dict) and isinstance(who.get("name"), str) and who.get("scope") in SCOPES:
                        return Principal(f"plugin:{plug.name}", who["name"], who["scope"], "plugin")
            return None
        if sessions.valid(request.cookies.get(COOKIE, "")):
            return Principal("session", "browser session", "admin", "session")
        return None

    def deny(status: int, text: str, why: str) -> Response:
        audit.write(event="denied", why=why, ip=client_ip(), method=request.method, path=request.path, status=status)
        return Response(text, status)

    @app.before_request
    def guard():
        if not security.host_allowed(request.host, app.config["ALLOWED_HOSTS"]):
            return deny(400, "400 – unexpected Host header. Open the URL printed by `gisstack web`, or allow the name with --allow-host.\n", "host")
        if request.path == "/healthz":
            return None
        ip = client_ip()
        if limiter.blocked(ip):
            resp = deny(429, "429 – too many failed attempts; wait a minute.\n", "rate-limited")
            resp.headers["Retry-After"] = str(security.FAILURE_WINDOW)
            return resp
        qt = request.args.get("token")
        if qt:
            if token_ok(qt):
                limiter.reset(ip)
                resp = make_response(redirect(request.path))
                resp.set_cookie(COOKIE, sessions.create(), httponly=True, samesite="Strict", max_age=sessions.absolute)
                audit.write(event="login", ip=ip)
                return resp
            limiter.fail(ip)
            return deny(401, "401 – invalid token.\n", "bad-token")
        principal = principal_of()
        if principal is None:
            limiter.fail(ip)
            if request.path.startswith("/api/v1/"):
                audit.write(event="denied", why="no-credentials", ip=ip, method=request.method, path=request.path, status=401)
                return api_mount.envelope(error=ApiError(401, "unauthorized", "authentication required (Bearer token or browser session)"), status=401)
            return deny(401, "401 – token required. Open the URL printed by `gisstack web`.\n", "no-session")
        request.principal = principal
        # CSRF protection exists for credentials the browser attaches by itself (the cookie); a Bearer header is never ambient
        if principal.kind == "session" and request.method not in ("GET", "HEAD") and request.headers.get("X-GisStack") != "1":
            return deny(403, "403\n", "csrf-header")
        return None

    @app.after_request
    def record(resp: Response) -> Response:
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.path != "/healthz" and resp.status_code != 429:
            sid = request.cookies.get(COOKIE, "")
            try:
                body = request.get_json(silent=True)
            except Exception:  # noqa: BLE001 - e.g. 413: the body was refused, there is nothing to summarise
                body = None
            who = getattr(request, "principal", None)
            audit.write(event="request", ip=client_ip(), method=request.method, path=request.path, status=resp.status_code,
                        session=(sid[:6] if sid else "bearer"), **({"principal": who.id, "scope": who.scope, "kind": who.kind} if who else {}),
                        **security.summarize(body))
        return resp

    @app.post("/api/logout")
    def api_logout() -> Response:
        sessions.revoke(request.cookies.get(COOKIE, ""))
        resp = make_response(jsonify({"ok": True}))
        resp.delete_cookie(COOKIE)
        return resp

    @app.after_request
    def headers(resp: Response) -> Response:
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cache-Control"] = "no-store"
        if request.path == "/dashboard" or request.path.startswith("/static/dashboard/"):
            # the dashboard has no inline script or style at all, so nothing needs 'unsafe-inline' (audit S-07)
            resp.headers["Content-Security-Policy"] = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                                       "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        else:
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.get("/healthz")
    def healthz() -> Response:
        return Response("ok\n")

    @app.get("/")
    def index() -> str:
        return render_template("index.html", version=__version__)

    @app.get("/api/catalog")
    def api_catalog() -> Response:
        d = config.default_config()
        for sec, key in (("postgres", "password"), ("geoserver", "admin_password"), ("tomcat", "manager_password")):
            d[sec][key] = ""
        return jsonify({"catalog": catalog.ui_catalog(), "defaults": d,
                        "is_root": hasattr(os, "geteuid") and os.geteuid() == 0})

    def _resolve_or_400(payload: Any) -> dict[str, Any]:
        try:
            return config.resolve(payload if isinstance(payload, dict) else {}, persist_secrets=True)
        except config.ConfigError as e:
            abort(make_response(jsonify({"error": str(e)}), 400))

    @app.post("/api/preview")
    def api_preview() -> Response:
        cfg = _resolve_or_400(request.get_json(silent=True))
        secrets_ = [cfg["postgres"]["password"], cfg["geoserver"]["admin_password"],
                    cfg["tomcat"]["manager_password"]]
        files: dict[str, str] = {}
        with tempfile.TemporaryDirectory() as td:
            out = generate.generate_bundle(cfg, Path(td) / "bundle")
            for p in sorted(out.rglob("*")):
                if not p.is_file() or "vendor" in p.parts or p.suffix in (".png", ".tgz"):
                    continue
                if p.stat().st_size > MAX_PREVIEW_BYTES:
                    continue
                txt = p.read_text(encoding="utf-8", errors="replace")
                for s in secrets_:
                    if s:
                        txt = txt.replace(s, MASK)
                files[p.relative_to(out).as_posix()] = txt
        return jsonify({"files": files, "warnings": cfg["_meta"]["warnings"]})

    @app.post("/api/bundle")
    def api_bundle() -> Response:
        cfg = _resolve_or_400(request.get_json(silent=True))
        with tempfile.TemporaryDirectory() as td:
            out = generate.generate_bundle(cfg, Path(td) / f"gisstack-{cfg['project']}")
            data = generate.bundle_zip_bytes(out)
        return send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True,
                         download_name=f"gisstack-{cfg['project']}.zip")

    def _register(job: Job) -> None:
        """Keep the job registry bounded: drop finished jobs after an hour and the oldest ones beyond MAX_JOBS."""
        jobs: dict[str, Job] = app.config["JOBS"]
        now = time.time()
        for jid in [k for k, j in jobs.items() if j.done and now - j.started > JOB_TTL]:
            del jobs[jid]
        while len(jobs) >= MAX_JOBS:
            oldest = next((k for k, j in jobs.items() if j.done), None)
            if oldest is None:
                break
            del jobs[oldest]
        jobs[job.id] = job

    def _start_job(cfg: dict[str, Any], steps: Any, cleanup: bool = False) -> Response:
        """Run steps(bundle_dir) -> Iterator[str] in a background job (one at a time)."""
        lock: threading.Lock = app.config["DEPLOY_LOCK"]
        if not lock.acquire(blocking=False):
            return make_response(jsonify({"error": "სხვა ოპერაცია უკვე მიმდინარეობს"}), 409)

        job = Job()
        _register(job)
        bundle_dir = paths.bundles_dir() / f"{cfg['project']}-{job.id}"

        def run() -> None:
            try:
                job.add("== bundle-ის გენერაცია")
                generate.generate_bundle(cfg, bundle_dir)
                os.chmod(bundle_dir, 0o700)
                job.add(f"bundle: {bundle_dir}")
                for line in steps(bundle_dir):
                    job.add(line)
                job.ok = True
            except deploy_mod.DeployError as e:
                job.add(f"ERROR: {e}")
            except Exception as e:  # noqa: BLE001 - surface anything to the log pane
                job.add(f"ERROR: {type(e).__name__}: {e}")
            finally:
                if cleanup:                    # the server has its own copy; do not keep passwords in a local bundle for nothing
                    shutil.rmtree(bundle_dir, ignore_errors=True)
                job.done = True
                lock.release()

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"job": job.id})

    @app.post("/api/preflight")
    def api_preflight() -> Response:
        """Body: {config, mode, ssh}. Disk, memory, OS, network and clock checks for the selected stack."""
        cfg, mode, ssh, target = _ops_args(request.get_json(silent=True) or {})
        try:
            return jsonify(preflight_mod.run(cfg, mode, ssh, target))
        except deploy_mod.DeployError as e:
            return make_response(jsonify({"error": str(e)}), 502)

    @app.post("/api/deploy")
    def api_deploy() -> Response:
        body = request.get_json(silent=True) or {}
        cfg = _resolve_or_400(body.get("config"))
        mode = body.get("mode")
        target = cfg["deploy_target"]
        if mode not in ("local", "ssh"):
            return make_response(jsonify({"error": "mode must be local or ssh"}), 400)
        ssh = body.get("ssh") or {}
        problems = deploy_mod.preflight(target, mode)
        if problems:
            return make_response(jsonify({"error": "; ".join(problems)}), 400)
        try:
            services.ensure_ready(cfg, mode, ssh, target, skip_preflight=bool(body.get("skip_preflight")))
        except services.PreflightFailed as e:
            return make_response(jsonify({"error": str(e), "preflight": e.result, "code": e.code}), 409)
        except deploy_mod.DeployError as e:
            return make_response(jsonify({"error": str(e), "code": e.code}), 502)
        rep = services.find_conflicts(cfg, mode, ssh, target)      # somebody else's program on a port we need: show it, do not start
        if rep:
            return make_response(jsonify({"error": "ზოგიერთი საჭირო პორტი დაკავებულია — აირჩიე რა ვუყოთ არსებულ პროგრამებს",
                                          "existing": rep, "code": "conflict"}), 409)
        return _start_job(cfg, lambda b: deploy_mod.deploy(cfg, b, target, mode, ssh), cleanup=(mode == "ssh"))

    @app.post("/api/clean")
    def api_clean() -> Response:
        """Remove the whole stack (components omitted/null) or just the listed components."""
        body = request.get_json(silent=True) or {}
        cfg = _resolve_or_400(body.get("config"))
        mode = body.get("mode")
        target = cfg["deploy_target"]
        if mode not in ("local", "ssh"):
            return make_response(jsonify({"error": "mode must be local or ssh"}), 400)
        comps = body.get("components")
        if comps is not None and (not isinstance(comps, list) or not comps
                                  or any(c not in deploy_mod.CLEAN_COMPONENTS for c in comps)):
            return make_response(jsonify({"error": "components must be a non-empty list of known component ids"}), 400)
        purge = bool(body.get("purge"))
        rm_pg = bool(body.get("remove_postgres"))
        ssh = body.get("ssh") or {}
        try:
            deploy_mod.clean_args(target, comps, purge, rm_pg)
        except deploy_mod.DeployError as e:
            return make_response(jsonify({"error": str(e)}), 400)
        problems = deploy_mod.preflight(target, mode)
        if problems:
            return make_response(jsonify({"error": "; ".join(problems)}), 400)
        return _start_job(cfg, lambda b: deploy_mod.clean(cfg, b, target, mode, ssh, comps, purge, rm_pg), cleanup=(mode == "ssh"))

    def _ops_args(body: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any], str]:
        cfg = _resolve_or_400(body.get("config"))
        mode = body.get("mode")
        if mode not in ("local", "ssh"):
            abort(make_response(jsonify({"error": "mode must be local or ssh"}), 400))
        return cfg, mode, body.get("ssh") or {}, cfg["deploy_target"]

    def _plain_job(steps: Any, locked: bool) -> Response:
        """Run steps(job) -> Iterator[str] in the background; `locked` serialises it with deploy/clean."""
        lock: threading.Lock = app.config["DEPLOY_LOCK"]
        if locked and not lock.acquire(blocking=False):
            return make_response(jsonify({"error": "სხვა ოპერაცია უკვე მიმდინარეობს"}), 409)
        job = Job()
        _register(job)

        def run() -> None:
            try:
                for line in steps(job):
                    job.add(line)
                job.ok = True
            except (deploy_mod.DeployError, updater_mod.UpdateError) as e:
                job.add(f"ERROR: {e}")
            except Exception as e:  # noqa: BLE001
                job.add(f"ERROR: {type(e).__name__}: {e}")
            finally:
                job.done = True
                if locked:
                    lock.release()

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"job": job.id})

    @app.post("/api/existing/scan")
    def api_existing_scan() -> Response:
        """Body: {config, mode, ssh}. What is already installed on the server / which of our ports are taken."""
        cfg, mode, ssh, target = _ops_args(request.get_json(silent=True) or {})
        if target != "native":
            return make_response(jsonify({"error": "native სამიზნე სჭირდება"}), 400)
        try:
            return jsonify(discover_mod.scan(cfg, mode, ssh, target))
        except deploy_mod.DeployError as e:
            return make_response(jsonify({"error": str(e)}), 502)

    def _plan_arg(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return existing_mod.normalize_plan(body.get("plan"))
        except existing_mod.PlanError as e:
            abort(make_response(jsonify({"error": str(e)}), 400))

    @app.post("/api/existing/backup")
    def api_existing_backup() -> Response:
        """Body: {config, mode, ssh, components, with_data?}. Back up settings of installed programs; copy comes here."""
        body = request.get_json(silent=True) or {}
        cfg, mode, ssh, target = _ops_args(body)
        comps = body.get("components")
        if not isinstance(comps, list) or not comps or any(c not in discover_mod.COMPONENTS for c in comps):
            return make_response(jsonify({"error": "components must be a non-empty list of known program ids"}), 400)
        return _plain_job(lambda job: existing_mod.backup(cfg, target, mode, ssh, comps, with_data=bool(body.get("with_data"))),
                          locked=True)

    @app.post("/api/existing/execute")
    def api_existing_execute() -> Response:
        """Body: {config, mode, ssh, plan, with_data?, install?}.  Back up -> remove -> (install) -> import."""
        body = request.get_json(silent=True) or {}
        cfg, mode, ssh, target = _ops_args(body)
        plan = _plan_arg(body)
        install = body.get("install", True)
        if target != "native":
            return make_response(jsonify({"error": "native სამიზნე სჭირდება"}), 400)
        problems = deploy_mod.preflight(target, mode)
        if problems:
            return make_response(jsonify({"error": "; ".join(problems)}), 400)
        wd = bool(body.get("with_data"))
        confirm = body.get("confirm_data_loss") is True
        if any(v["purge"] and v["action"] in ("remove", "reuse") for v in plan.values()) and not confirm:
            return make_response(jsonify({"error": "მონაცემების წაშლა მოითხოვს ექსპლიციტურ დადასტურებას (confirm_data_loss)"}), 400)

        def steps(job: Job) -> Any:
            bundle_dir = paths.bundles_dir() / f"{cfg['project']}-{job.id}"

            def make(c: dict[str, Any]) -> Path:
                generate.generate_bundle(c, bundle_dir)
                os.chmod(bundle_dir, 0o700)
                return bundle_dir
            yield from existing_mod.run(cfg, target, mode, ssh, plan, make, with_data=wd, install=bool(install),
                                        confirm_data_loss=confirm)
        return _plain_job(steps, locked=True)

    def _backup_args(body: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
        cfg, mode, ssh, _ = _ops_args(body)
        if cfg["deploy_target"] != "native":
            abort(make_response(jsonify({"error": "native სამიზნე სჭირდება", "code": "deploy"}), 400))
        return cfg, mode, ssh

    def _backup_job(make: Any) -> Response:
        """Validate eagerly (400 with a code), then run the generator as a locked background job."""
        try:
            gen = make()
            first = next(gen, None)            # validation errors are raised before the first line
        except deploy_mod.DeployError as e:
            return make_response(jsonify({"error": str(e), "code": e.code}), 400)
        import itertools
        return _plain_job(lambda job: itertools.chain([first] if first is not None else [], gen), locked=True)

    @app.post("/api/backup/list")
    def api_backup_list() -> Response:
        cfg, mode, ssh = _backup_args(request.get_json(silent=True) or {})
        try:
            return jsonify({"backups": backup_mod.list_backups(cfg, mode, ssh)})
        except deploy_mod.DeployError as e:
            return make_response(jsonify({"error": str(e), "code": e.code}), 502)

    @app.post("/api/backup/create")
    def api_backup_create() -> Response:
        """Body: {config, mode, ssh, components?, with_cache?, note?}."""
        body = request.get_json(silent=True) or {}
        cfg, mode, ssh = _backup_args(body)
        comps = body.get("components")
        if comps is not None and not isinstance(comps, list):
            return make_response(jsonify({"error": "components must be a list", "code": "deploy"}), 400)
        return _backup_job(lambda: backup_mod.create(cfg, mode, ssh, comps, with_cache=bool(body.get("with_cache")), note=str(body.get("note") or "")))

    @app.post("/api/backup/verify")
    def api_backup_verify() -> Response:
        body = request.get_json(silent=True) or {}
        cfg, mode, ssh = _backup_args(body)
        return _backup_job(lambda: backup_mod.verify(cfg, mode, ssh, str(body.get("id", ""))))

    @app.post("/api/backup/prune")
    def api_backup_prune() -> Response:
        """Body: {config, mode, ssh, keep?, days?, keep_pre?, dry_run?}.  dry_run defaults to TRUE: deleting needs dry_run=false."""
        body = request.get_json(silent=True) or {}
        cfg, mode, ssh = _backup_args(body)
        try:
            keep, keep_pre = int(body.get("keep", 14)), int(body.get("keep_pre", 5))
            days = int(body["days"]) if body.get("days") is not None else None
        except (TypeError, ValueError):
            return make_response(jsonify({"error": "keep/days must be numbers", "code": "deploy"}), 400)
        return _backup_job(lambda: backup_mod.prune(cfg, mode, ssh, keep, days, keep_pre, dry_run=body.get("dry_run", True) is not False))

    @app.post("/api/backup/restore")
    def api_backup_restore() -> Response:
        """Body: {config, mode, ssh, id, only?, dry_run?, staging?, confirm?, pre_backup?, rollback?}.

        dry_run defaults to TRUE (a plan).  Replacing live data needs dry_run=false AND confirm=true."""
        body = request.get_json(silent=True) or {}
        cfg, mode, ssh = _backup_args(body)
        only = body.get("only")
        if only is not None and not isinstance(only, list):
            return make_response(jsonify({"error": "only must be a list", "code": "deploy"}), 400)
        dry = body.get("dry_run", True) is not False
        return _backup_job(lambda: backup_mod.restore(cfg, mode, ssh, str(body.get("id", "")), only=only, dry_run=dry, staging=bool(body.get("staging")),
                                                       confirm=body.get("confirm") is True, pre_backup=body.get("pre_backup", True) is not False,
                                                       rollback=body.get("rollback", True) is not False))

    # ------------------------------------------------------------------ pages and exports outside /api/v1
    @app.get("/dashboard")
    def dashboard_page() -> Response | str:
        if not features.enabled("dashboard"):
            return api_mount.envelope(error=ApiError(404, "feature_disabled", "the dashboard is switched off (GISSTACK_FEATURES=dashboard)"), status=404)
        return render_template("dashboard.html", version=__version__)

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        """Prometheus text for the newest sample (needs both flags: monitoring and prometheus; same authentication as everything else)."""
        for flag in ("monitoring", "prometheus"):
            if not features.enabled(flag):
                return api_mount.envelope(error=ApiError(404, "feature_disabled", f"switched off (GISSTACK_FEATURES={flag})"), status=404)
        import re as _re

        from ..core import paths as _paths
        from ..monitoring import service as _service
        from ..monitoring.store import Store, StoreError
        project, host = request.args.get("project", ""), request.args.get("host", "localhost")
        if not _re.fullmatch(r"[a-z][a-z0-9_]{0,30}", project) or not _re.fullmatch(r"[A-Za-z0-9._:\[\]-]{1,100}", host):
            return api_mount.envelope(error=ApiError(400, "bad_request", "project and host are required and must be plain names"), status=400)
        db = _paths.monitor_db(project, host)
        if not db.exists():
            return api_mount.envelope(error=ApiError(404, "not_found", "no monitoring history for this machine yet"), status=404)
        try:
            store = Store(db)
        except StoreError as e:
            return api_mount.envelope(error=ApiError(500, "store", str(e)), status=500)
        try:
            return Response(_service.prometheus_text(store, project), mimetype="text/plain; version=0.0.4")
        finally:
            store.close()

    @app.post("/api/ops/status")
    def api_ops_status() -> Response:
        """Body: {config, mode, ssh}. Live state of every program (blocking call, a few seconds at most)."""
        cfg, mode, ssh, target = _ops_args(request.get_json(silent=True) or {})
        try:
            return jsonify(ops_mod.status(cfg, mode, ssh, target))
        except deploy_mod.DeployError as e:
            return make_response(jsonify({"error": str(e)}), 502)

    @app.post("/api/ops/control")
    def api_ops_control() -> Response:
        """Body: {config, mode, ssh, action, components?}. components omitted/null = the whole stack."""
        body = request.get_json(silent=True) or {}
        cfg, mode, ssh, target = _ops_args(body)
        action, comps = body.get("action"), body.get("components")
        if action not in ops_mod.ACTIONS:
            return make_response(jsonify({"error": "unknown action"}), 400)
        if comps is not None and (not isinstance(comps, list) or not comps or any(c not in ops_mod.ORDER for c in comps)):
            return make_response(jsonify({"error": "components must be a non-empty list of known component ids"}), 400)
        if mode == "local" and target == "native" and hasattr(os, "geteuid") and os.geteuid() != 0:
            problems = deploy_mod.preflight("native", "local")
            return make_response(jsonify({"error": "; ".join(problems)}), 400)
        return _plain_job(lambda job: ops_mod.control(cfg, mode, ssh, action, comps, target), locked=True)

    @app.post("/api/ops/logs")
    def api_ops_logs() -> Response:
        """Body: {config, mode, ssh, component, lines?, follow?}. Stream with /api/jobs/<id>; stop with /cancel."""
        body = request.get_json(silent=True) or {}
        cfg, mode, ssh, target = _ops_args(body)
        comp = body.get("component")
        if comp not in ops_mod.ORDER:
            return make_response(jsonify({"error": "unknown component"}), 400)
        try:
            lines = int(body.get("lines") or 100)
        except (TypeError, ValueError):
            return make_response(jsonify({"error": "lines must be a number"}), 400)
        follow = bool(body.get("follow"))
        return _plain_job(lambda job: ops_mod.logs(cfg, mode, ssh, comp, lines, follow, job.stop, target), locked=False)

    @app.post("/api/jobs/<jid>/cancel")
    def api_job_cancel(jid: str) -> Response:
        job: Job | None = app.config["JOBS"].get(jid)
        if not job:
            abort(404)
        job.stop.set()
        return jsonify({"ok": True})

    @app.get("/api/i18n")
    def api_i18n() -> Response:
        """Georgian -> English table for the page (the page translates text it shows)."""
        return jsonify({"en": i18n_mod.EN})

    @app.get("/api/settings")
    def api_settings_get() -> Response:
        return jsonify(settings_mod.view())

    @app.post("/api/settings")
    def api_settings_set() -> Response:
        body = request.get_json(silent=True) or {}
        try:
            settings_mod.save(body.get("lang"), body.get("theme"))
        except ValueError as e:
            return make_response(jsonify({"error": str(e)}), 400)
        return jsonify(settings_mod.view())

    @app.post("/api/update/check")
    def api_update_check() -> Response:
        """Ask GitHub whether a newer release exists (only when the user clicks)."""
        try:
            info = updater_mod.check()
        except updater_mod.UpdateError as e:
            return make_response(jsonify({"error": str(e)}), 502)
        return jsonify(info)

    @app.post("/api/update/apply")
    def api_update_apply() -> Response:
        """Download, verify (SHA-256) and install the new release into this virtualenv."""
        try:
            info = updater_mod.check()
        except updater_mod.UpdateError as e:
            return make_response(jsonify({"error": str(e)}), 502)
        if not info["newer"] or not info["can_auto"]:
            return make_response(jsonify({"error": "automatic update is not available here"}), 400)

        def steps(job: Job) -> Any:
            with tempfile.TemporaryDirectory() as td:
                yield from updater_mod.apply(info, td)
        try:
            return _plain_job(steps, locked=True)
        except updater_mod.UpdateError as e:
            return make_response(jsonify({"error": str(e)}), 400)

    @app.get("/api/projects")
    def api_projects() -> Response:
        return jsonify({"projects": projects_mod.list_projects()})

    @app.get("/api/projects/<name>")
    def api_project(name: str) -> Response:
        try:
            return jsonify({"config": projects_mod.load_full(name)})
        except projects_mod.ProjectError as e:
            return make_response(jsonify({"error": str(e)}), 404)

    def _save(body: dict[str, Any]) -> str:
        """Validate and store the whole project, or just one program's part of it. Returns the file path."""
        cfg = _resolve_or_400(body.get("config"))     # validation (and 400 with the reason)
        given = body.get("config")
        raw = dict(given if isinstance(given, dict) else {}, project=cfg["project"])
        comp = body.get("component")
        try:
            if comp is None:
                return str(projects_mod.save(raw))
            return str(projects_mod.save_component(raw, comp))
        except projects_mod.ProjectError as e:
            abort(make_response(jsonify({"error": str(e)}), 400))

    @app.post("/api/save")
    def api_save() -> Response:
        """Body: {config, component?}. Without component the whole project is saved."""
        body = request.get_json(silent=True) or {}
        return jsonify({"saved": _save(body)})

    @app.post("/api/apply")
    def api_apply() -> Response:
        """Body: {config, component, mode, ssh, save?}: roll out just one program (optionally saving it first)."""
        body = request.get_json(silent=True) or {}
        cfg = _resolve_or_400(body.get("config"))
        mode, comp, target = body.get("mode"), body.get("component"), cfg["deploy_target"]
        if mode not in ("local", "ssh"):
            return make_response(jsonify({"error": "mode must be local or ssh"}), 400)
        if not isinstance(comp, str):
            return make_response(jsonify({"error": "component is required"}), 400)
        try:
            deploy_mod.apply_args(target, comp)
        except deploy_mod.DeployError as e:
            return make_response(jsonify({"error": str(e)}), 400)
        problems = deploy_mod.preflight(target, mode)
        if problems:
            return make_response(jsonify({"error": "; ".join(problems)}), 400)
        if body.get("save"):
            _save(body)
        ssh = body.get("ssh") or {}
        return _start_job(cfg, lambda b: deploy_mod.apply_component(cfg, b, target, mode, ssh, comp), cleanup=(mode == "ssh"))

    @app.get("/api/jobs/<jid>")
    def api_job(jid: str) -> Response:
        job: Job | None = app.config["JOBS"].get(jid)
        if not job:
            abort(404)
        since = max(0, int(request.args.get("since", 0)))
        with job.lock:
            lines = job.lines[since:]
        return jsonify({"lines": lines, "next": since + len(lines), "done": job.done, "ok": job.ok})

    # ---------------------------------------------------------------- /api/v1 and the deprecation of the old /api routes
    from ..api import routes as _routes, routes_monitor as _routes_monitor  # noqa: F401 - importing registers the routes
    api_mount.mount(app, api_registry, __version__)
    legacy = {}
    for r in api_registry.routes:
        for old in r.legacy:
            legacy[old] = r.full_path

    @app.after_request
    def deprecation(resp: Response) -> Response:
        """The pre-1.0 /api/* routes keep working for one more release; they say so and point at their replacement."""
        rule = request.url_rule.rule if request.url_rule else ""
        if rule in legacy:
            resp.headers["Deprecation"] = "true"
            resp.headers["Link"] = f'<{legacy[rule]}>; rel="successor-version"'
        return resp

    return app


def main(host: str = "127.0.0.1", port: int = 8765, server: str = "werkzeug", allow_hosts: tuple[str, ...] = (),
         rotate: bool = False) -> None:
    with contextlib.suppress(AttributeError, ValueError):
        getattr(sys.stdout, "reconfigure")(line_buffering=True)          # the URL must reach the journal / a pipe at once  # noqa: B009
    token = rotate_token() if rotate else load_or_create_token()
    extra = tuple(allow_hosts) + security.allowed_hosts_from_env()
    if host not in ("127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0", "::") and host not in extra:  # noqa: S104  # nosec B104 - comparison, not a bind
        extra += (host,)                           # binding to a specific name implies using it in the Host header
    app = create_app(token, extra)
    print(f"gisstack web installer {__version__}")
    if rotate:
        print("  access token rotated: all earlier links and sessions are now invalid")
    print(f"  open:  http://{host}:{port}/?token={token}")
    if host in ("127.0.0.1", "localhost"):
        print(f"  remote server? tunnel first:  ssh -L {port}:127.0.0.1:{port} user@server")
    else:
        print("  WARNING: listening beyond localhost over plain HTTP - put it behind TLS or use an SSH tunnel.")
        if not extra:
            print("  Host names other than localhost are rejected; allow yours with --allow-host NAME (or GISSTACK_ALLOWED_HOSTS).")
    if server == "waitress":
        try:
            import waitress
        except ImportError:
            raise SystemExit("waitress is not installed: pip install waitress  (or use --server werkzeug)") from None
        waitress.serve(app, host=host, port=port, threads=8, max_request_body_size=app.config["MAX_CONTENT_LENGTH"])
        return
    print("  (development server; for long-running use: gisstack web --server waitress)")
    app.run(host=host, port=port, threaded=True)
