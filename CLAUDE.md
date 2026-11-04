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

## გაკვეთილები
- EL9: `python3` = 3.9, MapProxy 7 ითხოვს 3.10+ (ვიყენებთ `python3.12`); `spawn-fcgi` არ არის EL9-ში → QGIS FastCGI სოკეტს systemd აძლევს.
- `dnf list --available` არ აჩვენებს უკვე დაყენებულ პაკეტს — იდემპოტენტურობისთვის `dnf list` გამოიყენე.
- VM-ის QEMU-ს `kill` = დენის გამორთვა (0-ბაიტიანი ფაილები); ჯერ `poweroff`.
- Bash heredoc-ში `EOF` შიდა ტექსტი ხურავს გარე heredoc-ს — გამოიყენე უნიკალური ტერმინატორი.
- `pkill -f` შეიძლება საკუთარ shell-ს მოკლას — გამოიყენე PID.
- JSX `{{ }}` ეჯახება Jinja-ს; RN შაბლონებში StyleSheet გამოიყენე.
