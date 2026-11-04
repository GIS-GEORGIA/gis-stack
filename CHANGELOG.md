# Changelog

ცვლილებების ისტორია. ფორმატი: [Keep a Changelog](https://keepachangelog.com/), ვერსიები: `მაჟორი.მინორი.პატჩი`.
ყოველი გამოშვების ტექსტი ავტომატურად ხვდება GitHub Release-ში და ჩანს „განახლების შემოწმებაში".

## [0.1.2] - 2026-11-04

### დაემატა
- **განახლების შემოწმება:** `gisstack update` და ღილაკი „განახლების შემოწმება" ვებ/Tk GUI-ში — ხედავ ახალ ვერსიას და
  ცვლილებებს, ჩამოტვირთავ SHA-256-ით გადამოწმებულ ფაილს, ან `install.sh`-ით დაყენებულს ავტომატურად განაახლებ.
- **დიაგნოსტიკა:** ცოცხალი სტატუსი (IP:პორტი, რეალური შემოწმებები), start/stop/restart/enable/disable, ლოგები —
  `gisstack status|start|stop|restart|enable|disable|logs`, ვებ და Tk GUI.
- **პროგრამების ზოლები:** თითოეული პროგრამის მონიშვნა, შენახვა და „შენახვა და გამოყენება"; შენახული პროექტები.
- **გასუფთავება:** ცალკეული პროგრამის ან მთელი სტეკის წაშლა, ნებაყოფლობით მონაცემებითა და PostgreSQL-ით.
- MIT ლიცენზია, CONTRIBUTING, SECURITY, CI, ინსტრუქცია `docs/USAGE.md`.

### გასწორდა
- Tomcat-ის ფაილების უფლებები (სერვისი ვერ ეშვებოდა).
- EL9: PostGIS/QGIS პაკეტების განმეორებითი გაშვება, MapProxy-ისთვის Python 3.12, QGIS FastCGI systemd socket-ით
  (`spawn-fcgi` EL9-ში არ არის).
- გენერირებული პაროლები აღარ იცვლება ყოველ გაშვებაზე.
- k8s: GeoServer-ის readiness probe; compose: MapProxy-ის მომხმარებლის home.

## [0.1.1] - 2026-11-04
- გასწორებები native ინსტალაციაში (Ubuntu 24.04 და Rocky 9 VM-ებზე გამოცდის შედეგად).

## [0.1.0] - 2026-11-04
- პირველი ვერსია: PostGIS, Tomcat, GeoServer, QGIS Server, MapProxy, Caddy, ვებ რუკა; native / compose / k8s; ვებ და
  Tk GUI.
