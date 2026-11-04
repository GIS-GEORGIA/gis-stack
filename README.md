# GIS Stack Installer

ერთი ხელსაწყო, რომელიც Linux სერვერზე აყენებს სრულ GIS სტეკს — ბრძანებების ხელით აკრეფის გარეშე:

| კომპონენტი | შენიშვნა |
|---|---|
| PostgreSQL + PostGIS | ექსთენშენები მონიშვნით (raster, topology, pgRouting, pointcloud, ogr_fdw, H3, hstore, pg_trgm …), RAM-ის მიხედვით ტიუნინგი |
| Apache Tomcat | 9 / 10.1 / 11 (GeoServer-ის ხაზი თავად განსაზღვრავს) |
| GeoServer | 3.0.2 (Tomcat 11) ან 2.28.6 (Tomcat 9), გაფართოებები მონიშვნით, admin პაროლი ავტომატურად იცვლება |
| QGIS Server | FastCGI (native) ან კონტეინერი |
| MapProxy 7 | grid-ები (3857, 4326, UTM 37N/38N), file/sqlite/geopackage ქეში, ფენები, seed |
| Caddy | ერთი შესასვლელი `/`, `/geoserver`, `/mapproxy`, `/qgis`; ავტომატური HTTPS |
| ვებ რუკა | ორენოვანი (KA/EN) Leaflet, ოფლაინ-ბიბლიოთეკით, უკვე მიბმული შენს ფენებზე |
| მობილური | React Native (Expo) და Flutter შაბლონები, მიბმული შენს სერვერზე |

სამიზნეები: **პირდაპირ სერვერზე** (apt / dnf), **Docker / Podman Compose**, **Kubernetes**.

## დაყენება

```bash
# 1) პაკეტი
sudo apt install ./gisstack-installer_0.1.2_all.deb          # Debian / Ubuntu
sudo dnf install ./gisstack-installer-0.1.2-1.noarch.rpm      # RHEL / Rocky / Alma / Fedora (python3-flask: EPEL)

# 2) პაკეტის გარეშე, ამ საქაღალდიდან
sudo ./install.sh            # ან ./install.sh --tk  (Desktop GUI-სთვის)
```

## გამოყენება

```bash
gisstack web          # ვებ GUI → გახსენი დაბეჭდილი ბმული (token-ით)
gisstack desktop      # Desktop GUI (python3-tk)
```

**ვებ GUI სერვერზე:** ის მხოლოდ `127.0.0.1:8765`-ზე უსმენს. კომპიუტერიდან:
`ssh -L 8765:127.0.0.1:8765 user@server`, შემდეგ გახსენი `http://127.0.0.1:8765/?token=…`.
Token: `/var/lib/gisstack-installer/web.token`.

GUI-ში: მონიშნე კომპონენტები → „წინასწარი ნახვა“ (პაროლები დაფარულია) → **Deploy**
(ამ მანქანაზე, SSH-ით სხვა სერვერზე, ან მხოლოდ ZIP).

### უგრაფიკოდ

```bash
gisstack defaults > stack.json          # შეცვალე ველები
gisstack generate -c stack.json -o ./bundle
gisstack deploy   -c stack.json --mode ssh --ssh root@server --key ~/.ssh/id_ed25519
```

## რას აგენერირებს (`bundle/`)

```
native/    install.sh  uninstall.sh  stack.env  config/  systemd/  webmap/
compose/   up.sh  docker-compose.yml  .env  caddy/  mapproxy/  postgis/  webmap/
k8s/       apply.sh  kustomization.yaml  *.yaml  files/
webmap/    index.html  app.js  config.js  vendor/leaflet/
mobile/    react-native/  flutter/
```

`native/install.sh` იდემპოტენტურია: `--only postgis,caddy`, `--skip qgis`, `--list`.
`compose/up.sh`: `up | down | logs | ps | seed`. `k8s/apply.sh`: `apply | delete | status`.

## უსაფრთხოება

- პაროლები ავტომატურად გენერირდება; ფაილები (`stack.env`, `.env`, `secret.yaml`, `stack.json`) 600 უფლებით. **git-ში არ ატვირთო.**
- Native რეჟიმში Tomcat / MapProxy / QGIS მხოლოდ `127.0.0.1`-ზე უსმენს, გარეთ მხოლოდ Caddy (80/443).
- Tomcat manager არ ექვემდებარება გარე წვდომას (Caddy 403-ს აბრუნებს).
- PostgreSQL გარე წვდომა გამორთულია, სანამ GUI-ში არ ჩართავ.
- K8s `secret.yaml` — პროდაქშენისთვის ჩაანაცვლე SealedSecrets / Vault-ით.

## რა არის გადამოწმებული და რა — არა

გადამოწმებულია ამ გარემოში: გენერატორი სამივე სამიზნეზე; ყველა YAML იპარსება; shell-ები
`bash -n`-ს გადის; `docker compose config` ამოწმებს Compose ფაილს; MapProxy-ის კონფიგი იტვირთება
რეალურ MapProxy 7.0.0-ში; ვებ GUI-ის API (ავტორიზაცია, CSRF, ვალიდაცია); deb და rpm იგება და ინსტალირდება; Caddy + MapProxy + ვებ რუკა end-to-end (ტაილების კოორდინატები Leaflet-ის XYZ-ს ემთხვევა: კლიენტები იყენებენ MapProxy-ის WMTS REST მისამართს `/mapproxy/wmts/<ფენა>/GLOBAL_WEBMERCATOR/{z}/{x}/{y}.png`, რადგან TMS მისამართს დონე/Y-ღერძი აცდენილი აქვს).

**ჯერ არ გაშვებულა რეალურ მანქანებზე** — პირველ რეალურ გაშვებამდე ჩაატარე სატესტო VM-ზე:
- `native/install.sh` რეალურ Debian/Ubuntu/Rocky სერვერზე (პაკეტების სახელები და PGDG/EPEL ნაბიჯები);
- Caddyfile `caddy validate`-ით (ამ გარემოში Caddy-ს ბინარი ვერ ჩამოვტვირთე);
- Docker image tag-ები (`postgis/postgis:<ver>-3.5`, `docker.osgeo.org/geoserver:3.0.2`, `camptocamp/qgis-server`) — ვერ ვადასტურებ; `up.sh` pull-ზე გაჩერდება, თუ რომელიმე არასწორია — შეცვალე `docker-compose.yml`-ში (GeoServer-ის tag: GUI/კონფიგის `geoserver.image`);
- Desktop GUI (Tkinter არ იყო დაყენებული; კონფიგის შეგროვების ლოგიკა გატესტილია, ვიჯეტები — არა);
- React Native / Flutter კოდი არ დაკომპილებულა; Flutter მიზნად ისახავს `flutter_map` 6–8 API-ს;
- GeoServer 3 / Tomcat 11 შესაბამისობა აღებულია GeoServer-ის დოკუმენტაციიდან (10.1 — მხარდაუჭერელი).

## პაკეტების აგება

```bash
packaging/build.sh deb        # dist/gisstack-installer_<ver>_all.deb
packaging/build.sh rpm        # საჭიროა rpmbuild (dnf install rpm-build) ან nfpm
python3 tests/test_gisstack.py
packaging/vendor.sh           # Leaflet-ის ოფლაინ ასლის განახლება
```

ლიცენზია: MIT. Leaflet — BSD-2-Clause (`gisstack/vendor/leaflet/LICENSE.txt`).
