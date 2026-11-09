# დაყენების ინსტრუქცია

როგორ მოვათავსოთ gis-stack კომპიუტერზე და როგორ დავაყენოთ მისით GIS სტეკი. ინგლისური ვერსია: [../en/INSTALL.md](../en/INSTALL.md).

## მოთხოვნები

* **native** რეჟიმისთვის Linux systemd-ით: Ubuntu 22.04/24.04, Debian 12, Rocky/Alma 9 (CI-ში კონტეინერებზეა გატესტილი); **Compose**-ისთვის Docker ან Podman Compose-ით; **Kubernetes**-ისთვის კლასტერი და `kubectl`.
* Python 3.9 ან უფრო ახალი იმ მანქანაზე, სადაც gis-stack მუშაობს (პაკეტები საკუთარ დამოკიდებულებებს თავადვე მოიტანს).
* root, ან მომხმარებელი პაროლის გარეშე `sudo`-თი იმ სერვერზე, სადაც აყენებ.
* საკმარისი RAM და დისკი PostgreSQL-ისა და GeoServer-ის heap-ისთვის (preflight ამას დაყენებამდე ამოწმებს).

## 1. დააყენე gis-stack

პაკეტიდან (სერვერებზე რეკომენდებულია):

```bash
sudo apt install ./gisstack-installer_<version>_all.deb          # Debian / Ubuntu
sudo dnf install ./gisstack-installer-<version>-1.noarch.rpm     # Rocky / Alma / Fedora (python3-flask comes from EPEL)
```

წყაროდან:

```bash
git clone https://github.com/GIS-GEORGIA/gis-stack.git && cd gis-stack
sudo ./install.sh
```

შემოწმება: `gisstack --version`.

## 2. ჯერ სამიზნე შეამოწმე

```bash
gisstack preflight --mode ssh --ssh root@server --key ~/.ssh/id_ed25519
```

preflight ამოწმებს დისკს, მეხსიერებას, OS-ს, ქსელსა და საათს და გაჩვენებს პორტებს, რომლებსაც სხვა პროგრამები უკვე იყენებენ. `gisstack scan` აჩვენებს, რა არის უკვე დაყენებული.

## 3. გააკეთე კონფიგურაცია და გაუშვი

ვებ GUI-ით (ის მხოლოდ `127.0.0.1:8765`-ზე უსმენს):

```bash
gisstack web
```

გახსენი დაბეჭდილი ბმული (მასში token-ია). დაშორებულ სერვერზე ჯერ გაიტარე ტუნელი: `ssh -L 8765:127.0.0.1:8765 user@server`.

GUI-ს გარეშე:

```bash
gisstack defaults > stack.json
gisstack generate -c stack.json -o ./bundle
gisstack deploy -c stack.json --mode ssh --ssh root@server --key ~/.ssh/id_ed25519
```

`generate` მხოლოდ ფაილებს წერს, ამიტომ დაყენებამდე შეგიძლია წაიკითხო ისინი (და `native/install.sh` თავად გაუშვა). გაშვება უსაფრთხოდ მეორდება: ყოველი ნაბიჯი იდემპოტენტურია.

## 4. შეამოწმე

```bash
gisstack status -c stack.json
```

ყველა პროგრამა უნდა მუშაობდეს და პასუხობდეს. გენერირებული პაროლები სერვერზე `/etc/gisstack/credentials.txt`-შია (მხოლოდ root-ისთვის).

## წაშლა

```bash
gisstack clean -c stack.json --only tomcat,caddy
```

`--purge`-ის გარეშე ბაზები, GeoServer-ის მონაცემები და ქეშები რჩება. რამის წაშლამდე იხილე [ბექაფის ინსტრუქცია](BACKUP.md).
