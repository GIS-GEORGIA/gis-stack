"""Web installer GUI (Flask).

Security model: the app can run commands as the user it runs as (root, when it
installs a server), so it
  * binds to 127.0.0.1 by default (reach it through `ssh -L 8765:127.0.0.1:8765 server`),
  * requires a secret token (cookie / Bearer / ?token=) for every request,
  * only accepts state-changing requests that carry the custom X-GisStack header
    with a SameSite=Strict cookie (blocks cross-site requests).
"""
from __future__ import annotations

import hmac
import io
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, make_response, redirect, render_template, request, send_file

from .. import __version__, catalog, config, deploy as deploy_mod, discover as discover_mod, existing as existing_mod, generate, ops as ops_mod, projects as projects_mod, updater as updater_mod, settings as settings_mod, i18n as i18n_mod

MASK = "••••••••"
MAX_PREVIEW_BYTES = 120_000


def load_or_create_token(path: Path | None = None) -> str:
    env = os.environ.get("GISSTACK_TOKEN")
    if env:
        return env
    path = path or (deploy_mod.home_dir() / "web.token")
    if path.exists():
        return path.read_text().strip()
    tok = secrets.token_urlsafe(24)
    path.write_text(tok)
    path.chmod(0o600)
    return tok


class Job:
    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.lines: list[str] = []
        self.done = False
        self.ok = False
        self.started = time.time()
        self.lock = threading.Lock()
        self.stop = threading.Event()      # set by /cancel (log following)

    def add(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)


def create_app(token: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["TOKEN"] = token or load_or_create_token()
    app.config["JOBS"] = {}
    app.config["DEPLOY_LOCK"] = threading.Lock()

    def authed() -> bool:
        tok = app.config["TOKEN"]
        cand = request.cookies.get("gs_token") or ""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            cand = auth[7:]
        return bool(cand) and hmac.compare_digest(cand, tok)

    @app.before_request
    def guard():
        if request.path == "/healthz":
            return None
        qt = request.args.get("token")
        if qt and hmac.compare_digest(qt, app.config["TOKEN"]):
            resp = make_response(redirect(request.path))
            resp.set_cookie("gs_token", qt, httponly=True, samesite="Strict")
            return resp
        if not authed():
            return Response("401 – token required. Open the URL printed by `gisstack web`.\n", 401)
        if request.method not in ("GET", "HEAD") and request.headers.get("X-GisStack") != "1":
            abort(403)
        return None

    @app.after_request
    def headers(resp: Response) -> Response:
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
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

    def _start_job(cfg: dict[str, Any], steps: Any) -> Response:
        """Run steps(bundle_dir) -> Iterator[str] in a background job (one at a time)."""
        lock: threading.Lock = app.config["DEPLOY_LOCK"]
        if not lock.acquire(blocking=False):
            return make_response(jsonify({"error": "სხვა ოპერაცია უკვე მიმდინარეობს"}), 409)

        job = Job()
        app.config["JOBS"][job.id] = job
        bundle_dir = deploy_mod.home_dir() / "bundles" / f"{cfg['project']}-{job.id}"

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
                job.done = True
                lock.release()

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"job": job.id})

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
        if target == "native":
            # somebody else's program on a port we need: do not start, show what is there so the user can decide
            try:
                rep = discover_mod.scan(cfg, mode, ssh, target)
            except deploy_mod.DeployError:
                rep = None                       # cannot inspect (e.g. no sudo): the installer will report its own errors
            if rep and rep["conflicts"]:
                return make_response(jsonify({"error": "ზოგიერთი საჭირო პორტი დაკავებულია — აირჩიე რა ვუყოთ არსებულ პროგრამებს",
                                              "existing": rep}), 409)
        return _start_job(cfg, lambda b: deploy_mod.deploy(cfg, b, target, mode, ssh))

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
        return _start_job(cfg, lambda b: deploy_mod.clean(cfg, b, target, mode, ssh, comps, purge, rm_pg))

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
        app.config["JOBS"][job.id] = job

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
            return existing_mod._norm(body.get("plan"))
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

        def steps(job: Job) -> Any:
            bundle_dir = deploy_mod.home_dir() / "bundles" / f"{cfg['project']}-{job.id}"

            def make(c: dict[str, Any]) -> Path:
                generate.generate_bundle(c, bundle_dir)
                os.chmod(bundle_dir, 0o700)
                return bundle_dir
            yield from existing_mod.run(cfg, target, mode, ssh, plan, make, with_data=wd, install=bool(install))
        return _plain_job(steps, locked=True)

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
        raw = body.get("config") if isinstance(body.get("config"), dict) else {}
        raw = dict(raw, project=cfg["project"])
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
        return _start_job(cfg, lambda b: deploy_mod.apply_component(cfg, b, target, mode, ssh, comp))

    @app.get("/api/jobs/<jid>")
    def api_job(jid: str) -> Response:
        job: Job | None = app.config["JOBS"].get(jid)
        if not job:
            abort(404)
        since = max(0, int(request.args.get("since", 0)))
        with job.lock:
            lines = job.lines[since:]
        return jsonify({"lines": lines, "next": since + len(lines), "done": job.done, "ok": job.ok})

    return app


def main(host: str = "127.0.0.1", port: int = 8765) -> None:
    token = load_or_create_token()
    app = create_app(token)
    print(f"gisstack web installer {__version__}")
    print(f"  open:  http://{host}:{port}/?token={token}")
    if host in ("127.0.0.1", "localhost"):
        print(f"  remote server? tunnel first:  ssh -L {port}:127.0.0.1:{port} user@server")
    else:
        print("  WARNING: listening beyond localhost over plain HTTP - put it behind TLS or use an SSH tunnel.")
    app.run(host=host, port=port, threaded=True)
