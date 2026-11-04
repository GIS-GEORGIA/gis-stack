"""gisstack command line.

  gisstack web [--host 127.0.0.1] [--port 8765]    web installer GUI
  gisstack desktop                                  desktop installer GUI (needs python3-tk)
  gisstack defaults > stack.json                    print the default configuration
  gisstack generate -c stack.json -o ./bundle       render all deployment files
  gisstack deploy   -c stack.json --mode local|ssh [--ssh user@host[:port]] [--key FILE]
  gisstack save     -c stack.json [--component postgis]   store as a project / store one program's settings
  gisstack apply    -c stack.json postgis [--save]        (re)install only that program
  gisstack status   -c stack.json [--watch] [--json]      live state, IP:port, health of every program
  gisstack start|stop|restart|enable|disable -c stack.json [program...]
  gisstack logs     -c stack.json geoserver [-n 200] [-f]
  gisstack scan     -c stack.json                         what is already installed on the server?
  gisstack existing backup|remove tomcat caddy ...         back up / uninstall programs found by scan
  gisstack deploy   -c stack.json --existing postgis=adopt,caddy=reuse   decide per program
  gisstack settings [--lang auto|ka|en] [--theme auto|light|dark]   interface language and theme (both GUIs)
  gisstack update   [--check] [--download DIR] [-y]      newer release on GitHub? download / install it
  gisstack clean    -c stack.json [--only tomcat,caddy] [--purge] [--remove-postgres] [-y]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, config, deploy, deps, discover, existing, generate, i18n, ops, preflight, projects, rotation, settings, updater


MAX_CONFIG_BYTES = 1_000_000


def _load(path: str | None) -> dict:
    """Read a JSON config file; any problem is reported as a ConfigError (clean message, exit code 2), not a traceback."""
    if not path:
        return {}
    try:
        p = Path(path)
        if p.stat().st_size > MAX_CONFIG_BYTES:
            raise config.ConfigError(f"{path}: file is larger than {MAX_CONFIG_BYTES // 1000} kB")
        data = json.loads(p.read_text(encoding="utf-8"))
    except config.ConfigError:
        raise
    except (OSError, UnicodeDecodeError) as e:
        raise config.ConfigError(f"{path}: cannot be read ({e.__class__.__name__}: {e})") from e
    except ValueError as e:
        raise config.ConfigError(f"{path}: not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise config.ConfigError(f"{path}: the top level must be a JSON object")
    return data


def _parse_ssh(a: argparse.Namespace) -> dict | None:
    if a.mode != "ssh":
        return {}
    if not a.ssh:
        print("--ssh user@host is required with --mode ssh", file=sys.stderr)
        return None
    userhost, _, port = a.ssh.partition(":")
    user, _, host = userhost.rpartition("@")
    return {"user": user or "root", "host": host, "port": int(port or 22), "key": a.key or ""}


def _clean(a: argparse.Namespace, cfg: dict) -> int:
    target = a.target or cfg["deploy_target"]
    comps = [x.strip() for x in a.only.split(",") if x.strip()] if a.only else None
    ssh = _parse_ssh(a)
    if ssh is None:
        return 2
    what = ", ".join(comps) if comps else "THE WHOLE STACK"
    where = f"{ssh['user']}@{ssh['host']}" if a.mode == "ssh" else "this machine"
    if not a.yes:
        print(f"About to remove {what} ({target}) on {where}" + (" INCLUDING DATA" if a.purge else "") + ".")
        if input("Type 'yes' to continue: ").strip() != "yes":
            print("aborted")
            return 1
    out = deploy.home_dir() / "bundles" / f"{cfg['project']}-clean"
    generate.generate_bundle(cfg, out)
    try:
        for line in deploy.clean(cfg, out, target, a.mode, ssh, comps, a.purge, a.remove_postgres, a.force_postgres):
            print(line, flush=True)
    except deploy.DeployError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


def _ops(a: argparse.Namespace, cfg: dict) -> int:
    ssh = _parse_ssh(a)
    if ssh is None:
        return 2
    target = a.target or cfg["deploy_target"]
    comps = getattr(a, "components", None) or None
    try:
        if a.cmd == "status":
            tty = sys.stdout.isatty()
            while True:
                res = ops.status(cfg, a.mode, ssh, target, comps)
                if a.json:
                    print(json.dumps(res, ensure_ascii=False, indent=2))
                else:
                    if a.watch and tty:
                        print("\033[H\033[2J", end="")
                    print(ops.format_table(res, color=tty))
                if not a.watch:
                    return 0 if all(r["ok"] for r in res["rows"]) else 1
                try:
                    time.sleep(a.watch)
                except KeyboardInterrupt:
                    return 0
        if a.cmd == "logs":
            try:
                for line in ops.logs(cfg, a.mode, ssh, a.component, a.lines, a.follow, target=target):
                    print(line, flush=True)
            except KeyboardInterrupt:
                pass
            return 0
        for line in ops.control(cfg, a.mode, ssh, a.cmd, comps, target):
            print(line, flush=True)
        return 0
    except deploy.DeployError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def _update(a: argparse.Namespace) -> int:
    try:
        info = updater.check()
        print(f"installed: {info['current']}   latest: {info['latest']}"
              + (f"   ({info['published']})" if info["published"] else "") + f"   [{info['install_kind']}]")
        if not info["newer"]:
            print("უკვე უახლესი ვერსიაა.")
            return 0
        print(f"ახალი ვერსია: {info['name']}\n{info['url']}")
        if info["notes"]:
            print("\n" + "\n".join(info["notes"].splitlines()[:25]) + "\n")
        if a.check:
            return 10
        if a.download:
            asset = info["package"] or info["sdist"]
            if asset is None:
                raise updater.UpdateError("ამ გამოშვებაში ჩამოსატვირთი ფაილი არ არის")
            path = updater.download(asset, info["sums"], a.download)
            print(f"ჩამოტვირთულია და SHA-256 გადამოწმებულია: {path}")
            print(f"დასაყენებლად: {updater.package_hint(path)}")
            return 0
        if not info["can_auto"]:
            raise updater.UpdateError(
                {"package": "პაკეტით დაყენებულია: gisstack update --download ~/Downloads, შემდეგ დააყენე ჩამოტვირთული ფაილი",
                 "source": "git checkout-ია: git pull"}.get(info["install_kind"], "ჩამოტვირთე ხელით: " + info["url"]))
        if not a.yes and input("განვაახლო ახლავე? (yes/no): ").strip().lower() not in ("y", "yes"):
            print("გაუქმდა")
            return 1
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            for line in updater.apply(info, td):
                print(line, flush=True)
        return 0
    except updater.UpdateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def _rotate(a: argparse.Namespace, cfg: dict) -> int:
    ssh = _parse_ssh(a)
    if ssh is None:
        return 2
    new = None
    try:
        if a.password_file:
            p = Path(a.password_file)
            if hasattr(os, "geteuid") and p.stat().st_mode & 0o077:
                print(f"ERROR: {p} is readable by others (chmod 600 it)", file=sys.stderr)
                return 2
            new = p.read_text(encoding="utf-8").strip("\r\n")
        res = rotation.rotate(dict(cfg, deploy_target=a.target or cfg["deploy_target"]), a.what, a.mode, ssh, new)
    except (deploy.DeployError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"✔ {res['what']}: პაროლი შეიცვალა და გადამოწმდა. ახალი პაროლი: სერვერზე /etc/gisstack/credentials.txt და ამ კომპიუტერზე ~/.gisstack/secrets/{cfg['project']}.json")
    return 0


def _preflight(a: argparse.Namespace, cfg: dict) -> int:
    ssh = _parse_ssh(a)
    if ssh is None:
        return 2
    try:
        res = preflight.run(cfg, a.mode, ssh, a.target or cfg["deploy_target"])
    except deploy.DeployError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else preflight.format_table(res))
    return 0 if res["ok"] else 4


def _scan(a: argparse.Namespace, cfg: dict) -> int:
    ssh = _parse_ssh(a)
    if ssh is None:
        return 2
    try:
        rep = discover.scan(cfg, a.mode, ssh, a.target or cfg["deploy_target"])
    except deploy.DeployError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else discover.format_report(rep))
    return 3 if rep["conflicts"] else 0


def _existing(a: argparse.Namespace, cfg: dict) -> int:
    ssh = _parse_ssh(a)
    if ssh is None:
        return 2
    target = a.target or cfg["deploy_target"]
    comps = ["qgis_server" if c == "qgis" else c for c in a.components]
    if a.action == "remove" and not a.yes:
        print(f"ამოიშლება: {', '.join(comps)}" + (" მონაცემებითურთ!" if a.purge else " (მონაცემები დარჩება)") + " — ბექაფი ჯერ შეიქმნება.")
        if input("Type 'yes' to continue: ").strip() != "yes":
            print("aborted")
            return 1
    plan = {c: {"action": "remove", "purge": a.purge} for c in comps}
    try:
        if a.action == "backup":
            rep = discover.scan(cfg, a.mode, ssh, target)
            ts = existing.new_backup_id()
            out = ops._run(a.mode, ssh, existing.backup_script(rep, comps, ts, a.with_data), root=True, timeout=900)
            print(out, end="")
            if "BACKUP_VERIFIED=" not in out:
                print("ERROR: ბექაფი ვერ შეიქმნა ან ვერ გადამოწმდა", file=sys.stderr)
                return 1
            local = existing.fetch_backup(a.mode, ssh, ts, a.backup_dir, existing.host_label(a.mode, ssh))
            print("ასლი ამ მანქანაზე:", local, f"(SHA-256 ✔ {existing.verify_backup_archive(local)} ფაილი)")
            return 0
        for line in existing.run(cfg, target, a.mode, ssh, plan, None, with_data=a.with_data,
                                 backup_dir=a.backup_dir, install=False, confirm_data_loss=True):   # confirmed above
            print(line, flush=True)
        return 0
    except deploy.DeployError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def _apply(a: argparse.Namespace, cfg: dict) -> int:
    target = a.target or cfg["deploy_target"]
    ssh = _parse_ssh(a)
    if ssh is None:
        return 2
    try:
        if a.save:
            print("saved:", projects.save_component(
                dict(config._merge(config.default_config(), _load(a.config)), project=cfg["project"]), a.component))
        out = deploy.home_dir() / "bundles" / f"{cfg['project']}-apply"
        generate.generate_bundle(cfg, out)
        for line in deploy.apply_component(cfg, out, target, a.mode, ssh, a.component):
            print(line, flush=True)
    except (deploy.DeployError, projects.ProjectError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gisstack", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"gisstack {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("web", help="start the web installer")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--server", choices=["werkzeug", "waitress"], default="werkzeug",
                   help="werkzeug = built-in development server (default); waitress = production WSGI server (pip install waitress)")
    w.add_argument("--allow-host", action="append", default=[], metavar="NAME", help="accept this Host header too (repeatable)")
    w.add_argument("--rotate-token", action="store_true", help="create a new access token (invalidates old links and sessions)")

    sub.add_parser("desktop", help="start the desktop installer")
    sub.add_parser("defaults", help="print default configuration as JSON")

    g = sub.add_parser("generate", help="render the bundle")
    g.add_argument("-c", "--config")
    g.add_argument("-o", "--out", required=True)

    d = sub.add_parser("deploy", help="generate and run")
    d.add_argument("-c", "--config")
    d.add_argument("-o", "--out", help="bundle folder (default: ~/.gisstack/bundles/<project>)")
    d.add_argument("--target", choices=["native", "compose", "k8s"], help="default: deploy_target from config")
    d.add_argument("--mode", choices=["local", "ssh"], default="local")
    d.add_argument("--ssh", help="user@host[:port]")
    d.add_argument("--key", help="SSH private key file")
    d.add_argument("--existing", metavar="PLAN", help="what to do with programs already on the server: "
                   "comma list like postgis=adopt,tomcat=remove,caddy=reuse,nginx=disable (see: gisstack scan)")
    d.add_argument("--with-data", action="store_true", help="with --existing: also dump databases before removing")
    d.add_argument("--confirm-data-loss", action="store_true",
                   help="with --existing: allow '+purge' actions (they delete the program's data after a verified backup)")
    d.add_argument("--backup-dir", default=None, help="default: ~/.gisstack/backups")
    d.add_argument("--skip-preflight", action="store_true", help="do not check disk, memory, network and OS before installing")

    c = sub.add_parser("clean", help="remove the whole stack, or just some components")
    c.add_argument("-c", "--config")
    c.add_argument("--target", choices=["native", "compose", "k8s"], help="default: deploy_target from config")
    c.add_argument("--mode", choices=["local", "ssh"], default="local")
    c.add_argument("--ssh", help="user@host[:port]")
    c.add_argument("--key", help="SSH private key file")
    c.add_argument("--only", help="comma-separated components (default: everything): "
                   + ",".join(deploy.CLEAN_COMPONENTS))
    c.add_argument("--purge", action="store_true", help="also delete data (databases, caches, volumes)")
    c.add_argument("--remove-postgres", action="store_true", help="native: also uninstall the PostgreSQL server")
    c.add_argument("--force-postgres", action="store_true", help="remove PostgreSQL even if it holds other databases")
    c.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    ap_ = sub.add_parser("apply", help="(re)install just one component with the current settings")
    ap_.add_argument("-c", "--config")
    ap_.add_argument("component", choices=deploy.CLEAN_COMPONENTS)
    ap_.add_argument("--target", choices=["native", "compose", "k8s"], help="default: deploy_target from config")
    ap_.add_argument("--mode", choices=["local", "ssh"], default="local")
    ap_.add_argument("--ssh", help="user@host[:port]")
    ap_.add_argument("--key", help="SSH private key file")
    ap_.add_argument("--save", action="store_true", help="also store this component's settings in the project")

    sv = sub.add_parser("save", help="store a config as a project (~/.gisstack/projects/<name>.json)")
    sv.add_argument("-c", "--config")
    sv.add_argument("--component", choices=deploy.CLEAN_COMPONENTS, help="store only this component's part")

    sub.add_parser("projects", help="list saved projects")

    se = sub.add_parser("settings", help="show / change the interface language and theme (shared by the web and desktop GUI)")
    se.add_argument("--lang", choices=settings.LANGS)
    se.add_argument("--theme", choices=settings.THEMES)

    up = sub.add_parser("update", help="check GitHub for a newer release; download / install it")
    up.add_argument("--check", action="store_true", help="only report (exit 10 if a newer version exists)")
    up.add_argument("--download", metavar="DIR", help="download the verified package / archive into DIR and stop")
    up.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    def conn(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-c", "--config")
        sp.add_argument("--target", choices=["native", "compose", "k8s"], help="default: deploy_target from config")
        sp.add_argument("--mode", choices=["local", "ssh"], default="local")
        sp.add_argument("--ssh", help="user@host[:port]")
        sp.add_argument("--key", help="SSH private key file")

    st = sub.add_parser("status", help="live state, IP:port and health of every program")
    conn(st)
    st.add_argument("components", nargs="*", help="default: all selected components")
    st.add_argument("--json", action="store_true")
    st.add_argument("-w", "--watch", nargs="?", const=3, type=float, metavar="SEC", help="refresh every SEC seconds (default 3)")
    for act in ops.ACTIONS:
        sa = sub.add_parser(act, help=f"{act} programs (no names = whole stack)")
        conn(sa)
        sa.add_argument("components", nargs="*")
    lg = sub.add_parser("logs", help="show a program's log")
    conn(lg)
    lg.add_argument("component", choices=[*ops.ORDER, "qgis"])
    lg.add_argument("-n", "--lines", type=int, default=100)
    lg.add_argument("-f", "--follow", action="store_true")

    rt = sub.add_parser("rotate-credentials", help="change the PostgreSQL role password or the GeoServer admin password on an installed server")
    conn(rt)
    rt.add_argument("what", choices=rotation.WHAT)
    rt.add_argument("--password-file", metavar="FILE", help="read the new password from this file (mode 600) instead of generating one")

    pf = sub.add_parser("preflight", help="can this machine take the selected stack? (disk, memory, OS, network, clock)")
    conn(pf)
    pf.add_argument("--json", action="store_true")

    sc = sub.add_parser("scan", help="what is already installed on the server? (before installing ours)")
    conn(sc)
    sc.add_argument("--json", action="store_true")
    ex = sub.add_parser("existing", help="back up / remove / re-use programs that are already installed")
    conn(ex)
    ex.add_argument("action", choices=["backup", "remove"])
    ex.add_argument("components", nargs="+", help="postgis tomcat geoserver qgis_server mapproxy caddy")
    ex.add_argument("--purge", action="store_true", help="remove: also delete the program's data")
    ex.add_argument("--with-data", action="store_true", help="backup: also dump databases / big data directories")
    ex.add_argument("--backup-dir", default=None, help="default: ~/.gisstack/backups")
    ex.add_argument("-y", "--yes", action="store_true")

    a = ap.parse_args(argv)

    if a.cmd == "web":
        from .webgui.app import main as web_main
        web_main(a.host, a.port, a.server, tuple(a.allow_host), a.rotate_token)
        return 0
    if a.cmd == "desktop":
        if not deps.ensure_tk():           # asks before installing the system package
            return 1
        from .desktop import main as desk_main
        try:
            desk_main()
        except Exception as e:  # noqa: BLE001 - tkinter.TclError: no display (SSH session, server without a desktop)
            if type(e).__name__ != "TclError":
                raise
            print(i18n.tr("გრაფიკული ეკრანი არ არის ხელმისაწვდომი (DISPLAY არ არის დაყენებული). გამოიყენე ვებ GUI: gisstack web, ან SSH -X.",
                          settings.resolve_lang()), file=sys.stderr)
            return 1
        return 0
    if a.cmd == "defaults":
        d0 = config.default_config()
        print(json.dumps(d0, indent=2, ensure_ascii=False))
        return 0

    if a.cmd == "projects":
        print("\n".join(projects.list_projects()))
        return 0

    if a.cmd == "update":
        return _update(a)

    if a.cmd == "settings":
        cur = settings.save(a.lang, a.theme) if (a.lang or a.theme) else settings.load()
        print(f"lang={cur['lang']} (now: {settings.resolve_lang(cur['lang'])})  theme={cur['theme']}  repo={settings.REPO_URL}")
        return 0

    try:
        cfg = config.resolve(_load(a.config), persist_secrets=True)
    except config.ConfigError as e:
        print(f"configuration error:\n{e}", file=sys.stderr)
        return 2

    if a.cmd == "generate":
        out = generate.generate_bundle(cfg, a.out)
        print(f"bundle written to {out}")
        for w_ in cfg["_meta"]["warnings"]:
            print("note:", w_)
        return 0

    if a.cmd == "clean":
        return _clean(a, cfg)

    if a.cmd == "scan":
        return _scan(a, cfg)
    if a.cmd == "existing":
        return _existing(a, cfg)

    if a.cmd in ("status", "logs") or a.cmd in ops.ACTIONS:
        return _ops(a, cfg)

    if a.cmd == "preflight":
        return _preflight(a, cfg)
    if a.cmd == "rotate-credentials":
        return _rotate(a, cfg)

    if a.cmd == "save":
        try:
            raw = dict(config._merge(config.default_config(), _load(a.config)), project=cfg["project"])
            path = projects.save_component(raw, a.component) if a.component else projects.save(raw)
        except projects.ProjectError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"saved: {path}")
        return 0

    if a.cmd == "apply":
        return _apply(a, cfg)

    if a.cmd == "deploy":
        target = a.target or cfg["deploy_target"]
        out = Path(a.out) if a.out else deploy.home_dir() / "bundles" / cfg["project"]
        generate.generate_bundle(cfg, out)
        ssh = {}
        if a.mode == "ssh":
            if not a.ssh:
                print("--ssh user@host is required with --mode ssh", file=sys.stderr)
                return 2
            userhost, _, port = a.ssh.partition(":")
            user, _, host = userhost.rpartition("@")
            ssh = {"user": user or "root", "host": host, "port": int(port or 22), "key": a.key or ""}
        try:
            if not a.skip_preflight:
                res = preflight.run(cfg, a.mode, ssh, target)
                if not (res["ok"] and all(c["status"] == "ok" for c in res["checks"])):
                    print(preflight.format_table(res), file=sys.stderr)
                if not res["ok"]:
                    print("\nERROR: მანქანა არჩეული სტეკისთვის არ ვარგა (გვერდის ავლა: --skip-preflight)", file=sys.stderr)
                    return 4
            if a.existing is not None:
                plan = existing.parse_plan_spec(a.existing) if a.existing else {}

                def make(c: dict) -> Path:
                    generate.generate_bundle(c, out)
                    return out
                lines = existing.run(cfg, target, a.mode, ssh, plan, make, with_data=a.with_data, backup_dir=a.backup_dir,
                                     confirm_data_loss=a.confirm_data_loss)
            else:
                # no plan given: refuse only when someone else holds a port we need
                if target == "native":
                    rep = discover.scan(cfg, a.mode, ssh, target)
                    if rep["conflicts"]:
                        print(discover.format_report(rep), file=sys.stderr)
                        print("\nERROR: ზოგიერთი საჭირო პორტი დაკავებულია. გადაწყვიტე რა ვუყოთ არსებულს: "
                              "gisstack deploy ... --existing postgis=adopt,tomcat=remove,caddy=reuse,nginx=disable", file=sys.stderr)
                        return 3
                lines = deploy.deploy(cfg, out, target, a.mode, ssh)
            for line in lines:
                print(line, flush=True)
        except deploy.DeployError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
