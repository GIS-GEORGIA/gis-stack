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

## 3. დაშიფრე ასლები

ბექაფში პაროლებია, ამიტომ სერვერიდან გასული ასლი დაშიფრული უნდა იყოს. ამისთვის შენს მანქანაზე `gpg` სჭირდება. დაშიფვრა **შენს** მანქანაზე ხდება, ჩამოტვირთვის გადამოწმების შემდეგ; სერვერი შენს პაროლს ან გასაღებს არასოდეს ხედავს.

```bash
echo 'a long passphrase you will not forget' > ~/.gisstack-backup.pw && chmod 600 ~/.gisstack-backup.pw
gisstack backup download ID --dest ~/gis-backups --passphrase-file ~/.gisstack-backup.pw --mode ssh --ssh root@server
gisstack backup download ID --dest ~/gis-backups --encrypt-to backup-admin@example.org --mode ssh --ssh root@server
gisstack backup decrypt ~/gis-backups/<host>/ID.tar.gz.gpg --passphrase-file ~/.gisstack-backup.pw
gisstack backup upload ~/gis-backups/<host>/ID.tar.gz.gpg --passphrase-file ~/.gisstack-backup.pw --mode ssh --ssh root@newserver
```

* **პაროლი** (AES-256): მინიმუმ 12 სიმბოლო; იკითხება `chmod 600` ფაილიდან, `GISSTACK_BACKUP_PASSPHRASE`-დან ან ორჯერ იკითხება ტერმინალში. ბრძანების სტრიქონში არასოდეს ხვდება. თუ დაკარგე, ბექაფიც დაკარგულია.
* **საჯარო გასაღები** (`--encrypt-to`): გაშიფვრა მხოლოდ პირად გასაღებს შეუძლია; გასაღები უკვე შენს keyring-ში უნდა იყოს.
* შედეგია `ID.tar.gz.gpg`; დაუშიფრავი ასლი იშლება. პაროლით დაშიფრული ასლი ბრძანების დასრულებამდე ერთხელ იშიფრება და მოწმდება. შეცვლილი ფაილი არ იშიფრება.
* `upload` ადგილზე შიფრავს და სერვერს SSH-ით დაუშიფრავ არქივს უგზავნის. თავად სერვერზე შენახული ბექაფები დაშიფრული არ არის: ცოცხალი საიდუმლოებები იმავე მანქანაზეა.

## 4. დააავტომატე

```bash
gisstack backup schedule enable --calendar daily --keep 14 --days 30 --mode ssh --ssh root@server
```

ეს სერვერზე systemd ტაიმერს აყენებს. უახლესი ბექაფი არასოდეს იშლება.

## 5. აღადგინე

```bash
gisstack backup restore ID --mode ssh --ssh root@server --dry-run     # only show the plan
gisstack backup restore ID --mode ssh --ssh root@server               # asks, then restores
```

აღდგენა ამოწმებს ბექაფს, აკეთებს გადამოწმებულ აღდგენამდელ ბექაფს, მონაცემებს გადარქმევით ცვლის, შედეგს ამოწმებს და შეცდომისას თავად ბრუნდება უკან. ძველი მონაცემები რჩება, სანამ თავად არ წაშლი.

## 6. ივარჯიშე სათადარიგო მანქანაზე

დროდადრო აღადგინე ახალ სერვერზე (`gisstack backup upload`, შემდეგ `restore`) ან დროებით ბაზაში (`--staging`). ბექაფი, რომელიც არასოდეს აღგიდგენია, იმედია და არა გეგმა.
