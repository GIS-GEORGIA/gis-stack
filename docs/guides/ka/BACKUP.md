# ბექაფისა და აღდგენის ინსტრუქცია

ბექაფისა და აღდგენის მოკლე გზა. ინგლისური ვერსია: [../en/BACKUP.md](../en/BACKUP.md). ყველა პარამეტრიანი სრული ინსტრუქცია: [docs/RECOVERY.md](../../RECOVERY.md). ბექაფი **native** დაყენებას ეხება.

## 1. გააკეთე ბექაფი

```bash
gisstack backup create --mode ssh --ssh root@server
gisstack backup list   --mode ssh --ssh root@server
gisstack backup verify ID --mode ssh --ssh root@server
```

ბექაფი შეიცავს კონფიგურაციას (`credentials.txt`-ის ჩათვლით), PostgreSQL-ის თანმიმდევრულ დამპს როლებით და GeoServer-ისა და QGIS-ის მონაცემებს. MapProxy-ის ფილების ქეში არ შედის, თუ `--with-cache`-ს არ დაამატებ (ის თავიდან აიგება). **ბექაფში საიდუმლოებებია: დაიცავი საქაღალდეც და ყველა ასლიც.**

## 2. ასლი სხვა ადგილას შეინახე

```bash
gisstack backup download ID --dest ~/gis-backups --mode ssh --ssh root@server
```

ასლი ჰეშებით გადამოწმებულია და არსებულ ფაილს არასოდეს გადაწერს. ბექაფი, რომელიც მხოლოდ იმავე სერვერზეა, რომელსაც იცავს, ბექაფი არ არის.

## 3. დააავტომატე

```bash
gisstack backup schedule enable --calendar daily --keep 14 --days 30 --mode ssh --ssh root@server
```

ეს სერვერზე systemd ტაიმერს აყენებს. უახლესი ბექაფი არასოდეს იშლება.

## 4. აღადგინე

```bash
gisstack backup restore ID --mode ssh --ssh root@server --dry-run     # only show the plan
gisstack backup restore ID --mode ssh --ssh root@server               # asks, then restores
```

აღდგენა ამოწმებს ბექაფს, აკეთებს გადამოწმებულ აღდგენამდელ ბექაფს, მონაცემებს გადარქმევით ცვლის, შედეგს ამოწმებს და შეცდომისას თავად ბრუნდება უკან. ძველი მონაცემები რჩება, სანამ თავად არ წაშლი.

## 5. ივარჯიშე სათადარიგო მანქანაზე

დროდადრო აღადგინე ახალ სერვერზე (`gisstack backup upload`, შემდეგ `restore`) ან დროებით ბაზაში (`--staging`). ბექაფი, რომელიც არასოდეს აღგიდგენია, იმედია და არა გეგმა.
