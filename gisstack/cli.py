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
import contextlib
import json
import os
import sys
import time
from typing import Any
from pathlib import Path

from . import __version__, backup as backup_mod, backup_crypto, config, deploy, deps, discover, existing, generate, i18n, journal, ops, preflight, projects, rotation, services, settings, transport, updater
from .core import features, log, paths


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
    out = paths.bundles_dir() / f"{cfg['project']}-clean"
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
            path, sig = updater.download_verified(asset, info["sums"], a.download, sig_asset=info.get("signature"))
            print(updater.signature_line(sig))
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


def _token(a: argparse.Namespace) -> int:
    from .api.auth import TokenError, TokenStore
    store = TokenStore()
    try:
        if a.tcmd == "create":
            token, entry = store.create(a.name, a.scope, a.ttl_days)
            print(token)
            print(f"\ntoken id {entry['id']}, scope {entry['scope']}" + (f", expires in {a.ttl_days:g} days" if a.ttl_days else ", no expiry"), file=sys.stderr)
            print("This is the only time the token is shown; store it safely. Use it as:  Authorization: Bearer <token>", file=sys.stderr)
            return 0
        if a.tcmd == "list":
            rows = store.list()
            if a.json:
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    exp = time.strftime("%Y-%m-%d", time.localtime(r["expires"])) if r.get("expires") else "never"
                    used = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_used"])) if r.get("last_used") else "never"
                    print(f"{r['id']}  {r['scope']:<8} {r['name']:<24} expires {exp}  last used {used}")
                if not rows:
                    print("(no tokens)")
            return 0
        if store.revoke(a.id):
            print(f"revoked {a.id}")
            return 0
        print(f"ERROR: no such token: {a.id}", file=sys.stderr)
        return 1
    except TokenError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


def _api(a: argparse.Namespace) -> int:
    try:
        from .api import docs, routes, routes_geoserver, routes_mapproxy, routes_monitor, routes_postgis, routes_security, routes_servers, routes_stackbackup  # noqa: F401 - importing registers the routes
        from .api.registry import registry
    except ImportError as e:
        print(f"ERROR: the API needs Flask ({e}): pip install flask", file=sys.stderr)
        return 1
    spec = registry.openapi(__version__)
    text = json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=False) + "\n" if a.acmd == "openapi" else docs.markdown(spec)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


def _duration(text: str) -> float:
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not text or text[-1] not in unit or not text[:-1].isdigit():
        raise ValueError(f"bad duration {text!r} (use 30m, 6h, 7d)")
    return int(text[:-1]) * unit[text[-1]]


def _monitor(a: argparse.Namespace, cfg: dict) -> int:
    from .monitoring import alerts as m_alerts, render, service
    from .monitoring.store import Store, StoreError
    if not features.enabled("monitoring"):
        print("ERROR: monitoring is a feature that is switched off. Enable it with: export GISSTACK_FEATURES=monitoring", file=sys.stderr)
        return 3
    ssh = _parse_ssh(a)
    if ssh is None:
        return 2
    cfg = dict(cfg, deploy_target=a.target or cfg["deploy_target"])
    host = transport.host_label(a.mode, ssh)
    try:
        if a.mcmd in ("collect", "watch"):
            mon = service.Monitor(cfg, a.mode, ssh)

            def show(res: dict) -> None:
                if getattr(a, "json", False):
                    print(json.dumps({"errors": res["errors"], "notifications": res["notifications"]}, ensure_ascii=False, default=str), flush=True)
                else:
                    print(render.format_snapshot(service.snapshot(mon.store)), flush=True)
                    for n in res["notifications"]:
                        print(f"  >> {n['state'].upper()} {n['message']}", flush=True)
            if a.mcmd == "collect":
                show(mon.tick())
                return 0
            with contextlib.suppress(KeyboardInterrupt):
                mon.watch(a.interval, max_ticks=a.max_ticks, on_tick=show)
            return 0
        if a.mcmd == "rules":
            rules = m_alerts.load_rules(service.rules_path())
            print(json.dumps(rules, indent=2, ensure_ascii=False) if a.json else "\n".join(
                f"{'on ' if r['enabled'] else 'off'} {k:<22} {r['metric']} {r['op']} {r['threshold']:g} for {r['for']} sample(s) [{r['level']}]" for k, r in sorted(rules.items())))
            return 0
        db = paths.monitor_db(cfg["project"], host)
        if not db.exists():
            print(f"ERROR: no history for {cfg['project']} @ {host} yet - run: gisstack monitor collect", file=sys.stderr)
            return 1
        store = Store(db)
        if a.mcmd == "show":
            snap = service.snapshot(store)
            print(json.dumps(snap, ensure_ascii=False, indent=2) if a.json else render.format_snapshot(snap))
        elif a.mcmd == "alerts":
            state = [x for x in store.alert_state() if x["state"] == "firing"]
            ev = store.events(limit=20)
            if a.json:
                print(json.dumps({"firing": state, "events": ev}, ensure_ascii=False, indent=2))
            else:
                print("\n".join(f"FIRING {x['rule']} [{x['subject']}] {x['message']}" for x in state) or "no alerts firing")
                for e in ev:
                    print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(e['ts']))}  {e['level']:<5} {e['kind']:<16} {e['subject']}: {e['message']}")
        elif a.mcmd == "prune":
            print(f"deleted {store.prune()} rows")
        elif a.mcmd == "prometheus":
            print(service.prometheus_text(store, cfg["project"]), end="")
        elif a.mcmd == "history":
            labels = dict(x.split("=", 1) for x in a.label if "=" in x) or None
            pts = store.series(a.name, labels, since=time.time() - _duration(a.since), step=a.step)
            if a.json:
                print(json.dumps(pts, ensure_ascii=False))
            else:
                for p in pts:
                    print(f"{time.strftime('%H:%M:%S', time.gmtime(p['ts']))}  {p['value']:g}  {json.dumps(p['labels'], ensure_ascii=False) if p['labels'] else ''}")
        return 0
    except (deploy.DeployError, m_alerts.RulesError, StoreError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def _stack_backup(a: argparse.Namespace, cfg: dict) -> int:
    """Backups of a Compose / Podman / Kubernetes stack (the native target uses `gisstack backup`)."""
    from .backups import provider as stack_provider
    ssh = _parse_ssh(a)
    if ssh is None:
        return 2
    cfg = dict(cfg, deploy_target=a.target or cfg["deploy_target"])
    comps = [c for c in (getattr(a, "components", None) or "").split(",") if c] or None
    try:
        spec = _encrypt_spec(a) if a.sbcmd == "create" else None
        passphrase = backup_crypto.read_passphrase(getattr(a, "passphrase_file", None)) if a.sbcmd in ("restore", "check") else None
        prov = stack_provider.provider_for(cfg, a.mode, ssh, encryption=spec, passphrase=passphrase)

        def show(obj: Any) -> int:
            print(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))
            return 0
        if a.sbcmd == "list":
            rows = prov.list_backups()
            if a.json:
                return show(rows)
            for r in rows:
                print(f"{r['id']}  {r['status']:8} {r['kind']:11} {', '.join(r['components']) or '-'}  {r['bytes']} bytes{'  [encrypted]' if r['encrypted'] else ''}  {r['note']}")
            return 0
        if a.sbcmd == "inspect":
            return show(prov.inspect(a.id))
        if a.sbcmd in ("verify", "check"):
            rep = prov.verify(a.id) if a.sbcmd == "verify" else prov.check_restore(a.id, comps)
            if a.json:
                show(rep)
            else:
                print("OK" if rep["ok"] else "PROBLEMS:\n  " + "\n  ".join(rep["problems"]))
            return 0 if rep["ok"] else 1
        if a.sbcmd == "prune":
            res = prov.prune(keep=a.keep, days=a.days, keep_pre=a.keep_pre, dry_run=a.dry_run)
            return show(res)
        if a.sbcmd == "delete":
            if not a.yes and input(f"Delete backup {a.id}? type DELETE: ").strip() != "DELETE":
                print("cancelled")
                return 1
            prov.delete(a.id)
            print(f"deleted {a.id}")
            return 0
        if a.sbcmd == "restore":
            rep = prov.check_restore(a.id, comps)
            if not rep["ok"]:
                print("restore refused, nothing was changed:\n  " + "\n  ".join(rep["problems"]), file=sys.stderr)
                return 1
            print(f"restoring {a.id}: {', '.join(rep['components'])} (live data is replaced; a verified pre-restore backup is made first; undone automatically on failure)")
            if not a.yes and input("type RESTORE to continue: ").strip() != "RESTORE":
                print("cancelled")
                return 1
        gen = {"create": lambda: prov.create(comps, note=a.note, with_cache=a.with_cache), "restore": lambda: prov.restore(a.id, comps, confirm=True),
               "recover": prov.recover}[a.sbcmd]()
        with journal.exclusive():
            for line in gen:
                print(line, flush=True)
        if a.sbcmd == "create":
            print(f"BACKUP_VERIFIED={prov.result['id']}")
        return 0
    except (deploy.DeployError, journal.Busy) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def _crypt_args(sp: argparse.ArgumentParser) -> None:
    g = sp.add_argument_group("encryption of the copy kept on this machine (needs gpg; see docs/guides/en/BACKUP.md)")
    g.add_argument("--encrypt", action="store_true", help="encrypt with a passphrase (from --passphrase-file, $GISSTACK_BACKUP_PASSPHRASE, or asked twice)")
    g.add_argument("--passphrase-file", metavar="FILE", help="file whose first line is the passphrase (must be chmod 600); implies --encrypt")
    g.add_argument("--encrypt-to", metavar="KEY", action="append", help="encrypt to this public key (key ID, fingerprint or e-mail in your keyring); repeatable")


def _encrypt_spec(a: argparse.Namespace) -> backup_crypto.Spec | None:
    """--encrypt-to KEY (public key) or --encrypt (passphrase from --passphrase-file, $GISSTACK_BACKUP_PASSPHRASE or a prompt); None = plain copy."""
    to = getattr(a, "encrypt_to", None) or []
    if to:
        if getattr(a, "encrypt", False):
            raise backup_crypto.DeployError("--encrypt და --encrypt-to ერთად არ გამოიყენება")
        return backup_crypto.Spec(recipients=list(to)).validate()
    if getattr(a, "encrypt", False) or getattr(a, "passphrase_file", None):
        pw = backup_crypto.read_passphrase(getattr(a, "passphrase_file", None), prompt=True, confirm=True)
        return backup_crypto.Spec(passphrase=pw or "").validate()
    return None


def _backup_decrypt(a: argparse.Namespace) -> int:
    src = Path(a.file)
    if not backup_crypto.looks_encrypted(src):
        print("ERROR: " + "ფაილი .gpg არ არის", file=sys.stderr)
        return 1
    out = Path(a.out) if a.out else src.with_suffix("")
    try:
        pw = backup_crypto.read_passphrase(a.passphrase_file, prompt=False)
        backup_crypto.decrypt(src, out, pw)
        n = backup_mod.verify_archive(out)
    except deploy.DeployError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"{out} (SHA-256 ✔, {n} ფაილი)")
    return 0


def _backup(a: argparse.Namespace, cfg: dict) -> int:
    if a.bcmd == "decrypt":
        return _backup_decrypt(a)
    ssh = _parse_ssh(a)
    if ssh is None:
        return 2
    cfg = dict(cfg, deploy_target=a.target or cfg["deploy_target"])
    comps = [c for c in (getattr(a, "components", None) or "").split(",") if c] or None
    try:
        if a.bcmd == "create":
            lines: list[str] = []
            for line in backup_mod.create(cfg, a.mode, ssh, comps, with_cache=a.with_cache, note=a.note):
                lines.append(line)
                print(line, flush=True)
            bid = backup_mod.created_id(lines)
            if a.download is not None:
                path = backup_mod.download(cfg, a.mode, ssh, bid, a.download or None, _encrypt_spec(a))
                print(f"ასლი ამ კომპიუტერზე: {path} (SHA-256 ✔{', დაშიფრულია' if backup_crypto.looks_encrypted(path) else ''})")
            return 0
        if a.bcmd == "list":
            rows = backup_mod.list_backups(cfg, a.mode, ssh)
            print(json.dumps(rows, ensure_ascii=False, indent=2) if a.json else backup_mod.format_table(rows))
            return 0
        if a.bcmd == "verify":
            for line in backup_mod.verify(cfg, a.mode, ssh, a.id):
                print(line, flush=True)
            return 0
        if a.bcmd == "restore":
            return _backup_restore(a, cfg, ssh, comps)
        if a.bcmd == "prune":
            for line in backup_mod.prune(cfg, a.mode, ssh, a.keep, a.days, a.keep_pre, a.dry_run):
                print(line, flush=True)
            return 0
        if a.bcmd == "download":
            print(backup_mod.download(cfg, a.mode, ssh, a.id, a.dest, _encrypt_spec(a)))
            return 0
        if a.bcmd == "upload":
            pw = backup_crypto.read_passphrase(a.passphrase_file, prompt=False) if backup_crypto.looks_encrypted(a.file) else None
            print("UPLOAD_OK=" + backup_mod.upload(cfg, a.mode, ssh, a.file, pw))
            return 0
        if a.bcmd == "schedule":
            if a.action == "enable":
                print(backup_mod.schedule_enable(cfg, a.mode, ssh, calendar=a.calendar, keep=a.keep, days=a.days, components=comps,
                                                 with_cache=a.with_cache))
            elif a.action == "disable":
                print(backup_mod.schedule_disable(cfg, a.mode, ssh))
            else:
                print(backup_mod.schedule_status(cfg, a.mode, ssh))
            return 0
    except deploy.DeployError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 2


def _backup_restore(a: argparse.Namespace, cfg: dict, ssh: dict, comps: list[str] | None) -> int:
    only = [c for c in (a.only or "").split(",") if c] or None
    # the wizard: always show the plan first (nothing is touched), then ask
    for line in backup_mod.restore(cfg, a.mode, ssh, a.id, only=only, dry_run=True):
        print(line, flush=True)
    if a.dry_run:
        return 0
    if not a.staging and not a.yes:
        print("\nეს ცოცხალ მონაცემებს ჩაანაცვლებს (ძველი დარჩება გვერდით, ჯერ ავტომატური ბექაფი შეიქმნება).")
        if input("Type 'yes' to restore: ").strip() != "yes":
            print("aborted")
            return 1
    for line in backup_mod.restore(cfg, a.mode, ssh, a.id, only=only, dry_run=False, staging=a.staging, confirm=True,
                                   pre_backup=not a.no_pre_backup, rollback=not a.no_rollback):
        print(line, flush=True)
    return 0


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
            out = transport.run(a.mode, ssh, existing.backup_script(rep, comps, ts, a.with_data), root=True, timeout=900)
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
                dict(config.merge(config.default_config(), _load(a.config)), project=cfg["project"]), a.component))
        out = paths.bundles_dir() / f"{cfg['project']}-apply"
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
    ap.add_argument("--log-level", choices=["debug", "info", "warning", "error"], help="log to stderr (secrets are masked); default: silent")
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

    sb = sub.add_parser("stack-backup", help="verified backups and safe restores of a Docker Compose / Podman / Kubernetes stack")
    sbs = sb.add_subparsers(dest="sbcmd", required=True)

    def sconn(sp: argparse.ArgumentParser) -> None:
        conn(sp)
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--components", help="comma list of: postgres,geoserver,caddy,qgis,mapproxy_config,mapproxy_cache (default: everything this stack has; the cache only with --with-cache)")
    s1 = sbs.add_parser("create", help="create a backup (checksums, structure and the PostgreSQL dump are verified before it counts)")
    sconn(s1)
    s1.add_argument("--with-cache", action="store_true", help="also the MapProxy tile cache (can be very large)")
    s1.add_argument("--note", default="")
    _crypt_args(s1)
    sconn(sbs.add_parser("list", help="backups of this stack (newest first)"))
    for name, hlp in (("inspect", "manifest and integrity of one backup"), ("verify", "re-read a backup and check every checksum and archive"),
                      ("check", "pre-restore validation (changes nothing)"), ("restore", "restore a backup: validate, pre-restore backup, stage, swap, health check, undo on failure"),
                      ("delete", "delete one backup")):
        sp = sbs.add_parser(name, help=hlp)
        sconn(sp)
        sp.add_argument("id")
        if name in ("restore", "delete"):
            sp.add_argument("--yes", action="store_true", help="do not ask for the typed confirmation")
        if name in ("restore", "check"):
            sp.add_argument("--passphrase-file", metavar="FILE", help="passphrase of an encrypted backup (first line; mode 600); a private key is used through gpg-agent")
    sconn(sbs.add_parser("recover", help="put the previous data back after an interrupted restore"))
    s9 = sbs.add_parser("prune", help="retention: keep the newest N (the newest intact backup is never removed)")
    sconn(s9)
    s9.add_argument("--keep", type=int, default=14)
    s9.add_argument("--keep-pre", type=int, default=5)
    s9.add_argument("--days", type=int, default=None)
    s9.add_argument("--dry-run", action="store_true")

    bk = sub.add_parser("backup", help="verified backups and safe restores of an installed (native) stack")
    bsub = bk.add_subparsers(dest="bcmd", required=True)

    def bconn(sp: argparse.ArgumentParser) -> None:
        conn(sp)
        sp.add_argument("--json", action="store_true")
    b1 = bsub.add_parser("create", help="create a backup on the server (config, PostgreSQL, GeoServer data, QGIS projects)")
    bconn(b1)
    b1.add_argument("--components", help="comma list of: " + ",".join(backup_mod.COMPONENTS) + " (default: everything that exists)")
    b1.add_argument("--with-cache", action="store_true", help="also the MapProxy tile cache (can be very large)")
    b1.add_argument("--note", default="")
    b1.add_argument("--download", metavar="DIR", nargs="?", const="", help="also copy the verified backup to this machine")
    _crypt_args(b1)
    bconn(bsub.add_parser("list", help="backups on the server"))
    b3 = bsub.add_parser("verify", help="re-read a backup and check every checksum")
    bconn(b3)
    b3.add_argument("id")
    b4 = bsub.add_parser("restore", help="restore a backup (shows the plan first, asks before touching anything)")
    bconn(b4)
    b4.add_argument("id")
    b4.add_argument("--only", help="comma list of components")
    b4.add_argument("--dry-run", action="store_true", help="only show the plan")
    b4.add_argument("--staging", action="store_true", help="unpack next to the live data (DB under a new name) instead of replacing it")
    b4.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    b4.add_argument("--no-pre-backup", action="store_true", help="skip the automatic backup of the current state (not recommended)")
    b4.add_argument("--no-rollback", action="store_true", help="keep the half-restored state on failure instead of rolling back")
    b5 = bsub.add_parser("prune", help="delete old backups (the newest is never deleted)")
    bconn(b5)
    b5.add_argument("--keep", type=int, default=14)
    b5.add_argument("--days", type=int, help="only delete backups older than this many days (beyond --keep)")
    b5.add_argument("--keep-pre", type=int, default=5, help="how many pre-restore backups to keep")
    b5.add_argument("--dry-run", action="store_true")
    b6 = bsub.add_parser("download", help="copy a backup to this machine (verified, never overwrites)")
    bconn(b6)
    b6.add_argument("id")
    b6.add_argument("--dest", help="default: ~/.gisstack/backups/stack/<host>/")
    _crypt_args(b6)
    b7 = bsub.add_parser("upload", help="put a downloaded backup on a (new) server, e.g. for disaster recovery")
    bconn(b7)
    b7.add_argument("file")
    b7.add_argument("--passphrase-file", metavar="FILE", help="for an encrypted (.gpg) copy made with a passphrase (default: $GISSTACK_BACKUP_PASSPHRASE; a public-key copy asks gpg)")
    b9 = bsub.add_parser("decrypt", help="decrypt a downloaded .gpg backup on this machine (no server involved)")
    b9.add_argument("file")
    b9.add_argument("-o", "--out", help="default: the file name without .gpg, next to it")
    b9.add_argument("--passphrase-file", metavar="FILE")
    b9.add_argument("-c", "--config", help=argparse.SUPPRESS)            # no server and no configuration involved; the shared start-up code expects the option
    b8 = bsub.add_parser("schedule", help="automatic backups with a systemd timer")
    bconn(b8)
    b8.add_argument("action", choices=["enable", "disable", "status"])
    b8.add_argument("--calendar", default="daily", help="systemd OnCalendar value, e.g. daily, 'Mon *-*-* 03:00' (default: daily)")
    b8.add_argument("--keep", type=int, default=14)
    b8.add_argument("--days", type=int)
    b8.add_argument("--components")
    b8.add_argument("--with-cache", action="store_true")

    mo = sub.add_parser("monitor", help="real metrics, history and alerts (feature flag: GISSTACK_FEATURES=monitoring)")
    msub = mo.add_subparsers(dest="mcmd", required=True)
    m1 = msub.add_parser("collect", help="take one sample now, store it, evaluate alerts")
    conn(m1)
    m1.add_argument("--json", action="store_true")
    m2 = msub.add_parser("watch", help="sample every N seconds (run it as a service)")
    conn(m2)
    m2.add_argument("--interval", type=float, default=30.0)
    m2.add_argument("--max-ticks", type=int)
    m2.add_argument("--json", action="store_true")
    for name, hlp in (("show", "newest stored sample"), ("alerts", "firing alerts and recent events"), ("rules", "effective alert rules"),
                      ("prune", "apply the retention policy to the history"), ("prometheus", "newest sample as Prometheus text")):
        sp = msub.add_parser(name, help=hlp)
        conn(sp)
        sp.add_argument("--json", action="store_true")
    m3 = msub.add_parser("history", help="stored values of one metric")
    conn(m3)
    m3.add_argument("name")
    m3.add_argument("--since", default="1h", help="e.g. 30m, 6h, 7d (default 1h)")
    m3.add_argument("--step", type=float, help="average into buckets of this many seconds")
    m3.add_argument("--label", action="append", default=[], metavar="K=V")
    m3.add_argument("--json", action="store_true")

    tk = sub.add_parser("token", help="API tokens with scopes (read < operate < admin) for /api/v1")
    tsub = tk.add_subparsers(dest="tcmd", required=True)
    t1 = tsub.add_parser("create", help="create a token (shown once)")
    t1.add_argument("--name", required=True)
    t1.add_argument("--scope", choices=["read", "operate", "admin"], required=True)
    t1.add_argument("--ttl-days", type=float, help="expire after this many days (default: never)")
    t2 = tsub.add_parser("list", help="tokens (secrets are never stored or shown)")
    t2.add_argument("--json", action="store_true")
    t3 = tsub.add_parser("revoke", help="revoke a token at once")
    t3.add_argument("id")

    ap2 = sub.add_parser("api", help="the HTTP API description (generated from the route table)")
    asub = ap2.add_subparsers(dest="acmd", required=True)
    for name, hlp in (("openapi", "OpenAPI 3.1 document (JSON)"), ("docs", "Markdown reference")):
        sp = asub.add_parser(name, help=hlp)
        sp.add_argument("-o", "--out", help="write to a file instead of stdout")

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
    log.configure(a.log_level)

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

    if a.cmd == "token":
        return _token(a)
    if a.cmd == "api":
        return _api(a)

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

    if a.cmd == "monitor":
        return _monitor(a, cfg)
    if a.cmd == "backup":
        return _backup(a, cfg)
    if a.cmd == "stack-backup":
        return _stack_backup(a, cfg)
    if a.cmd == "preflight":
        return _preflight(a, cfg)
    if a.cmd == "rotate-credentials":
        return _rotate(a, cfg)

    if a.cmd == "save":
        try:
            raw = dict(config.merge(config.default_config(), _load(a.config)), project=cfg["project"])
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
        out = Path(a.out) if a.out else paths.bundles_dir() / cfg["project"]
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
            try:
                res = services.ensure_ready(cfg, a.mode, ssh, target, skip_preflight=a.skip_preflight)
            except services.PreflightFailed as e:
                print(preflight.format_table(e.result), file=sys.stderr)
                print("\nERROR: მანქანა არჩეული სტეკისთვის არ ვარგა (გვერდის ავლა: --skip-preflight)", file=sys.stderr)
                return 4
            if res and not all(c["status"] == "ok" for c in res["checks"]):
                print(preflight.format_table(res), file=sys.stderr)
            if a.existing is not None:
                plan = existing.parse_plan_spec(a.existing) if a.existing else {}

                def make(c: dict) -> Path:
                    generate.generate_bundle(c, out)
                    return out
                lines = services.deploy_steps(cfg, out, target, a.mode, ssh, plan=plan, make_bundle=make, with_data=a.with_data,
                                              backup_dir=a.backup_dir, confirm_data_loss=a.confirm_data_loss)
            else:
                rep = services.find_conflicts(cfg, a.mode, ssh, target)       # refuse only when someone else holds a port we need
                if rep:
                    print(discover.format_report(rep), file=sys.stderr)
                    print("\nERROR: ზოგიერთი საჭირო პორტი დაკავებულია. გადაწყვიტე რა ვუყოთ არსებულს: "
                          "gisstack deploy ... --existing postgis=adopt,tomcat=remove,caddy=reuse,nginx=disable", file=sys.stderr)
                    return 3
                lines = services.deploy_steps(cfg, out, target, a.mode, ssh, refuse_conflicts=False)
            for line in lines:
                print(line, flush=True)
        except deploy.DeployError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
