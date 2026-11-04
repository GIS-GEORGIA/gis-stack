"""Desktop GUI (Tkinter) for the installer.

Runs on your own computer, writes the bundle to a folder and can deploy it to a
server over SSH (key-based) or on this machine.  Needs python3-tk
(`sudo apt install python3-tk` / `sudo dnf install python3-tkinter`).
"""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any

from . import __version__, catalog, config, deploy, discover, existing, generate, ops, projects, updater

LAYER_HELP = ("ერთი ფენა ხაზზე:  სახელი | სათაური EN | სათაური KA | წყარო | WMS ფენა | URL\n"
              "წყარო: geoserver | qgis | wms | tiles")


def parse_layers(text: str) -> list[dict[str, str]]:
    layers = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [p.strip() for p in raw.split("|")] + [""] * 6
        name, title, title_ka, source, wms, url = parts[:6]
        layers.append({"name": name, "title": title or name, "title_ka": title_ka,
                       "source": source or "geoserver", "wms_layers": wms, "url": url})
    return layers


def layers_to_text(layers: list[dict[str, Any]]) -> str:
    return "\n".join(" | ".join([l.get("name", ""), l.get("title", ""), l.get("title_ka", ""),
                                 l.get("source", "geoserver"), l.get("wms_layers", ""), l.get("url", "")])
                     for l in layers)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"GIS Stack Installer {__version__}")
        self.geometry("1020x780")
        self.d = config.default_config()
        self.vars: dict[str, tk.Variable] = {}
        self.checks: dict[str, dict[str, tk.BooleanVar]] = {}
        self.log_q: queue.Queue[str | None] = queue.Queue()
        self.busy = False
        self.clean_btns: list[ttk.Button] = []     # every button that must stay disabled while a job runs

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(top, text="შენახული პროექტები:").pack(side="left")
        self.proj_var = tk.StringVar()
        self.proj_box = ttk.Combobox(top, textvariable=self.proj_var, state="readonly", width=28)
        self.proj_box.pack(side="left", padx=4)
        ttk.Button(top, text="ჩატვირთვა", command=self.load_project).pack(side="left", padx=2)
        ttk.Button(top, text="მთელი პროექტის შენახვა", command=self.save_project).pack(side="left", padx=2)
        ttk.Button(top, text="განახლების შემოწმება", command=self.check_update).pack(side="right", padx=2)
        self.refresh_projects()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.nb = nb
        self._tab_components_vars()
        self._tab_general(nb)
        self._tab_components(nb)
        self._tab_postgres(nb)
        self._tab_servers(nb)
        self._tab_mapproxy(nb)
        self._tab_map(nb)
        self._tab_deploy(nb)
        self._tab_existing(nb)
        self._tab_clean(nb)
        self._tab_ops(nb)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8)
        ttk.Button(bar, text="წინასწარი ნახვა", command=self.preview).pack(side="left", padx=4)
        ttk.Button(bar, text="შენახვა საქაღალდეში…", command=self.save_folder).pack(side="left", padx=4)
        self.deploy_btn = ttk.Button(bar, text="Deploy", command=self.do_deploy)
        self.deploy_btn.pack(side="left", padx=4)
        self.log = scrolledtext.ScrolledText(self, height=11, state="disabled", font=("monospace", 9))
        self.log.pack(fill="both", padx=8, pady=8)
        self.after(150, self._drain)

    # ------------------------------------------------------------ widgets
    def _entry(self, parent, label, path, row, width=34, show=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=3)
        cur = self.d
        for k in path.split("."):
            cur = cur[k]
        v = tk.StringVar(value="" if cur in (None, "") else str(cur))
        ttk.Entry(parent, textvariable=v, width=width, show=show).grid(row=row, column=1, sticky="w", padx=6)
        self.vars[path] = v

    def _combo(self, parent, label, path, values, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=3)
        cur = self.d
        for k in path.split("."):
            cur = cur[k]
        v = tk.StringVar(value=str(cur))
        ttk.Combobox(parent, textvariable=v, values=values, state="readonly", width=32).grid(row=row, column=1, sticky="w", padx=6)
        self.vars[path] = v

    def _check(self, parent, label, path, row):
        cur = self.d
        for k in path.split("."):
            cur = cur[k]
        v = tk.BooleanVar(value=bool(cur))
        ttk.Checkbutton(parent, text=label, variable=v).grid(row=row, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        self.vars[path] = v

    def _group(self, parent, name, items, selected, cols=2, label_key="ka", start_row=0):
        self.checks[name] = {}
        for i, it in enumerate(items):
            iid = it["id"] if isinstance(it, dict) else it
            text = it.get(label_key, iid) if isinstance(it, dict) else it
            v = tk.BooleanVar(value=iid in selected)
            ttk.Checkbutton(parent, text=text, variable=v).grid(row=start_row + i // cols, column=i % cols, sticky="w", padx=6, pady=1)
            self.checks[name][iid] = v

    def _tab(self, nb, title):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text=title)
        return f

    def _comp_bars(self, parent, row, comps):
        """Per-program bar: tick (on/off), save the program's settings, save + roll out only this program."""
        box = ttk.LabelFrame(parent, text="პროგრამა: მონიშვნა / შენახვა / გამოყენება")
        box.grid(row=row, column=0, columnspan=3, sticky="we", pady=10)
        names = {c["id"]: c["ka"] for c in catalog.COMPONENTS}
        for i, cid in enumerate(comps):
            ttk.Checkbutton(box, text=f"{names.get(cid, cid)} — ჩართულია", variable=self.checks["comp"][cid],
                            width=42).grid(row=i, column=0, sticky="w", padx=6, pady=2)
            b1 = ttk.Button(box, text="შენახვა", command=lambda c=cid: self.do_save_comp(c))
            b2 = ttk.Button(box, text="შენახვა და გამოყენება", command=lambda c=cid: self.do_apply(c))
            b1.grid(row=i, column=1, padx=4, pady=2)
            b2.grid(row=i, column=2, padx=4, pady=2)
            self.clean_btns += [b2]

    # --------------------------------------------------------------- tabs
    def _tab_general(self, nb):
        f = self._tab(nb, "ზოგადი")
        self._entry(f, "პროექტი", "project", 0)
        self._entry(f, "დომენი", "domain", 1)
        self._entry(f, "ელ-ფოსტა (Let's Encrypt)", "email", 2)
        ttk.Label(f, text="HTTPS").grid(row=3, column=0, sticky="w", padx=6, pady=3)
        self.tls_map = {t["ka"]: t["id"] for t in catalog.TLS_MODES}
        self.tls_var = tk.StringVar(value=next(k for k, v in self.tls_map.items() if v == self.d["tls"]))
        ttk.Combobox(f, textvariable=self.tls_var, values=list(self.tls_map), state="readonly", width=60).grid(row=3, column=1, sticky="w", padx=6)
        self._comp_bars(f, 4, ["caddy"])

    def _tab_components_vars(self):
        """The on/off ticks are created first so every tab's per-program bar can share them."""
        self.checks["comp"] = {c["id"]: tk.BooleanVar(value=bool(self.d["components"].get(c["id"])))
                               for c in catalog.COMPONENTS}

    def _tab_components(self, nb):
        f = self._tab(nb, "კომპონენტები")
        for i, c in enumerate(catalog.COMPONENTS):
            ttk.Checkbutton(f, text=c["ka"], variable=self.checks["comp"][c["id"]]).grid(
                row=i, column=0, sticky="w", padx=6, pady=1)
        ttk.Label(f, text="GeoServer native რეჟიმში Tomcat-ში ინსტალირდება — Tomcat ავტომატურად დაემატება.").grid(
            row=20, column=0, sticky="w", padx=6, pady=8)

    def _tab_postgres(self, nb):
        f = self._tab(nb, "PostgreSQL")
        self._combo(f, "ვერსია", "postgres.version", catalog.POSTGRES_VERSIONS, 0)
        self._entry(f, "ბაზა", "postgres.db", 1)
        self._entry(f, "მომხმარებელი", "postgres.user", 2)
        self._entry(f, "პაროლი (ცარიელი = შემთხვევითი)", "postgres.password", 3)
        self._entry(f, "RAM (GB)", "postgres.ram_gb", 4)
        self._entry(f, "max_connections", "postgres.max_connections", 5)
        self._check(f, "გარე წვდომა", "postgres.remote_access", 6)
        self._entry(f, "დაშვებული CIDR", "postgres.allowed_cidr", 7)
        box = ttk.LabelFrame(f, text="ექსთენშენები")
        box.grid(row=8, column=0, columnspan=2, sticky="we", pady=8)
        self._group(box, "ext", catalog.EXTENSIONS, self.d["postgres"]["extensions"], cols=3)
        self._comp_bars(f, 9, ["postgis"])

    def _tab_servers(self, nb):
        f = self._tab(nb, "GeoServer / Tomcat / QGIS")
        ttk.Label(f, text="GeoServer ხაზი").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        self.ch_map = {f"{c['label']} — {c['version']}": k for k, c in catalog.GEOSERVER_CHANNELS.items()}
        self.ch_var = tk.StringVar(value=next(k for k, v in self.ch_map.items() if v == self.d["geoserver"]["channel"]))
        ttk.Combobox(f, textvariable=self.ch_var, values=list(self.ch_map), state="readonly", width=40).grid(row=0, column=1, sticky="w", padx=6)
        self._entry(f, "GeoServer heap (MB)", "geoserver.heap_mb", 1)
        self._entry(f, "admin პაროლი (ცარიელი = შემთხვევითი)", "geoserver.admin_password", 2)
        self._entry(f, "Tomcat heap (MB)", "tomcat.heap_mb", 3)
        self._entry(f, "QGIS პროცესები", "qgis.processes", 4)
        box = ttk.LabelFrame(f, text="GeoServer გაფართოებები")
        box.grid(row=5, column=0, columnspan=2, sticky="we", pady=8)
        self._group(box, "gsext", catalog.GEOSERVER_EXTENSIONS, [], cols=4)
        self._comp_bars(f, 6, ["geoserver", "tomcat", "qgis_server"])

    def _tab_mapproxy(self, nb):
        f = self._tab(nb, "MapProxy")
        self._combo(f, "ქეშის ტიპი", "mapproxy.cache_type", catalog.CACHE_TYPES, 0)
        self._combo(f, "ფორმატი", "mapproxy.format", catalog.TILE_FORMATS, 1)
        self._entry(f, "workers", "mapproxy.workers", 2)
        self._combo(f, "ფონი", "mapproxy.upstream_basemap", ["none", "osm"], 3)
        box = ttk.LabelFrame(f, text="Grid-ები")
        box.grid(row=4, column=0, columnspan=2, sticky="we", pady=6)
        self._group(box, "grid", catalog.GRIDS, self.d["mapproxy"]["grids"], cols=2)
        ttk.Label(f, text=LAYER_HELP).grid(row=5, column=0, columnspan=2, sticky="w", padx=6)
        self.layers_txt = tk.Text(f, height=7, width=100)
        self.layers_txt.grid(row=6, column=0, columnspan=2, padx=6, pady=4)
        self.layers_txt.insert("1.0", layers_to_text(self.d["mapproxy"]["layers"]))
        self._check(f, "წინასწარი seed", "mapproxy.seed.enabled", 7)
        self._entry(f, "seed დონეები (from,to)", "mapproxy.seed.levels", 8)
        self._entry(f, "seed bbox (W,S,E,N)", "mapproxy.seed.bbox", 9)
        self._comp_bars(f, 10, ["mapproxy"])

    def _tab_map(self, nb):
        f = self._tab(nb, "ვებ რუკა")
        self._entry(f, "სათაური (KA)", "webmap.title_ka", 0)
        self._entry(f, "Title (EN)", "webmap.title_en", 1)
        self._combo(f, "ენა", "webmap.default_lang", ["ka", "en"], 2)
        self._entry(f, "ცენტრი lat,lon", "webmap.center", 3)
        self._entry(f, "zoom", "webmap.zoom", 4)
        self._combo(f, "ფონური რუკა", "webmap.basemap", [b["id"] for b in catalog.BASEMAPS], 5)
        self._entry(f, "საკუთარი XYZ URL", "webmap.basemap_url", 6, width=60)
        self._comp_bars(f, 7, ["webmap"])

    def _tab_deploy(self, nb):
        f = self._tab(nb, "სამიზნე / Deploy")
        box = ttk.LabelFrame(f, text="რა დაგენერირდეს")
        box.grid(row=0, column=0, columnspan=2, sticky="we", pady=4)
        self._group(box, "target", catalog.TARGETS, self.d["targets"], cols=3)
        self._combo(f, "Docker / Podman", "runtime", catalog.RUNTIMES, 1)
        self._combo(f, "რომელი გავუშვა", "deploy_target", [t["id"] for t in catalog.TARGETS], 2)
        self.d["_mode"] = "ssh"
        self._combo(f, "სად", "_mode", ["ssh", "local"], 3)
        self._entry(f, "SSH host", "ssh.host", 4)
        self._entry(f, "SSH port", "ssh.port", 5)
        self._entry(f, "SSH user", "ssh.user", 6)
        self._entry(f, "SSH key (ფაილი)", "ssh.key", 7, width=50)
        ttk.Button(f, text="…", width=3, command=lambda: self.vars["ssh.key"].set(filedialog.askopenfilename() or self.vars["ssh.key"].get())).grid(row=7, column=2)
        self._entry(f, "K8s namespace", "k8s.namespace", 8)
        self._entry(f, "K8s StorageClass", "k8s.storage_class", 9)
        self._combo(f, "K8s Service ტიპი", "k8s.service_type", ["LoadBalancer", "NodePort", "ClusterIP"], 10)

    def _tab_existing(self, nb):
        f = self._tab(nb, "არსებული პროგრამები")
        self.exist_tab = f
        self.exist_plan: dict[str, dict[str, Any]] = {}
        self.exist_rep = None
        ttk.Label(f, text="სანამ ჩვენს სტეკს დავაყენებთ, ვამოწმებთ რა დგას სერვერზე. აირჩიე პროგრამა სიაში და მიუთითე რა ვუყოთ.\n"
                          "Deploy გამოიყენებს ამ გეგმას. მხოლოდ native სამიზნე; ყოველ წაშლამდე ბექაფი კეთდება.",
                  justify="left").pack(anchor="w", padx=4, pady=2)
        top = ttk.Frame(f)
        top.pack(fill="x", pady=4)
        ttk.Button(top, text="შემოწმება", command=self.exist_scan).pack(side="left", padx=2)
        self.exist_data = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="ბექაფში მონაცემებიც (ბაზების დამპი)", variable=self.exist_data).pack(side="left", padx=8)
        self.exist_info = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.exist_info).pack(side="left", padx=8)
        cols = ("state", "found", "plan")
        self.exist_tree = ttk.Treeview(f, columns=cols, height=8)
        self.exist_tree.heading("#0", text="პროგრამა")
        self.exist_tree.column("#0", width=170)
        for c, t, w in (("state", "ვისია", 70), ("found", "რა არის ნაპოვნი", 480), ("plan", "გეგმა", 150)):
            self.exist_tree.heading(c, text=t)
            self.exist_tree.column(c, width=w, anchor="w")
        self.exist_tree.pack(fill="x", pady=4)
        self.exist_conf = tk.Text(f, height=4, state="disabled", font=("monospace", 9), foreground="#b00020")
        self.exist_conf.pack(fill="x", pady=2)
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=4)
        for text, act in (("დატოვება", "ignore"), ("დაშენება (PostgreSQL)", "adopt"), ("წაშლა", "remove"),
                          ("წაშლა + მონაცემები", "remove+purge"), ("გამოყენება ჩვენში", "reuse"), ("გავთიშო (web სერვერი)", "disable")):
            ttk.Button(bar, text=text, command=lambda a=act: self.exist_set(a)).pack(side="left", padx=2)
        bar2 = ttk.Frame(f)
        bar2.pack(fill="x", pady=4)
        b1 = ttk.Button(bar2, text="ბექაფი (უცხო პროგრამების)", command=self.exist_backup)
        b2 = ttk.Button(bar2, text="გეგმის შესრულება (დაყენების გარეშე)", command=self.exist_run)
        b1.pack(side="left", padx=2)
        b2.pack(side="left", padx=2)
        self.clean_btns += [b1, b2]

    # ------------------------------------------------------------ existing programs
    def _exist_conn(self):
        got = self._ops_cfg()
        if not got:
            return None
        cfg, mode, ssh = got
        if cfg["deploy_target"] != "native":
            messagebox.showinfo("არსებული პროგრამები", "ეს ფუნქცია მხოლოდ native სამიზნეზეა (ჩანართი „სამიზნე / Deploy“).")
            return None
        return cfg, mode, ssh

    def exist_scan(self) -> None:
        got = self._exist_conn()
        if not got:
            return
        cfg, mode, ssh = got
        self.exist_info.set("მოწმდება…")

        def run() -> None:
            try:
                rep = discover.scan(cfg, mode, ssh, "native")
                self.after(0, lambda: self._exist_show(rep, None))
            except deploy.DeployError as e:
                self.after(0, lambda: self._exist_show(None, str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _exist_show(self, rep, err) -> None:
        if err:
            self.exist_info.set("შეცდომა: " + err)
            return
        self.exist_rep = rep
        self.exist_info.set(f"{rep['os']} · კონფლიქტები: {len(rep['conflicts'])}")
        self.exist_tree.delete(*self.exist_tree.get_children())
        for c in rep["components"].values():
            if not c["found"]:
                continue
            who = "ჩვენია" if c["ours"] and not c["foreign"] else ("უცხო" if c["foreign"] and not c["ours"] else "ორივე")
            bits = ([c["version"]] if c["version"] else []) + [f"{s['name']}:{s['active']}" for s in c["services"][:3]] \
                + c["listen"][:2] + c["data"][:3]
            self.exist_tree.insert("", "end", iid=c["id"], text=c["name"], values=(who, "; ".join(bits), self._plan_label(c["id"])))
        for w in rep["web_servers"]:
            self.exist_tree.insert("", "end", iid=w["name"], text=w["name"],
                                   values=("web სერვერი", "; ".join(f"{s['name']}:{s['active']}" for s in w["services"]) + " " + " ".join(w["listen"]),
                                           self._plan_label(w["name"])))
        self.exist_conf.configure(state="normal")
        self.exist_conf.delete("1.0", "end")
        self.exist_conf.insert("1.0", "\n".join(f"პორტი {x['port']} ({x['component']}) დაკავებულია: {x['process']} {x['addr']}" for x in rep["conflicts"]))
        self.exist_conf.configure(state="disabled")

    def _plan_label(self, comp: str) -> str:
        p = self.exist_plan.get(comp)
        return "-" if not p else p["action"] + ("+purge" if p["purge"] else "")

    def exist_set(self, act: str) -> None:
        sel = self.exist_tree.selection()
        if not sel:
            messagebox.showinfo("არსებული პროგრამები", "აირჩიე პროგრამა სიაში")
            return
        purge = act.endswith("+purge")
        action = act.removesuffix("+purge")
        for comp in sel:
            if action == "ignore":
                self.exist_plan.pop(comp, None)
            else:
                self.exist_plan[comp] = {"action": action, "purge": purge}
            self.exist_tree.set(comp, "plan", self._plan_label(comp))

    def _exist_job(self, title: str, steps) -> None:
        if self.busy:
            return
        self.busy = True
        self.deploy_btn.state(["disabled"])
        for b in self.clean_btns:
            b.state(["disabled"])

        def run() -> None:
            try:
                for line in steps():
                    self._log(line)
            except deploy.DeployError as e:
                self._log(f"ERROR: {e}")
            finally:
                self.log_q.put(None)
                self.after(0, self.exist_scan)
        threading.Thread(target=run, daemon=True).start()

    def exist_backup(self) -> None:
        got = self._exist_conn()
        if not got or not self.exist_rep:
            if got:
                messagebox.showinfo("არსებული პროგრამები", "ჯერ დააჭირე „შემოწმებას“")
            return
        cfg, mode, ssh = got
        comps = [c["id"] for c in self.exist_rep["components"].values() if c["foreign"]]
        if not comps:
            messagebox.showinfo("არსებული პროგრამები", "უცხო პროგრამა არ არის ნაპოვნი")
            return
        wd = self.exist_data.get()
        self._exist_job("backup", lambda: existing.backup(cfg, "native", mode, ssh, comps, with_data=wd))

    def exist_run(self) -> None:
        got = self._exist_conn()
        if not got:
            return
        cfg, mode, ssh = got
        if not self.exist_plan:
            messagebox.showinfo("არსებული პროგრამები", "გეგმა ცარიელია")
            return
        what = "\n".join(f"{k}: {self._plan_label(k)}" for k in self.exist_plan)
        if not messagebox.askyesno("გეგმის შესრულება", f"შესრულდება (ყოველ წაშლამდე ბექაფი):\n{what}\n\nგავაგრძელო?", icon="warning"):
            return
        plan, wd = dict(self.exist_plan), self.exist_data.get()
        self._exist_job("run", lambda: existing.run(cfg, "native", mode, ssh, plan, lambda c: None, with_data=wd, install=False))

    def _tab_clean(self, nb):
        f = self._tab(nb, "გასუფთავება / Cleanup")
        ttk.Label(f, text="იყენებს „სამიზნე / Deploy“ ჩანართში არჩეულ სამიზნეს და „სად“-ს.").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=6, pady=3)
        for i, c in enumerate(catalog.COMPONENTS, start=1):
            ttk.Label(f, text=c["ka"]).grid(row=i, column=0, sticky="w", padx=6, pady=2)
            b = ttk.Button(f, text="გასუფთავება", command=lambda cid=c["id"]: self.do_clean([cid]))
            b.grid(row=i, column=1, sticky="w", padx=6)
            self.clean_btns.append(b)
        n = len(catalog.COMPONENTS) + 1
        self.cl_purge = tk.BooleanVar(value=False)
        self.cl_pg = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="მონაცემების წაშლაც (ბაზა, GeoServer data, ქეშები, volume-ები)",
                        variable=self.cl_purge).grid(row=n, column=0, columnspan=3, sticky="w", padx=6, pady=(10, 2))
        ttk.Checkbutton(f, text="PostgreSQL სერვერის პაკეტების წაშლაც (native; მოითხოვს მონაცემების წაშლას)",
                        variable=self.cl_pg).grid(row=n + 1, column=0, columnspan=3, sticky="w", padx=6, pady=2)
        b = ttk.Button(f, text="მთელი სტეკის გასუფთავება", command=lambda: self.do_clean(None))
        b.grid(row=n + 2, column=0, sticky="w", padx=6, pady=8)
        self.clean_btns.append(b)
        ttk.Label(f, text="Kubernetes-ზე მთელი სტეკის გასუფთავება შლის namespace-ს, ანუ ყველა volume-საც.").grid(
            row=n + 3, column=0, columnspan=3, sticky="w", padx=6)

    def _tab_ops(self, nb):
        f = self._tab(nb, "დიაგნოსტიკა")
        ttk.Label(f, text="იყენებს „სამიზნე / Deploy“ ჩანართში არჩეულ სამიზნეს და „სად“-ს. "
                          "მიუთითებს სტატუსს, IP:პორტს და რეალურ პასუხს.").pack(anchor="w", padx=4, pady=2)
        top = ttk.Frame(f)
        top.pack(fill="x", pady=4)
        ttk.Button(top, text="განახლება", command=self.ops_refresh).pack(side="left", padx=2)
        self.ops_live = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="ცოცხლად (ყოველ 5 წმ)", variable=self.ops_live, command=self._ops_tick).pack(side="left", padx=8)
        self.ops_info = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.ops_info).pack(side="left", padx=8)

        cols = ("state", "auto", "pid", "listen", "checks")
        self.ops_tree = ttk.Treeview(f, columns=cols, height=9)
        self.ops_tree.heading("#0", text="პროგრამა")
        self.ops_tree.column("#0", width=170)
        for c, t, w in (("state", "სტატუსი", 80), ("auto", "auto", 50), ("pid", "PID", 60),
                        ("listen", "IP:პორტი", 330), ("checks", "შემოწმება", 260)):
            self.ops_tree.heading(c, text=t)
            self.ops_tree.column(c, width=w, anchor="w")
        for st, col in (("running", "#1b7f3b"), ("failed", "#b00020"), ("stopped", "#c98a00"),
                        ("starting", "#0b79b5"), ("missing", "#7a8794")):
            self.ops_tree.tag_configure(st, foreground=col)
        self.ops_tree.pack(fill="x", pady=4)
        self.ops_tree.bind("<<TreeviewSelect>>", self._ops_select)
        self.ops_detail = tk.Text(f, height=6, state="disabled", font=("monospace", 9))
        self.ops_detail.pack(fill="x", pady=2)

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=4)
        for text, act in (("გაშვება", "start"), ("გაჩერება", "stop"), ("რესტარტი", "restart"),
                          ("ავტოგაშვება: ჩართვა", "enable"), ("ავტოგაშვება: გამორთვა", "disable")):
            b = ttk.Button(bar, text=text, command=lambda a=act: self.ops_do(a, self._ops_selected()))
            b.pack(side="left", padx=2)
            self.clean_btns.append(b)
        ttk.Button(bar, text="ლოგი", command=lambda: self.ops_logs(False)).pack(side="left", padx=(12, 2))
        ttk.Button(bar, text="ლოგი ცოცხლად", command=lambda: self.ops_logs(True)).pack(side="left", padx=2)
        bar2 = ttk.Frame(f)
        bar2.pack(fill="x")
        for text, act in (("ყველას გაშვება", "start"), ("ყველას რესტარტი", "restart"), ("ყველას გაჩერება", "stop")):
            b = ttk.Button(bar2, text=text, command=lambda a=act: self.ops_do(a, None))
            b.pack(side="left", padx=2)
            self.clean_btns.append(b)
        self.ops_rows: dict[str, dict[str, Any]] = {}
        self.ops_loading = False

    # ------------------------------------------------------------ diagnostics
    def _ops_cfg(self):
        cfg = self._collect_checked()
        if not cfg:
            return None
        mode, ssh, where = self._ssh_mode()
        return config.resolve(cfg, generate_secrets=False), mode, ssh

    def _ops_selected(self):
        sel = self.ops_tree.selection()
        if not sel:
            messagebox.showinfo("დიაგნოსტიკა", "აირჩიე პროგრამა სიიდან")
            return None
        return list(sel)

    def _ops_select(self, _e=None) -> None:
        sel = self.ops_tree.selection()
        r = self.ops_rows.get(sel[0]) if sel else None
        lines = []
        if r:
            lines = [f"{r['name']}  [{r['unit']}]  {r['detail']}", f"since: {r['since'] or '-'}  restarts: {r['restarts']}"]
            lines += [f"listen: {x}" for x in r["listen"]] or ["listen: -"]
            lines += [f"check:  {'OK ' if c['ok'] else 'BAD'} {c['label']} -> {c['code']}" for c in r["checks"]]
            lines += [f"url:    {u}" for u in r["urls"]]
        self.ops_detail.configure(state="normal")
        self.ops_detail.delete("1.0", "end")
        self.ops_detail.insert("1.0", "\n".join(lines))
        self.ops_detail.configure(state="disabled")

    def ops_refresh(self) -> None:
        if self.ops_loading:
            return
        got = self._ops_cfg()
        if not got:
            return
        cfg, mode, ssh = got
        self.ops_loading = True
        self.ops_info.set("…")

        def run() -> None:
            try:
                res = ops.status(cfg, mode, ssh)
                self.after(0, lambda: self._ops_show(res, None))
            except (deploy.DeployError, ValueError) as e:
                self.after(0, lambda: self._ops_show(None, str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _ops_show(self, res, err) -> None:
        self.ops_loading = False
        if err:
            self.ops_info.set("შეცდომა: " + err)
            return
        self.ops_info.set(f"{res['target']} · {res['domain']} · {res['time']} ({res['took']} წმ)")
        keep = self.ops_tree.selection()
        self.ops_tree.delete(*self.ops_tree.get_children())
        self.ops_rows = {r["id"]: r for r in res["rows"]}
        for r in res["rows"]:
            auto = {True: "yes", False: "no", None: "-"}[r["enabled"]]
            checks = "  ".join(f"{'✔' if c['ok'] else '✘'}{c['label']} {c['code']}" for c in r["checks"]) or "-"
            self.ops_tree.insert("", "end", iid=r["id"], text=r["name"], tags=(r["state"],),
                                 values=(r["state"], auto, r["pid"] or "-", ", ".join(r["listen"]) or "-", checks))
        for k in keep:
            if self.ops_tree.exists(k):
                self.ops_tree.selection_add(k)
        self._ops_select()

    def _ops_tick(self) -> None:
        if self.ops_live.get():
            self.ops_refresh()
            self.after(5000, self._ops_tick)

    def ops_do(self, action: str, comps) -> None:
        if self.busy or (comps is None and action not in ("start", "stop", "restart")):
            return
        if comps is not None and not comps:
            return
        got = self._ops_cfg()
        if not got:
            return
        cfg, mode, ssh = got
        what = ", ".join(comps) if comps else "მთელი სტეკი"
        if action in ("stop", "restart") and not messagebox.askyesno(action, f"{action}: {what}\nგავაგრძელო?"):
            return
        self.busy = True
        self.deploy_btn.state(["disabled"])
        for b in self.clean_btns:
            b.state(["disabled"])

        def run() -> None:
            try:
                for line in ops.control(cfg, mode, ssh, action, comps):
                    self._log(line)
                self._log("✔ დასრულდა")
            except deploy.DeployError as e:
                self._log(f"ERROR: {e}")
            finally:
                self.log_q.put(None)
                self.after(0, self.ops_refresh)
        threading.Thread(target=run, daemon=True).start()

    def ops_logs(self, follow: bool) -> None:
        sel = self._ops_selected()
        if not sel:
            return
        got = self._ops_cfg()
        if not got:
            return
        cfg, mode, ssh = got
        comp = sel[0]
        win = tk.Toplevel(self)
        win.title(f"ლოგი: {comp}{' (ცოცხლად)' if follow else ''}")
        win.geometry("900x500")
        txt = scrolledtext.ScrolledText(win, font=("monospace", 9))
        txt.pack(fill="both", expand=True)
        stop = threading.Event()
        q: queue.Queue[str | None] = queue.Queue()

        def close() -> None:
            stop.set()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)

        def run() -> None:
            try:
                for line in ops.logs(cfg, mode, ssh, comp, 50 if follow else 200, follow, stop):
                    q.put(line)
            except deploy.DeployError as e:
                q.put(f"ERROR: {e}")
            q.put(None)
        threading.Thread(target=run, daemon=True).start()

        def pump() -> None:
            if not win.winfo_exists():
                return
            try:
                while True:
                    line = q.get_nowait()
                    if line is None:
                        return
                    txt.insert("end", line + "\n")
                    txt.see("end")
            except queue.Empty:
                pass
            win.after(200, pump)
        pump()

    # ------------------------------------------------------------ collect
    def _get(self, path: str) -> Any:
        v = self.vars[path].get()
        cur = self.d
        for k in path.split("."):
            cur = cur.get(k, "") if isinstance(cur, dict) else ""
        if isinstance(cur, bool):
            return bool(v)
        if isinstance(cur, int):
            return int(float(v)) if str(v).strip() else 0
        if isinstance(cur, float):
            return float(v)
        if isinstance(cur, list):
            return [json.loads(x) if x.strip().lstrip("-").replace(".", "", 1).isdigit() else x.strip()
                    for x in str(v).replace("[", "").replace("]", "").split(",") if x.strip()]
        return str(v).strip()

    def collect(self) -> dict[str, Any]:
        def put(cfg, path, val):
            cur = cfg
            parts = path.split(".")
            for k in parts[:-1]:
                cur = cur.setdefault(k, {})
            cur[parts[-1]] = val
        cfg: dict[str, Any] = {}
        for path in self.vars:
            if path.startswith("_") or path.startswith("ssh."):
                continue
            put(cfg, path, self._get(path))
        cfg["tls"] = self.tls_map[self.tls_var.get()]
        cfg["components"] = {k: v.get() for k, v in self.checks["comp"].items()}
        cfg["postgres"]["extensions"] = [k for k, v in self.checks["ext"].items() if v.get()]
        cfg["geoserver"]["channel"] = self.ch_map[self.ch_var.get()]
        cfg["geoserver"]["extensions"] = [k for k, v in self.checks["gsext"].items() if v.get()]
        cfg["mapproxy"]["grids"] = [k for k, v in self.checks["grid"].items() if v.get()]
        cfg["mapproxy"]["layers"] = parse_layers(self.layers_txt.get("1.0", "end"))
        cfg["targets"] = [k for k, v in self.checks["target"].items() if v.get()]
        # seed.levels / center are lists of numbers
        s = cfg["mapproxy"]["seed"]
        s["levels"] = [int(x) for x in s["levels"]][:2]
        s["bbox"] = [float(x) for x in s["bbox"]][:4]
        cfg["webmap"]["center"] = [float(x) for x in cfg["webmap"]["center"]][:2]
        return cfg

    def resolved(self) -> dict[str, Any] | None:
        try:
            return config.resolve(self.collect(), persist_secrets=True)
        except (config.ConfigError, ValueError) as e:
            messagebox.showerror("არასწორი კონფიგურაცია", str(e))
            return None

    # ------------------------------------------------------------ actions
    def _log(self, line: str) -> None:
        self.log_q.put(line)

    def _drain(self) -> None:
        try:
            while True:
                line = self.log_q.get_nowait()
                if line is None:
                    self.busy = False
                    self.deploy_btn.state(["!disabled"])
                    for b in self.clean_btns:
                        b.state(["!disabled"])
                    continue
                self.log.configure(state="normal")
                self.log.insert("end", line + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._drain)

    def preview(self) -> None:
        cfg = self.resolved()
        if not cfg:
            return
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = generate.generate_bundle(cfg, Path(td) / "bundle")
            files = {p.relative_to(out).as_posix(): p.read_text("utf-8", "replace")
                     for p in sorted(out.rglob("*"))
                     if p.is_file() and "vendor" not in p.parts and p.suffix not in (".png", ".tgz") and p.stat().st_size < 120_000}
        secrets_ = [cfg["postgres"]["password"], cfg["geoserver"]["admin_password"], cfg["tomcat"]["manager_password"]]
        for k, t in files.items():
            for s in secrets_:
                t = t.replace(s, "••••••••") if s else t
            files[k] = t
        win = tk.Toplevel(self)
        win.title("წინასწარი ნახვა")
        win.geometry("900x600")
        lb = tk.Listbox(win, width=38)
        lb.pack(side="left", fill="y")
        txt = scrolledtext.ScrolledText(win, font=("monospace", 9))
        txt.pack(side="left", fill="both", expand=True)
        names = list(files)
        for n in names:
            lb.insert("end", n)

        def show(_e=None):
            if lb.curselection():
                txt.delete("1.0", "end")
                txt.insert("1.0", files[names[lb.curselection()[0]]])
        lb.bind("<<ListboxSelect>>", show)

    def save_folder(self) -> None:
        cfg = self.resolved()
        if not cfg:
            return
        d = filedialog.askdirectory(title="აირჩიე საქაღალდე")
        if not d:
            return
        out = generate.generate_bundle(cfg, Path(d) / f"gisstack-{cfg['project']}")
        self._log(f"bundle შენახულია: {out}")
        messagebox.showinfo("მზადაა", f"{out}\n\nპაროლები stack.env / .env / secret.yaml ფაილებშია.")

    def _ssh_mode(self):
        mode = self.vars["_mode"].get()
        ssh = {"host": self.vars["ssh.host"].get().strip(), "port": self.vars["ssh.port"].get().strip() or "22",
               "user": self.vars["ssh.user"].get().strip(), "key": self.vars["ssh.key"].get().strip()}
        where = f"{ssh['user']}@{ssh['host']}" if mode == "ssh" else "ამ კომპიუტერზე"
        return mode, ssh, where

    def _run_job(self, cfg, tag, steps) -> None:
        """Run steps(bundle_dir) -> Iterator[str] in a thread, logging to the log pane."""
        self.busy = True
        self.deploy_btn.state(["disabled"])
        for b in self.clean_btns:
            b.state(["disabled"])

        def run() -> None:
            try:
                bundle = deploy.home_dir() / "bundles" / f"{cfg['project']}-{tag}"
                self._log("== bundle-ის გენერაცია")
                generate.generate_bundle(cfg, bundle)
                for line in steps(bundle):
                    self._log(line)
                self._log("✔ დასრულდა")
            except deploy.DeployError as e:
                self._log(f"ERROR: {e}")
            except Exception as e:  # noqa: BLE001
                self._log(f"ERROR: {type(e).__name__}: {e}")
            finally:
                self.log_q.put(None)

        threading.Thread(target=run, daemon=True).start()

    def do_deploy(self) -> None:
        if self.busy:
            return
        cfg = self.resolved()
        if not cfg:
            return
        mode, ssh, where = self._ssh_mode()
        target = cfg["deploy_target"]
        if not messagebox.askyesno("Deploy", f"{target} → {where}\nგავაგრძელო?"):
            return
        if target == "native":
            plan, wd = dict(self.exist_plan), self.exist_data.get()

            def steps(b):
                if plan:
                    def make(c):
                        generate.generate_bundle(c, b)
                        return b
                    yield from existing.run(cfg, target, mode, ssh, plan, make, with_data=wd)
                    return
                rep = discover.scan(cfg, mode, ssh, target)
                if rep["conflicts"]:
                    self.after(0, lambda: (self._exist_show(rep, None), self.nb.select(self.exist_tab)))
                    raise deploy.DeployError("საჭირო პორტები დაკავებულია — აირჩიე რა ვუყოთ არსებულ პროგრამებს (ჩანართი „არსებული პროგრამები“)")
                yield from deploy.deploy(cfg, b, target, mode, ssh)
            self._run_job(cfg, "desktop", steps)
            return
        self._run_job(cfg, "desktop", lambda b: deploy.deploy(cfg, b, target, mode, ssh))

    def do_clean(self, components) -> None:
        if self.busy:
            return
        cfg = self.resolved()
        if not cfg:
            return
        mode, ssh, where = self._ssh_mode()
        target = cfg["deploy_target"]
        purge, rm_pg = self.cl_purge.get(), self.cl_pg.get()
        if rm_pg and not purge:
            messagebox.showerror("Cleanup", "PostgreSQL-ის წასაშლელად ჩართე „მონაცემების წაშლაც“.")
            return
        what = ", ".join(components) if components else "მთელი სტეკი"
        warn = ("⚠ მონაცემებიც წაიშლება და ვერ აღდგება!" + ("\n⚠ PostgreSQL სერვერიც წაიშლება!" if rm_pg else "")
                if purge else "მონაცემები დარჩება.")
        if not messagebox.askyesno("გასუფთავება", f"{target} → {where}\nრა: {what}\n{warn}\nგავაგრძელო?",
                                   icon="warning"):
            return
        self._run_job(cfg, "desktop-clean",
                      lambda b: deploy.clean(cfg, b, target, mode, ssh, components, purge, rm_pg))

    # ------------------------------------------------------------ projects
    def refresh_projects(self, select: str | None = None) -> None:
        names = projects.list_projects()
        self.proj_box["values"] = names
        if select:
            self.proj_var.set(select)
        elif names and not self.proj_var.get():
            self.proj_var.set(names[0])

    def _collect_checked(self) -> dict[str, Any] | None:
        """collect() + validation, without resolving secrets; shows the reason on error."""
        try:
            cfg = self.collect()
            config.resolve(cfg, generate_secrets=False)
            return cfg
        except (config.ConfigError, ValueError) as e:
            messagebox.showerror("არასწორი კონფიგურაცია", str(e))
            return None

    def save_project(self) -> None:
        cfg = self._collect_checked()
        if not cfg:
            return
        try:
            path = projects.save(cfg)
        except projects.ProjectError as e:
            messagebox.showerror("შენახვა", str(e))
            return
        self.refresh_projects(cfg["project"])
        self._log(f"✔ პროექტი შენახულია: {path}")

    def do_save_comp(self, comp: str) -> bool:
        cfg = self._collect_checked()
        if not cfg:
            return False
        try:
            path = projects.save_component(cfg, comp)
        except projects.ProjectError as e:
            messagebox.showerror("შენახვა", str(e))
            return False
        self.refresh_projects(cfg["project"])
        self._log(f"✔ {comp}: შენახულია → {path}")
        return True

    def load_project(self) -> None:
        name = self.proj_var.get()
        if not name:
            return
        try:
            self.apply_config(projects.load_full(name))
        except projects.ProjectError as e:
            messagebox.showerror("ჩატვირთვა", str(e))
            return
        self._log(f"✔ ჩაიტვირთა: {name}")

    def apply_config(self, d: dict[str, Any]) -> None:
        """Fill every widget from a (defaults + saved) config dict."""
        def look(path):
            cur = d
            for k in path.split("."):
                cur = cur.get(k, "") if isinstance(cur, dict) else ""
            return cur
        for path, var in self.vars.items():
            if path.startswith("_") or path.startswith("ssh."):
                continue
            val = look(path)
            if isinstance(var, tk.BooleanVar):
                var.set(bool(val))
            else:
                var.set("" if val in (None, "") else str(val))
        for name, selected in (("comp", [k for k, v in d["components"].items() if v]),
                               ("ext", d["postgres"]["extensions"]), ("gsext", d["geoserver"]["extensions"]),
                               ("grid", d["mapproxy"]["grids"]), ("target", d["targets"])):
            for iid, v in self.checks[name].items():
                v.set(iid in selected)
        self.tls_var.set(next((k for k, v in self.tls_map.items() if v == d["tls"]), self.tls_var.get()))
        self.ch_var.set(next((k for k, v in self.ch_map.items() if v == d["geoserver"]["channel"]), self.ch_var.get()))
        self.layers_txt.delete("1.0", "end")
        self.layers_txt.insert("1.0", layers_to_text(d["mapproxy"]["layers"]))

    def do_apply(self, comp: str) -> None:
        """Save one program's settings, then roll out only that program."""
        if self.busy:
            return
        cfg = self.resolved()
        if not cfg:
            return
        mode, ssh, where = self._ssh_mode()
        target = cfg["deploy_target"]
        if not messagebox.askyesno("გამოყენება", f"{comp}: შენახვა და გამოყენება\n{target} → {where}\n"
                                   "მხოლოდ ეს პროგრამა დაინსტალირდება/გადაიტვირთება. გავაგრძელო?"):
            return
        if not self.do_save_comp(comp):
            return
        self._run_job(cfg, "desktop-apply", lambda b: deploy.apply_component(cfg, b, target, mode, ssh, comp))

    # ------------------------------------------------------------ updates
    def check_update(self) -> None:
        self._log("განახლების შემოწმება GitHub-ზე…")

        def run() -> None:
            try:
                info = updater.check()
                self.after(0, lambda: self._show_update(info))
            except updater.UpdateError as e:
                self.after(0, lambda: messagebox.showerror("განახლება", str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _show_update(self, info) -> None:
        if not info["newer"]:
            messagebox.showinfo("განახლება", f"უკვე უახლესი ვერსიაა ({info['current']}).")
            return
        notes = "\n".join(info["notes"].splitlines()[:12])
        text = (f"ახალი ვერსია: {info['latest']} (შენი: {info['current']})\n\n{notes}\n\n")
        if info["can_auto"]:
            if messagebox.askyesno("განახლება", text + "განვაახლო ახლავე? (ჩამოტვირთვა + SHA-256 შემოწმება + pip)"):
                self._run_update(info)
            return
        import webbrowser
        if messagebox.askyesno("განახლება", text + "ამ დაყენების ავტომატური განახლება შეუძლებელია.\nგავხსნა ჩამოტვირთვის გვერდი?"):
            webbrowser.open(info["url"])

    def _run_update(self, info) -> None:
        if self.busy:
            return
        self.busy = True
        self.deploy_btn.state(["disabled"])

        def run() -> None:
            import tempfile
            try:
                with tempfile.TemporaryDirectory() as td:
                    for line in updater.apply(info, td):
                        self._log(line)
            except updater.UpdateError as e:
                self._log(f"ERROR: {e}")
            finally:
                self.log_q.put(None)
        threading.Thread(target=run, daemon=True).start()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
