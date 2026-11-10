# gis-stack — პროექტის კონტექსტი Claude Code-ისთვის

Python პაკეტი `gisstack`: Jinja2 შაბლონებით აგენერირებს GIS სტეკს (PostgreSQL/PostGIS, Tomcat,
GeoServer, QGIS Server, MapProxy, Caddy, ორენოვანი Leaflet ვებ რუკა, React Native და Flutter)
სამ სამიზნეზე: native (apt/dnf), compose (docker/podman), k8s. GUI: Flask ვებ (`gisstack web`)
და Tkinter (`gisstack desktop`). პაკეტები: `packaging/build.sh deb|rpm`.

## ბრძანებები
- ტესტები: `python3 tests/test_gisstack.py` (16 ტესტი; MapProxy-ის ტესტი ითხოვს `pip install mapproxy`)
- გენერაცია: `python3 -m gisstack generate -c stack.json -o ./bundle`
- პაკეტები: `packaging/build.sh deb` / `rpm`

## მნიშვნელოვანი ფაქტები
- GeoServer 3.0.2 ⇒ Tomcat 11 (Java 17/21); GeoServer 2.28.x ⇒ Tomcat 9; Tomcat 10.1 არ არის მხარდაჭერილი.
- კლიენტების ტაილ-URL უნდა იყოს MapProxy-ის **WMTS REST**
  (`/mapproxy/wmts/<ფენა>/GLOBAL_WEBMERCATOR/{z}/{x}/{y}.png`). TMS მისამართი დონეს +1-ით და Y-ს აბრუნებს
  (`origin: nw` არ ეხმარება) — გადამოწმებულია MapProxy 7.0.0-ზე.
- Caddy `handle_path /mapproxy/*` + `header_up X-Script-Name /mapproxy`.
- ვებ GUI: token + `X-GisStack: 1` (CSRF), მხოლოდ 127.0.0.1:8765.

## გადამოწმებულია (2026-11-04)
- `native/install.sh` + `uninstall.sh`: Ubuntu 24.04 და Rocky 9.8 (SELinux enforcing), QEMU/KVM VM-ზე: სრული ინსტალაცია,
  განმეორებითი გაშვება, ცალკეული კომპონენტის გასუფთავება, `--purge`, `--purge --remove-postgres`, ხელახალი ინსტალაცია.
- Compose (`up.sh up|clean`), k8s (`kubectl apply -k` kind-ზე, `apply.sh clean`), Flutter `analyze`, Expo `export`,
  Flask და Tkinter GUI (გასუფთავების ღილაკებით).

## რა არ არის გადამოწმებული
1. Debian 12, Fedora, AlmaLinux; PostgreSQL 16/18; `QGIS_OFFICIAL_REPO=1`
2. APK/iOS ბილდი; Expo `MapLibreScreen.js`
3. k8s `cleanup` რეალურ კლასტერზე (მხოლოდ stub-ით ტესტირდება); Caddy LoadBalancer
4. QGIS Server რეალური პროექტით (`/qgis/?MAP=...`) — ახლა მხოლოდ „პროექტი არ არის“ პასუხია

## გასუფთავება (cleanup)
- ბირთვი: `deploy.clean()`; CLI `gisstack clean [--only a,b] [--purge] [--remove-postgres] [-y]`;
  Web `POST /api/clean`; Tk ჩანართი „გასუფთავება“. სკრიპტები: native `uninstall.sh`, compose `up.sh clean`, k8s `apply.sh clean`.
- `--purge` შლის მონაცემებს (ბაზა, GeoServer data, ქეშები, volume-ები). PostgreSQL სერვერს მხოლოდ `--remove-postgres` შლის
  და უარს ამბობს, თუ სხვა ბაზებიც არის (`--force-postgres`).
- k8s-ზე მთელი სტეკის გასუფთავება შლის namespace-ს (ანუ ყველა volume-საც).
- გენერირებული პაროლები ინახება `~/.gisstack/secrets/<project>.json`-ში (600) და თავიდან გამოიყენება.

## პროგრამების კონფიგურაცია / შენახვა / გამოყენება
- ყოველ პროგრამას (PostGIS, Tomcat, GeoServer, QGIS, MapProxy, Caddy, ვებ რუკა) GUI-ში აქვს ზოლი: „ჩართულია“ + „შენახვა“ +
  „შენახვა და გამოყენება“. შენახვა წერს მხოლოდ ამ პროგრამის სექციებს `~/.gisstack/projects/<პროექტი>.json`-ში
  (`projects.SECTIONS`); გამოყენება უშვებს მხოლოდ მას: native `install.sh --only <ნაბიჯი>`, compose `up.sh only <სერვისი>`,
  k8s `apply.sh only <კომპონენტი>`. ვებ რუკა compose/k8s-ში Caddy-ს ეკუთვნის.
- CLI: `gisstack save [--component X]`, `gisstack apply X [--save]`, `gisstack projects`; Web: `/api/save`, `/api/apply`,
  `/api/projects[/<name>]`. პროექტის ფაილი ჩვეულებრივი კონფიგია (`-c` იღებს).
- გადამოწმებულია Ubuntu VM-ზე: MapProxy-ის workers 4→2, Tomcat/Caddy არ გადაიტვირთა.

## დიაგნოსტიკა (`gisstack/ops.py`)
- `ops.status()` ერთი ბრძანებით (`bash -c`, ლოკალურად ან SSH) კითხულობს: native — `systemctl show` + `ss -Hltn` + curl/tcp
  შემოწმებები; compose — `compose -p <პროექტი> ps --format json` + `inspect`; k8s — `kubectl get pods,svc,deploy,sts -o json`.
  პარსერები (`parse_native/parse_compose/parse_k8s`) წმინდაა და ტესტირდება მზა ტექსტებზე.
- `control()` — start/stop/restart/enable/disable (native: systemd, stop-ის დროს უკუ-რიგით; compose: `compose -p`, enable/disable =
  `update --restart`; k8s: scale / rollout restart); `logs()` — journalctl / compose logs / kubectl logs, `-f` გაჩერდება `stop` Event-ით.
- ინტერფეისები: CLI `status|start|stop|restart|enable|disable|logs`; Web `/api/ops/status|control|logs`, `/api/jobs/<id>/cancel`;
  Tk ჩანართი „დიაგნოსტიკა“. გადამოწმებულია Ubuntu და Rocky VM-ზე (ssh, არა-root მომხმარებელი + `sudo -n`).
- Flask ინახავს შაბლონებს ქეშში — HTML-ის შეცვლის შემდეგ ვებ სერვერი თავიდან გაუშვი.

## არსებული პროგრამები (`discover.py`, `existing.py`)
- `discover.scan()` ერთი root სკრიპტით (dpkg/rpm, systemctl, ss, psql) აგროვებს ფაქტებს; `parse/analyse/conflicts` წმინდაა და ტესტირდება
  მზა ტექსტებზე. „ჩვენია“ = `gisstack-*` unit-ები / `/etc/gisstack/stack.env`; დარჩენილი ფაილები (წაშლილი პაკეტის /var/lib/...) უცხო პროგრამა არ არის.
- `existing.run()` = scan → plan-ის შემოწმება (`validate`) → ბექაფი → წაშლა → `deploy(extra_args=["--adopt-postgres"])` → იმპორტი.
  სკრიპტებში პაკეტების/ბაზების სახელები regex-ით მოწმდება და `shlex.quote`-ით ჩაიწერება; ბექაფის გზები მუდმივებია.
- Debian-ზე `apt remove` ტოვებს unit-ს **masked**-ად (symlink → /dev/null) — `systemctl unmask` + `clear_mask` install.sh-ში;
  `apt purge postgresql-*` კლასტერებს შლის, ამიტომ purge მხოლოდ ექსპლიციტურად; `/etc/postgresql` გვერდზე გადაინაცვლებს, თორემ ახალი კლასტერი 5433-ზე ავა.
- `psql -c "a; b"` ≤v14-ზე მხოლოდ ბოლო შედეგს ბეჭდავს — გამოიყენე stdin; Rocky-ზე PG პროცესის სახელი `postmaster`-ია.
- დამპი root-ისაა (600): `pg_restore ... < "$f"` root-ის shell-იდან.

## ენა და თემა (`i18n.py`, `settings.py`)
- წყარო ენაა ქართული; `i18n.EN` = ქართული → ინგლისური. `tr()` ჯერ მთელ სტრიქონს ეძებს, მერე ფრაგმენტებს ქართული სიტყვის საზღვრებზე
  (ამიტომ „ბექაფი: postgis“ სწორად ითარგმნება). ახალი ტექსტი **უნდა** ჩაემატოს, თორემ `I18nTests` ჩავარდება.
- ვებ: გვერდი ფრაგმენტებს თარგმნის ბრაუზერში (`MutationObserver` + `/api/i18n`), ორიგინალი ინახება და ენის გადართვისას აღდგება;
  `#view` (ფაილის გადახედვა) არ ითარგმნება. თემა — CSS ცვლადები + `html[data-theme]`; პარამეტრები — `/api/settings`.
- Tk: ttk ვიჯეტების `text=`, `Notebook.add`, `Treeview.heading`, `messagebox`, `title()` გატარებულია `tr()`-ში ერთ ადგილას; ენა/თემა გადატვირთვით
  (`os.execv`). `ttk.Style("clam")` + `recolor()` tk ვიჯეტებისთვის.
- `desktop.LANG` ტესტში პირდაპირ ისმება; `importlib.reload(desktop)` არ გამოიყენო — monkeypatch-ები რეკურსიაში გადადის.

## უსაფრთხოება (Phase 1a)
- ყველა კონფიგურაციის ველს აქვს წესი `validation.py`-ში; ახალი ველი = ახალი წესი + `InjectionFuzzTests` ავტომატურად ფარავს (ტექსტურ ველებს).
- შაბლონებში ყოველი მნიშვნელობა კონტექსტის ფილტრით: `shq` (მთელი სიტყვა), `shdq` ("..."), `shdw` ("${V:-...}"; ბაშში `'` ამ კონტექსტში ახალ ციტატას ხსნის → `"'"`), `yq` (YAML).
- საიდუმლოს ჩაწერა მხოლოდ `fsutil.write_private/write_file`-ით; საქაღალდე `fsutil.private_dir`-ით. პირდაპირი `write_text`+`chmod` არ გამოიყენო.
- ბექაფის სკრიპტი ვერასოდეს ბეჭდავს `BACKUP_VERIFIED=`-ს ჩავარდნისას; `existing.run` მხოლოდ ამ მარკერზე გადადის წაშლაზე. `GS_BACKUP_ROOT`/`GS_PGRUN` ტესტებისთვისაა.
- `ss` პროცესის სახელს 15 სიმბოლომდე ჭრის — შედარება prefix-ით (`discover._is_ours_proc`).

## საიმედოობა და ვებ-უსაფრთხოება (Phase 1b)
- ყველა გარე ბრძანება `runner.py`-ით: `stream()` (ვადა/უმოქმედობა/გაუქმება/process-group kill/დამალვა), `run(..., input_text=)` — საიდუმლო მხოლოდ stdin-ით. `ops._run/_stream` და `deploy._stream` მასზეა გადაყვანილი.
- `journal.py`: `Operation` (ჟურნალი `~/.gisstack/journal.jsonl`), `exclusive()` (ლოკალური flock); სერვერზე `flock -n -E 75 /tmp/gisstack-op.lock` (`deploy.REMOTE_LOCK`). native bundle სერვერიდან იშლება.
- `preflight.py`: `script()` (მხოლოდ წაკითხვა) → `parse()` → `evaluate()` (წმინდა); `deploy` CLI/ვებ-ში გეითია. ახალი შემოწმება = ახალი ფაქტი scripts-ში + წესი evaluate-ში + ტესტი.
- `webgui/security.py`: Sessions/RateLimiter/AuditLog/host_allowed/summarize. აუდიტში მხოლოდ `SAFE_BODY_KEYS` — ახალი ველი ნაგულისხმევად არ ჩაიწერება.
- `config.SCHEMA_VERSION` + `MIGRATIONS` (key რენეიმი/გადატანა = ახალი მიგრაცია); `secretsrc.py` (env / `_FILE` / რეგისტრირებადი პროვაიდერი).
- `rotation.py`: პაროლი stdin-ით, **ჯერ გადამოწმება, მერე ფაილებში ჩაწერა**, ჩავარდნისას უკან; GeoServer ცვლილებას წამიერად აგვიანებს (verify ციკლი), ძველი პაროლი ქეშის გამო რამდენიმე წუთი კიდევ მუშაობს.
- ტესტებში watcher-ის შაბლონი უნდა ვერ ემთხვეოდეს საკუთარ cmdline-ს (`Mark[e]r`), თორემ ყველაფერი „გაჟონვაა“.

## არქიტექტურა (Phase 2)
- ფენები: `core/*` (errors, paths, events, log) < `runner` < `transport` < `providers/*` < `ops`/`deploy`/`preflight`/`rotation`/`existing` < `services` < `cli`/`webgui`/`desktop`. `tests/test_architecture.py` იცავს (ციკლი, ფენა, სხვის private-ზე წვდომა).
- ახალი სამიზნე = ახალი `providers/<name>.py` (ENTRY, CLEAN_ENTRY, ROOT, fetch_status, control_script, logs_script, clean_args, apply_args) + რეგისტრაცია `providers/__init__.py`. `ops`/`deploy` არ იცვლება.
- ქცევის შეცვლა განზრახ? `UPDATE_GOLDEN=1 python -m unittest tests.test_golden` და გადახედე `tests/golden/*.json` diff-ს.
- ახალი გამონაკლისი = `core/errors.py` (უნიკალური `code`); ძველი სახელები (`deploy.DeployError` ...) იგივე კლასებია.
- Tk-ის თარგმნა: `desktop.install_translation()` (App.__init__ იძახებს); იმპორტზე არაფერი იცვლება.

## ბექაფი / აღდგენა (Phase 7)
- ლოგიკა სერვერზეა: `templates/native/gisstack-backup.sh` (სტატიკური bash, shellcheck-ში ამოწმდება); Python (`backup.py`) მხოლოდ აყენებს (`install_tool`, sha256-ით) და ატარებს `transport`-ით. ახალი ბრძანება = ახალი ტესტი `tests/test_backup_tool.py`-ში (რეალური bash/tar, `Box` ფიქსტურა; `GS_*` გადაფარვები root-ის გარეშე გასაშვებად).
- `tests/test_backup_tool.py::PostgresRoundTripTests` ითხოვს docker + `postgis/postgis:17-3.5` (CI-ში `GISSTACK_TEST_DOCKER=1`).
- ყოველთვის: `.partial` → გადამოწმება → გადარქმევა; აღდგენა = pre-restore ბექაფი → გადარქმევით ჩანაცვლება → ვალიდაცია → rollback. არასდროს წაშალო ძველი მონაცემი ავტომატურად.
- tar 1.35 არ წერს აბსოლუტურ symlink-ზე (`/opt/tomcat`) — არქივში რეალური ბილიკებია (`readlink -f`).
- `docs/RECOVERY.md` — runbook; ქცევის ცვლილებისას განაახლე.

## მონიტორინგი (Phase 8)
- `monitoring/collect.py`: ერთი read-only სკრიპტი (`script()`) → სუფთა პარსერები (`parse`, `host_metrics`, `postgres_metrics`...) → `build_sample` → `flatten` (მეტრიკა = (სახელი, labels, მნიშვნელობა)). ფიქსტურები `tests/fixtures/monitor/` — **რეალური** სერვერის გამოტანა; ახალი მაჩვენებელი = სკრიპტში ახალი სექცია + პარსერი + ტესტი ფიქსტურაზე. წყაროს არ ყოფნა → არ არის მეტრიკა + `errors`, არასოდეს გამოგონილი რიცხვი.
- `store.py` (SQLite ring buffer), `alerts.py` (state machine; `cause_of` = ერთი ავარია ერთი შეტყობინება; ახალი წესი უნდა ეყრდნობოდეს `flatten`-ის მეტრიკას — ამას ტესტი ამოწმებს), `notify.py`, `service.Monitor` (tick/watch).
- Feature flag-ები `core/features.py`; ახალი დიდი ფუნქცია = ახალი flag, ნაგულისხმევად გამორთული.
- `docs/MONITORING.md` — მომხმარებლის აღწერა.

## ვებ დაფა (Phase 3)
- `webgui/static/dashboard/` (ES modules, build-ის გარეშე) + `templates/dashboard.html`; მკაცრი CSP მხოლოდ `/dashboard` და `/static/dashboard/*`-ზე. **არ გამოიყენო** `innerHTML`, inline `style=`/`on*=`, `eval` — `tests/test_dashboard.py` ამოწმებს. DOM-ს აშენებს `ui.h()` (მონაცემი ტექსტურ node-ად მიდის).
- ყველა მოთხოვნა `api.js`-ით (CSRF header). ახალი ქართული ტექსტი → `i18n.py` (JS-იც სკანირდება). ახალი view = `views/<x>.js` (`export default {title, async mount(root, {arg, params}) -> {refresh?, destroy?}}`) + რეგისტრაცია `app.js`-ში + nav.
- სუფთა ლოგიკა (`format`, `chart`, `i18n.createTranslator`, `groupSeries`, `configSummary`, `summarize`) — `tests/js/pure.test.mjs` (`node --test`). რეალური ბრაუზერით შეამოწმე (preview) ვიზუალი/ენის გადართვა; ენის გადართვისას `setText` ქეში უნდა განულდეს.
- კავშირი (პროექტი/რეჟიმი/SSH) ინახება მხოლოდ `sessionStorage`-ში.

## API და პლაგინები (Phase 10)
- ახალი ოპერაცია = ახალი მარშრუტი `api/routes*.py`-ში (`@R(method, path, summary, scope, body=…, response=…)`); სქემა ამოწმებს მოთხოვნას, ცხრილი აგენერირებს OpenAPI-ს. შემდეგ: `python -m gisstack api openapi -o docs/openapi.json && python -m gisstack api docs -o docs/API.md` (ტესტი ამოწმებს drift-ს). დაფის JS იძახებს მხოლოდ `/api/v1/*`.
- ტოკენის სკოუპი: წაკითხვა `read`, ოპერაცია `operate`, დესტრუქციული/კონფიგურაცია `admin`. ტესტი `test_scope_matrix_over_every_route` ყველა მარშრუტს ამოწმებს.
- პლაგინები: `gisstack/plugins/` (`contract` არაფერს არ ანაწილებს gisstack-იდან, `loader` იზოლირებს ჩავარდნებს, `kit` ავტორებისთვის). ყველა გამოძახება `plugins.safe_call`-ით.

## GeoServer მენეჯერი (Phase 4)
- `gisstack/geoserver/`: `gateway` (curl 127.0.0.1-დან transport-ით, პაროლი stdin-ზე, `data-raw` არასდროს `data-binary`), `manager` (REST/OGC, სახელების ვალიდაცია, XML-ს DTD-ს უარყოფს); მარშრუტები `api/routes_geoserver.py`; გვერდი `views/geoserver.js`. ახალი ოპერაცია = ფუნქცია `manager.py`-ში + მარშრუტი + ტესტი fake `Gateway`-ით და (თუ შეიძლება) რეალურ კონტეინერზე.

## PostGIS მენეჯერი (Phase 5)
- `gisstack/postgis/`: `sqlsplit` (მხოლოდ UX, **არა** უსაფრთხოების საზღვარი), `server` (`ScriptGateway`: psql სერვერზე, SQL stdin-ზე base64, **ყოველთვის** ფიქსირებული `SET …;` პრეფიქსით — `psql -c '\! cmd'` ასრულებს shell-ს), `manager` (inventory/health/query/execute/Elevations). საზღვარი = როლი `gisstack_ro` + read-only ტრანზაქცია. ახალი ოპერაცია = ტესტი რეალურ PostGIS კონტეინერზე (shim `psql` + `GS_PGRUN=""`, `GS_PG_PWFILE`; pg_hba-ში loopback უნდა გახდეს scram, სურათში `trust`-ია).
- JS ფაილის კომენტარში არ დაწერო `/*` (i18n ტესტი მას ბლოკ-კომენტარად აღიქვამს და ფაილს გამოტოვებს).

## MapProxy მენეჯერი (Phase 6)
- `gisstack/mapproxy/`: `server` (`ScriptGateway`: სკრიპტები სერვერზე, `GS_MP_*` გარემოს ცვლადები მხოლოდ ტესტებისთვის, ფაილის შიგთავსი stdin-ზე base64), `manager` (yaml ოპერაციები, seed-ის დაცვა, სტატისტიკა). **MapProxy-ის `load_configuration` ნაგულისხმევად მხოლოდ აფრთხილებს** — ვალიდატორი `ignore_warnings=False` + `configured_services()`-ს იყენებს. `mapproxy-seed` ნაგულისხმევად ყველა ფილას თავიდან იღებს — ჩვენ `refresh_before: 2000-01-01`-ს ვსვამთ. `pgrep -f` საკუთარ `bash -c` სკრიპტს ემთხვევა, თუ სკრიპტში ტექსტი პირდაპირ წერია (`[m]approxy-seed` ხრიკი + `mapproxy"-seed`). `journal.Operation(op, **meta)` — meta-ში `op=` არ გამოიყენო.

## გაკვეთილები
- EL9: `python3` = 3.9, MapProxy 7 ითხოვს 3.10+ (ვიყენებთ `python3.12`); `spawn-fcgi` არ არის EL9-ში → QGIS FastCGI სოკეტს systemd აძლევს.
- `dnf list --available` არ აჩვენებს უკვე დაყენებულ პაკეტს — იდემპოტენტურობისთვის `dnf list` გამოიყენე.
- VM-ის QEMU-ს `kill` = დენის გამორთვა (0-ბაიტიანი ფაილები); ჯერ `poweroff`.
- Bash heredoc-ში `EOF` შიდა ტექსტი ხურავს გარე heredoc-ს — გამოიყენე უნიკალური ტერმინატორი.
- `pkill -f` შეიძლება საკუთარ shell-ს მოკლას — გამოიყენე PID.
- JSX `{{ }}` ეჯახება Jinja-ს; RN შაბლონებში StyleSheet გამოიყენე.

## მრავალი სერვერი (Phase 9, flag `multi_server`)
- `gisstack/servers/{store,hostkey,service}.py`, `api/routes_servers.py`, `views/servers.js`, `tests/test_servers.py`, `docs/SERVERS.md`.
- პროფილები: `~/.gisstack/servers.json` (0600) + `known_hosts/<id>` (0600). ინახება მხოლოდ გასაღების **გზა**; „PRIVATE KEY", ახალი ხაზი ან ფარდობითი გზა უარყოფილია. პაროლი/passphrase არ არსებობს (BatchMode, `sudo -n`).
- `transport.ssh_base`: თუ `ssh["known_hosts"]` არის, გამოიყენება `StrictHostKeyChecking=yes` + `UserKnownHostsFile=<ის> ` + `GlobalKnownHostsFile=/dev/null`; კლიენტის `ssh` სქემა `known_hosts`-ს არ იღებს (additionalProperties=false), მხოლოდ `store.ssh_for(id)` ამატებს.
- `routes.machine()` ერთადერთი ადგილია, სადაც `server` იხსნება: `mode` უნდა იყოს `ssh`, უფიქსირებელი სერვერი უარყოფილია, ბადიში მოსული `ssh` იგნორირებულია. ახალი მანქანის მარშრუტი ავტომატურად მუშაობს სერვერებზე, თუ `machine()`-ს იყენებს.
- host key-ის დაფიქსირება: scan არაფერს ენდობა; pin ითხოვს ანაბეჭდს (`SHA256:...`), რომელსაც ოპერატორი სერვერის კონსოლიდან ადარებს; არსებულის ჩანაცვლებას `replace=true` (UI: აკრიფე REPLACE) სჭირდება.
- ისტორია იკითხება ჟურნალიდან `host`-ის მიხედვით (ერთი მისამართის ორი პროფილი ერთსა და იმავე ისტორიას ხედავს).
- ტესტებში რეალური `sshd` უნდა გაეშვას 127.0.0.1-ზე უპრივილეგიოდ (`-D -e -f`), `StrictModes no`, `UsePAM no`; თუ ვერ ეშვება, ტესტი skip-დება.

## ინტეგრაციული ტესტები (Phase 11b)
- `tests/integration/{sysd,test_installer,test_packages,test_compose}.py`, `tests/e2e/test_browser.py`, `.github/workflows/integration.yml`, `docs/TESTING.md`. ყველა opt-in (`GISSTACK_TEST_INSTALLER/DOCKER/E2E=1`), ჩვეულებრივ გაშვებაზე skip-დება.
- gis-stack-ის საკუთარ კოდს კონტეინერზე ვუშვებთ `transport.argv`-ის patch-ით (`Box.argv`): `mock.patch.object(transport, "argv", lambda mode, ssh, script, *, root: box.argv(script))`.
- RPM-ზე `pg_isready` PATH-ში არ არის: PostgreSQL-ის მუშაობას ვამოწმებთ `runuser -u postgres -- psql -X -Atqc "select 1"`-ით. PGDG repo-ს `$releasever_major.$releasever_minor` install.sh-ში `$releasever_major`-ზე იცვლება.
- Playwright: dashboard-ზე პირველი ვიზიტი „კავშირი“ დიალოგს ხსნის (დახურე `#dlg button.primary`-ით); CSP-ის გამო `wait_for_function` (eval) არ მუშაობს, გამოიყენე სელექტორები.
- ახალი golden ფაილები: `UPDATE_GOLDEN=1 python -m unittest tests.test_golden`.

## გამოშვების ხარისხი (Phase 12)
- ხელმოწერა: `release.yml` → `cosign sign-blob --bundle SHA256SUMS.sigstore.json SHA256SUMS` (keyless, `id-token: write`) + იმავე workflow-ში verify. `updater.verify_signature` იძახებს `cosign verify-blob`-ს იდენტობით `SIGNER_IDENTITY` (მხოლოდ ამ რეპოს release.yml, ვერსიის tag). არასწორი ხელმოწერა ყოველთვის უარყოფს; არარსებობა/cosign-ის უქონლობა — გაფრთხილება, `GISSTACK_REQUIRE_SIGNATURE=1`-ზე უარყოფა. ტესტებში cosign ყალბია (`tests/test_signature.py`); რეალური ხელმოწერა მხოლოდ tag-ზე გადის (v0.1.5 რეალური cosign-ით გადამოწმდა). გაკვეთილი: release workflow უშვებს ტესტებს ubuntu-latest-ზე, სადაც `$USER` არ არის დაყენებული — ტესტებში გამოიყენე `getpass.getuser()`; ჩავარდნილი tag წაშალე (`git tag -d vX && git push origin :refs/tags/vX`), გაასწორე, თავიდან დადე.
- ინსტრუქციები: `docs/guides/{en,ka}/*.md`; `tests/test_guides.py` ითხოვს ერთნაირ სათაურებს, **ერთნაირ კოდის ბლოკებს**, არსებულ ბმულებს და არსებულ CLI ბრძანებებს. ახალი ინსტრუქცია = ორივე ენა.
- `updater.py` არის i18n ტესტის სკანირებაში: ახალი ქართული შეტყობინება → `EN` ჩანაწერი.

## დაშიფრული ასლები
- `gisstack/backup_crypto.py` (gpg), `backup.download/upload/offsite(..., encrypt/passphrase)`, CLI `--encrypt/--passphrase-file/--encrypt-to`, `backup decrypt`. დაშიფვრა მხოლოდ **ადგილობრივ** ასლზეა, `gisstack-backup` და სერვერი უცვლელია (სერვერზე ცოცხალი საიდუმლოებებიც დგას, ამიტომ იქ დაშიფვრა ცოტას მატებს).
- პაროლი არასოდეს argv/env-ში გადაეცემა gpg-ს: `--passphrase-fd 0`. recipient იწყება ასოთი/ციფრით (არასოდეს `-`), რომ gpg-ს პარამეტრად ვერ იქცეს.
- `gpg --list-packets` უნდა გაეშვას `--list-only`-ით (სხვაგვარად agent-ს ელოდება და 60 წმ იკიდება); gpg 2.4 იყენებს AEAD პაკეტს („aead encrypted packet“), ძველი — MDC-ს.
- ტესტებში GNUPGHOME ხანმოკლე გზაზე (`/tmp/gn…`: agent-ის socket-ს სიგრძის ლიმიტი აქვს) და `gpgconf --kill all` cleanup-ში.
- OpenSSH 9.8+ drops connections from a source that connected without authenticating (ssh-keyscan) - "Connection reset"; throw-away test sshd must set PerSourcePenalties no. Check against a newer sshd: docker run debian:trixie (OpenSSH 10).
- release.yml installs cosign itself (pinned sha256, curl retries); sigstore/cosign-installer failed repeatedly with curl exit 22 during a GitHub release-download outage (504).

## მენეჯერები Compose/K8s-ზე
- `gisstack/targets.py`: `Place.prelude()` განსაზღვრავს `gs_in USER CMD...` ბაშ ფუნქციას (Compose: `docker exec -i -u USER -e PG...`, container ლეიბლებით; K8s: `kubectl exec -i TYPE/NAME --`, USER და env **იგნორირებულია**). კონტეინერის არარსებობა → exit 98 + ნათელი შეტყობინება.
- PostGIS `ScriptGateway(place)`: კონტეინერში superuser = `cfg.postgres.user` (`gs_in postgres env PGUSER=...`), ro-პაროლის ფაილი `$HOME/.gisstack/postgis-ro-<project>.pw`, `root=False` (sudo-ს root-ის ცარიელი kubeconfig/docker ექნებოდა); K8s-ზე `PGCONNECT_TIMEOUT`/`PGOPTIONS` გადაეცემა `env`-ით pod-ის შიგნით.
- GeoServer `CurlGateway(place)`: მთელი curl სკრიპტი `gs_in '' bash -c <quoted>`-ით კონტეინერის შიგნით (127.0.0.1:8080), კონფიგი stdin-ზე.
- MapProxy `PlaceGateway`: `_sh` = კონტეინერის შიგნით (`INNER_PRELUDE`: GS_MP_* გადაწერა, `pgrep` ჩანაცვლება — `$$` და `$BASHPID` გამოირიცხება, curl-ის ნაცვლად python); Compose-ზე კონფიგი/ასლები ჰოსტზე `$WD/mapproxy` (`com.docker.compose.project.working_dir` ლეიბლი); K8s-ზე რედაქტირება უარყოფილია (ConfigMap). გაუქმებული seed კონტეინერში უნდა მოიკლას (docker/kubectl კლიენტის მოკვლა საკმარისი არ არის).
- ტესტები: `tests/integration/test_k8s_managers.py` (რეალური kind კლასტერი, `GISSTACK_TEST_K8S=1`; ლოკალურად kind+kubectl ბინარები ჩამოიტვირთება GitHub-იდან, node image `kindest/node`; `kind load docker-image` ხშირად ჩავარდება multi-arch-ზე — pod-ები თავად ჩამოტვირთავენ image-ებს), `tests/test_targets.py` (ყალბი docker/kubectl), `tests/integration/test_compose_managers.py` (რეალური Compose + kubectl shim). pgrep-ის ტესტში ყოველთვის `[m]approxy-seed` ფორმა, თორემ საკუთარ ბრძანებას მოძებნის.
- tests/integration/test_compose_managers.py: GISSTACK_TEST_INTEGRATION=1 (NOT the GISSTACK_TEST_DOCKER that the ordinary tests job sets); it needs host ports 8080/8081 free.

## უსაფრთხოების გვერდი და აუდიტი 2026-11
- `gisstack/security_report.py` (წმინდა: `posture()`, `audit_events()`), `api/routes_security.py` (`admin`), `views/security.js`. ახალი შემოწმება = ახალი ჩანაწერი `posture()`-ში + ტესტი `tests/test_security_report.py`-ში. არარსებული ფაილი არ ბეჭდება (არაფერი გამოგონილი); symlink არ მიჰყვება.
- დაფის JS-ში დინამიურ ტექსტს `t()`-ით **ნუ** გაატარებ: გვერდს თავად თარგმნის `MutationObserver`; `t()` ტექსტს ინგლისურად ამოყინავს და ენის გადართვისას უკან ვეღარ ბრუნდება.
- ლოკალური გაშვებისთვის flask-იანი venv გჭირდება: `python3` ფლასკის გარეშე API ტესტებს (ApiBase) გამოტოვებს; მხოლოდ venv-ით გაშვებული 759 ტესტი სრულად მოწმდება. `node --test tests/js` დირექტორიით ვერ მუშაობს — `node --test tests/js/*.mjs`.
- აუდიტის გეგმა და მდგომარეობა: `docs/AUDIT-2026-11.md`.

## ალერტების მენეჯერი და განთავსების ჩანაწერები
- წესები: `monitoring/rulestore.py` (ბლოკი + `revision`, ატომური ჩაწერა, ჩაშენებული წესი არ იშლება), ვალიდაცია `alerts.custom_rule/check_text/KNOWN_METRICS`. ახალი მეტრიკა = დაამატე `KNOWN_METRICS`-ში (ტესტი ამოწმებს ნაგულისხმევ წესებს). შეცდომის ტექსტში ბილიკი არ ჩაწერო (`p.name`).
- განთავსების ჩანაწერი: `deployments.py` + კაუკი `deploy.deploy()`-ში (ყველა UI გადის მასზე). უკან დაბრუნება = წინა `ok` ჩანაწერის კონფიგის ხელახლა გამოყენება; **ახლანდელი საიდუმლოები რჩება** (`with_current_secrets`); არასოდეს purge/clean. ახალი გამოსავალი → `OUTCOMES`. ტესტებში `GISSTACK_VERIFY_TIMEOUT=0` დააყენე, თორემ არაჯანმრთელი mock 60 წმ ელოდება.
- კლასიკური `/` გვერდის `unsafe-inline` ჯერ რჩება (შიგნით ინლაინ სკრიპტი/სტილი, იხ. `docs/AUDIT-2026-11.md`); ძველი `/api/*` რჩება სანამ ეს გვერდი არ გაქრება.

## ბექაფი Compose/Podman/K8s-ზე (`gisstack/backups/`)
- `model` (ფორმატი, ids, კომპონენტები, `scan_tar`), `store` (ატომური `.partial`→seal, SHA256SUMS, verify, retention, restore-state), `provider` (ერთი საერთო `BackupProvider` + `Compose/Podman/Kubernetes` ადაპტერები: მხოლოდ `place`, `restart`, `running`). ახალი კომპონენტი = ჩანაწერი `model.COMPONENTS` + `targets.WORKLOADS` + ტესტი.
- წესები: ბექაფი = სრულად გადამოწმებული; არაფერი იცვლება სანამ ჩანაცვლება არ დადასტურდა; ყოველი გაცვლა შექცევადია და state-ფაილში იწერება **წინასწარ**; წინა მონაცემები (`<db>_pre_*`, `.gs-pre-*`) არასოდეს იშლება ავტომატურად; საიდუმლოები არც ბექაფშია, არც manifest/ლოგში; ფაილები 0600 (`os.open`, არა `open`).
- ტესტები: `tests/test_stack_backup.py` (ლოკალური დირექტორია ცვლის volume-ს: `LocalProvider._argv`), `tests/test_stack_backup_api.py`, რეალური: `tests/integration/test_stack_backup_{compose,k8s}.py` (საერთო ქეისები `stackbackup_cases.py`), ბრაუზერი `tests/e2e/test_browser_backup.py`. `kubectl exec` workload-ზე 0 რეპლიკით ელოდება pod-ს ~1 წუთი — `running()` ჯერ readyReplicas-ს ამოწმებს.
- `statefulset` pod-ის „forced rollback" პრობლემა — იხ. `apply.sh` `replace_stale_pod`.

## Podman და გამკაცრება
- Podman: `runtime: podman` → `generate` სურათებს `docker.io/`-ით აკვალიფიცირებს (`reg` ცვლადი შაბლონებში); `providers/compose.py` სტატუსს/ლოგებს/restart-policy-ს `podman ps --filter label=com.docker.compose.project=...`-დან კითხულობს და `_from_podman`-ით ადაპტირებს (health ტექსტი: `(healthy)`, არა `(health: healthy)`). `podman compose` = რომელი provider დგას (podman-compose-ს `ps -a`, `--wait`, `--no-color` არ აქვს). healthcheck-ებს Podman-ში systemd სჭირდება — ტესტები: `tests/integration/run_podman_tests.sh`.
- ვებ სერვისი: `packaging/gisstack-web.service` (DynamicUser) და `gisstack-web-root.service`; `tests/integration/test_packages.py::web_service` რეალურად უშვებს სერვისს systemd კონტეინერში. ტოკენი stdout-ზე მხოლოდ tty-ზე იბეჭდება.
- SSH: `transport.adhoc_trust()`; პროფილი = მკაცრი pinned key (უცვლელი).
- `docs/LEGACY-API.md` უნდა ემთხვეოდეს `registry`-ს (`tests/test_legacy_api_doc.py`).

