"""gisstack command line.

  gisstack web [--host 127.0.0.1] [--port 8765]    web installer GUI
  gisstack desktop                                  desktop installer GUI (needs python3-tk)
  gisstack defaults > stack.json                    print the default configuration
  gisstack generate -c stack.json -o ./bundle       render all deployment files
  gisstack deploy   -c stack.json --mode local|ssh [--ssh user@host[:port]] [--key FILE]
  gisstack save     -c stack.json [--component postgis]   store as a project / store one program's settings
  gisstack apply    -c stack.json postgis [--save]        (re)install only that program
  gisstack clean    -c stack.json [--only tomcat,caddy] [--purge] [--remove-postgres] [-y]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, config, deploy, generate, projects


def _load(path: str | None) -> dict:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


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


def _apply(a: argparse.Namespace, cfg: dict) -> int:
    target = a.target or cfg["deploy_target"]
    ssh = _parse_ssh(a)
    if ssh is None:
        return 2
    try:
        if a.save:
            print("saved:", projects.save_component(dict(_load(a.config), project=cfg["project"]), a.component))
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

    a = ap.parse_args(argv)

    if a.cmd == "web":
        from .webgui.app import main as web_main
        web_main(a.host, a.port)
        return 0
    if a.cmd == "desktop":
        try:
            from .desktop import main as desk_main
        except ImportError:
            print("Tkinter is missing: sudo apt install python3-tk  |  sudo dnf install python3-tkinter", file=sys.stderr)
            return 1
        desk_main()
        return 0
    if a.cmd == "defaults":
        d0 = config.default_config()
        print(json.dumps(d0, indent=2, ensure_ascii=False))
        return 0

    if a.cmd == "projects":
        print("\n".join(projects.list_projects()))
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

    if a.cmd == "save":
        try:
            raw = dict(_load(a.config), project=cfg["project"])
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
            for line in deploy.deploy(cfg, out, target, a.mode, ssh):
                print(line, flush=True)
        except deploy.DeployError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
