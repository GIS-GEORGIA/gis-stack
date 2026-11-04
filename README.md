# GIS Stack Installer

[![tests](https://github.com/GIS-GEORGIA/gis-stack/actions/workflows/tests.yml/badge.svg)](https://github.com/GIS-GEORGIA/gis-stack/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**KA** — ერთი ხელსაწყო, რომელიც Linux სერვერზე აყენებს სრულ GIS სტეკს ბრძანებების ხელით აკრეფის გარეშე.
**EN** — Generate and run a complete open-source GIS server stack (PostGIS, Tomcat, GeoServer, QGIS Server,
MapProxy, Caddy, a bilingual web map and mobile app templates) from a web or desktop GUI, a CLI, or plain
config files. Targets: native packages (apt / dnf), Docker / Podman Compose, Kubernetes.

| კომპონენტი | შენიშვნა |
|---|---|
| PostgreSQL + PostGIS | ექსთენშენები მონიშვნით (raster, topology, pgRouting, pointcloud, ogr_fdw, H3, hstore, pg_trgm …), RAM-ის მიხედვით ტიუნინგი |
| Apache Tomcat | 9 / 10.1 / 11 (GeoServer-ის ხაზი თავად განსაზღვრავს) |
| GeoServer | 3.0.2 (Tomcat 11) ან 2.28.6 (Tomcat 9), გაფართოებები მონიშვნით, admin პაროლი ავტომატურად იცვლება |
| QGIS Server | FastCGI (native, systemd socket) ან კონტეინერი |
| MapProxy 7 | grid-ები (3857, 4326, UTM 37N/38N), file/sqlite/geopackage ქეში, ფენები, seed |
| Caddy | ერთი შესასვლელი `/`, `/geoserver`, `/mapproxy`, `/qgis`; ავტომატური HTTPS |
| ვებ რუკა | ორენოვანი (KA/EN) Leaflet, ოფლაინ-ბიბლიოთეკით, უკვე მიბმული შენს ფენებზე |
| მობილური | React Native (Expo) და Flutter შაბლონები, მიბმული შენს სერვერზე |

> **ახალი ხარ?** ნაბიჯ-ნაბიჯ ინსტრუქცია — [docs/USAGE.md](docs/USAGE.md): როგორ გაუშვა, როგორ აირჩიო პროგრამები და ვერსიები ვებ GUI-ში, როგორ აკონტროლო.

## დაყენება

```bash
git clone https://github.com/GIS-GEORGIA/gis-stack.git && cd gis-stack
sudo ./install.sh            # ან ./install.sh --tk  (Desktop GUI-სთვის)
```

ან როგორც პაკეტი (იხ. [პაკეტების აგება](#პაკეტების-აგება)):

```bash
sudo apt install ./gisstack-installer_<ver>_all.deb          # Debian / Ubuntu
sudo dnf install ./gisstack-installer-<ver>-1.noarch.rpm     # RHEL / Rocky / Alma / Fedora (python3-flask: EPEL)
```

## გამოყენება

```bash
gisstack web          # ვებ GUI → გახსენი დაბეჭდილი ბმული (token-ით)
gisstack desktop      # Desktop GUI (python3-tk)
```

**ვებ GUI სერვერზე:** ის მხოლოდ `127.0.0.1:8765`-ზე უსმენს. კომპიუტერიდან:
`ssh -L 8765:127.0.0.1:8765 user@server`, შემდეგ გახსენი `http://127.0.0.1:8765/?token=…`.
Token: `/var/lib/gisstack-installer/web.token`.

### GUI

1. მონიშნე კომპონენტები და შეავსე მათი პარამეტრები → **წინასწარი ნახვა** (პაროლები დაფარულია).
2. თითოეულ პროგრამას აქვს ზოლი: ☑ **ჩართულია** · **შენახვა** (წერს მხოლოდ ამ პროგრამის პარამეტრებს) ·
   **შენახვა და გამოყენება** (ააყენებს/გადატვირთავს მხოლოდ მას, დანარჩენს არ ეხება).
3. **Deploy** — ყველაფერი ერთად: ამ მანქანაზე, SSH-ით სხვა სერვერზე, ან მხოლოდ ZIP.
4. **გასუფთავება** — თითოეული პროგრამა ცალკე, ან მთელი სტეკი; „მონაცემების წაშლაც“ ჩართვის გარეშე ბაზა,
   GeoServer-ის მონაცემები და ქეშები რჩება.

პროექტები ინახება `~/.gisstack/projects/<პროექტი>.json`-ში (მხოლოდ მფლობელისთვის წაკითხვადი); GUI-ს
ზედა ზოლიდან ჩაიტვირთება. გენერირებული პაროლები ცალკე ინახება და ხელახალ გაშვებაზე იგივე რჩება.

5. **დიაგნოსტიკა** — ცოცხალი ცხრილი (სურვილისამებრ ყოველ 5 წამში): თითოეული პროგრამის სტატუსი, ავტოგაშვება, PID,
   **რომელ IP-ზე და პორტზე უსმენს**, რეალური შემოწმება (HTTP / TCP / ბმული Caddy-ს გავლით) და ღილაკები:
   გაშვება, გაჩერება, რესტარტი, ავტოგაშვების ჩართვა/გამორთვა, ლოგი (ბოლო სტრიქონები ან ცოცხლად).
   Native-ზე ეს systemd ერთეულებია, Compose-ზე — კონტეინერები, Kubernetes-ზე — workload-ები.

### უგრაფიკოდ (CLI)

```bash
gisstack defaults > stack.json                         # შეცვალე ველები
gisstack generate -c stack.json -o ./bundle
gisstack deploy   -c stack.json --mode ssh --ssh root@server --key ~/.ssh/id_ed25519
gisstack save     -c stack.json --component mapproxy   # მხოლოდ ამ პროგრამის პარამეტრები → პროექტი
gisstack apply    -c stack.json mapproxy --save        # მხოლოდ MapProxy-ის გამოყენება
gisstack clean    -c stack.json --only tomcat,caddy    # ცალკეული პროგრამების გასუფთავება
gisstack clean    -c stack.json --purge -y             # მთელი სტეკი + მონაცემები
gisstack projects                                      # შენახული პროექტები

gisstack status  -c stack.json [--watch] [--json]      # სტატუსი, IP:პორტი, შემოწმებები (--watch: ცოცხლად)
gisstack restart -c stack.json geoserver               # start | stop | restart | enable | disable [პროგრამა…]
gisstack stop    -c stack.json                         # პროგრამების გარეშე = მთელი სტეკი
gisstack logs    -c stack.json caddy -n 200 -f         # ლოგი (-f: ცოცხლად)
```

`status` ამთავრებს მუშაობას კოდით 0, თუ ყველაფერი ჯანსაღია, და 1-ით — თუ რომელიმე პროგრამა არ მუშაობს ან
შემოწმება ვერ გაიარა (მოსახერხებელია მონიტორინგისთვის).

`clean` ნაგულისხმევად მონაცემებს ინახავს. `--purge` შლის ბაზას, GeoServer-ის მონაცემებს, ქეშებს, volume-ებს;
PostgreSQL-ის სერვერსაც მხოლოდ `--purge --remove-postgres` შლის (და უარს ამბობს, თუ იქ სხვა ბაზებიც არის).
Kubernetes-ზე მთელი სტეკის გასუფთავება შლის namespace-ს, ანუ ყველა volume-საც.

## რას აგენერირებს (`bundle/`)

```
native/    install.sh  uninstall.sh  stack.env  config/  systemd/  webmap/
compose/   up.sh  docker-compose.yml  .env  caddy/  mapproxy/  postgis/  webmap/
k8s/       apply.sh  kustomization.yaml  *.yaml  files/
webmap/    index.html  app.js  config.js  vendor/leaflet/
mobile/    react-native/  flutter/
```

| სამიზნე | შესასვლელი |
|---|---|
| native | `install.sh [--only postgis,caddy] [--skip qgis] [--list]` · `uninstall.sh [--only …] [--purge] [--remove-postgres]` (იდემპოტენტურია) |
| compose | `up.sh up \| down \| logs \| ps \| seed \| only <სერვისი>… \| clean [--purge] [სერვისი…]` |
| k8s | `apply.sh apply \| delete \| status \| only <კომპონენტი>… \| clean [--purge] [კომპონენტი…]` |

## უსაფრთხოება

- პაროლები ავტომატურად გენერირდება; ფაილები (`stack.env`, `.env`, `secret.yaml`, `stack.json`) 600 უფლებით. **git-ში არ ატვირთო.**
- Native რეჟიმში Tomcat / MapProxy / QGIS მხოლოდ `127.0.0.1`-ზე უსმენს, გარეთ მხოლოდ Caddy (80/443).
- Tomcat manager არ ექვემდებარება გარე წვდომას (Caddy 403-ს აბრუნებს).
- PostgreSQL გარე წვდომა გამორთულია, სანამ GUI-ში არ ჩართავ.
- K8s `secret.yaml` — პროდაქშენისთვის ჩაანაცვლე SealedSecrets / Vault-ით.
- დაუცველობის შესახებ შეტყობინება: [SECURITY.md](SECURITY.md).

## რა არის გადამოწმებული

გადამოწმებულია რეალურ გარემოზე:

- `native/install.sh` და `uninstall.sh` — **Ubuntu 24.04** და **Rocky Linux 9.8** (SELinux enforcing) VM-ებზე: სრული
  ინსტალაცია, განმეორებითი გაშვება, ცალკეული პროგრამის გასუფთავება და გამოყენება, `--purge`, ხელახალი ინსტალაცია;
- Docker Compose (`up.sh up|clean`), Kubernetes (`kubectl apply -k` kind-ზე);
- Flutter (`flutter analyze`) და Expo (`expo export`) კოდი; ვებ და Tkinter GUI;
- ავტომატური ტესტები (`python3 tests/test_gisstack.py`), მათ შორის რეალური MapProxy 7-ით.

**ჯერ არ არის გადამოწმებული:** Debian 12, Fedora, AlmaLinux; PostgreSQL 16/18; `QGIS_OFFICIAL_REPO=1`;
APK/iOS ბილდი; Expo-ს MapLibre ეკრანი; k8s-ის გასუფთავება რეალურ კლასტერზე; QGIS Server რეალური პროექტით.
თუ ამათგან რომელიმეს გამოსცდი — მოგვწერე [issue](https://github.com/GIS-GEORGIA/gis-stack/issues)-ში.

## პაკეტების აგება

```bash
packaging/build.sh deb        # dist/gisstack-installer_<ver>_all.deb
packaging/build.sh rpm        # საჭიროა rpmbuild (dnf install rpm-build) ან nfpm
python3 tests/test_gisstack.py
packaging/vendor.sh           # Leaflet-ის ოფლაინ ასლის განახლება
```

## წვლილის შეტანა / Contributing

წვლილის შეტანა ნებისმიერს შეუძლია: გახსენი issue, გააკეთე fork და გამოაგზავნე pull request. დეტალები —
[CONTRIBUTING.md](CONTRIBUTING.md). ყველა მონაწილე იცავს [ქცევის კოდექსს](CODE_OF_CONDUCT.md).

## ლიცენზია / License

[MIT](LICENSE) © GIS GEORGIA contributors. წვლილი ამავე ლიცენზიით ქვეყნდება.
Leaflet — BSD-2-Clause (`gisstack/vendor/leaflet/LICENSE.txt`).
