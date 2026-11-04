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

## გაკვეთილები
- EL9: `python3` = 3.9, MapProxy 7 ითხოვს 3.10+ (ვიყენებთ `python3.12`); `spawn-fcgi` არ არის EL9-ში → QGIS FastCGI სოკეტს systemd აძლევს.
- `dnf list --available` არ აჩვენებს უკვე დაყენებულ პაკეტს — იდემპოტენტურობისთვის `dnf list` გამოიყენე.
- VM-ის QEMU-ს `kill` = დენის გამორთვა (0-ბაიტიანი ფაილები); ჯერ `poweroff`.
- Bash heredoc-ში `EOF` შიდა ტექსტი ხურავს გარე heredoc-ს — გამოიყენე უნიკალური ტერმინატორი.
- `pkill -f` შეიძლება საკუთარ shell-ს მოკლას — გამოიყენე PID.
- JSX `{{ }}` ეჯახება Jinja-ს; RN შაბლონებში StyleSheet გამოიყენე.
