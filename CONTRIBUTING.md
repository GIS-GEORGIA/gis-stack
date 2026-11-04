# წვლილის შეტანა / Contributing

გმადლობთ, რომ გსურთ პროექტის გაუმჯობესება! წვლილის შეტანა ნებისმიერს შეუძლია — კოდით, ტესტებით,
დოკუმენტაციით, თარგმანით ან იმით, რომ გამოსცდით სხვადასხვა სისტემაზე და შედეგს გვაცნობებთ.
Anyone is welcome to contribute: code, tests, docs, translations, or reports from systems we have not tried.

## ლიცენზია / License

პროექტი ქვეყნდება [MIT](LICENSE) ლიცენზიით. pull request-ის გამოგზავნით თქვენ ადასტურებთ, რომ თქვენი
წვლილის ავტორი ხართ (ან გაქვთ მისი გავრცელების უფლება) და თანახმა ხართ, ის გავრცელდეს იმავე MIT
ლიცენზიით. By submitting a pull request you agree that your contribution is licensed under the MIT license.

## როგორ დავიწყოთ

1. გახსენი [issue](https://github.com/GIS-GEORGIA/gis-stack/issues) (ბაგი, იდეა ან კითხვა) — დიდ ცვლილებამდე
   სჯობს ჯერ განვიხილოთ.
2. გააკეთე **fork**, შემდეგ ბრენჩი: `git checkout -b my-change`.
3. გაუშვი იგივე შემოწმებები, რასაც CI გაუშვებს:
   ```bash
   pip install flask jinja2 pyyaml mapproxy ruff mypy types-PyYAML coverage bandit pip-audit
   python3 -m unittest discover -s tests        # ტესტები (Python 3.9 – 3.13)
   ruff check .                                   # სტილი და ტიპური შეცდომები
   mypy                                           # ტიპები
   coverage run -m unittest discover -s tests && coverage report --fail-under=70
   bandit -r gisstack -ll                         # უსაფრთხოება
   ```
   CI ასევე ამოწმებს shell სკრიპტებს (`shellcheck`), YAML-ს, `docker compose config`-ს, Kubernetes მანიფესტებს (`kubeconform`), დამოკიდებულებებს (`pip-audit`),
   პაკეტების აგებასა და დაყენებას (sdist, wheel, deb, rpm) და CodeQL-ს.
4. გახსენი **pull request** `main`-ზე და აღწერე, რა შეიცვალა და როგორ გადაამოწმე.

## პროექტის აგებულება

| გზა | რა არის |
|---|---|
| `gisstack/config.py`, `catalog.py` | პარამეტრები, ვალიდაცია, კომპონენტების კატალოგი |
| `gisstack/generate.py`, `templates/` | Jinja2 შაბლონები და ფაილების გეგმა (native / compose / k8s / webmap / mobile) |
| `gisstack/deploy.py`, `projects.py` | გაშვება ლოკალურად ან SSH-ით, გასუფთავება, პროექტების შენახვა |
| `gisstack/i18n.py`, `settings.py` | ინტერფეისის თარგმანები (KA → EN) და პარამეტრები (ენა, თემა) |
| `gisstack/ops.py` | დიაგნოსტიკა: სტატუსი, start/stop/restart, ლოგები (native / compose / k8s) |
| `gisstack/webgui/`, `desktop.py`, `cli.py` | ვებ (Flask), Tkinter და ბრძანების ხაზის ინტერფეისები |
| `tests/` | `unittest` ტესტები |

## წესები

- **ტესტები:** ახალი ქცევა ტესტით დაფარე; PR-მდე `python3 tests/test_gisstack.py` უნდა გადიოდეს.
- **შაბლონები:** shell-ის შაბლონი უნდა გადიოდეს `bash -n`-ს და იყოს იდემპოტენტური (ორჯერ გაშვება არაფერს არღვევს).
  Jinja-ში `{#` და `{%` ბაშის ტექსტში ერიდეთ (მაგ. `${#arr[@]}`).
- **ინტერფეისის ტექსტი:** ახალი ქართული ტექსტი ჩაამატე ინგლისური თარგმანით ფაილში `gisstack/i18n.py` (ქართული → English); ტესტი `I18nTests` ჩავარდება, თუ რომელიმე ტექსტი უთარგმნელია.
- **უსაფრთხოება:** არასოდეს ჩაწერო პაროლი, ტოკენი ან გასაღები კოდში, ტესტებში ან მაგალითებში.
- **სტილი:** მიჰყევი არსებულ კოდს (სახელები, კომენტარების სიმჭიდროვე). მომხმარებლის ტექსტი ქართულია,
  კოდი და კომენტარები — ინგლისური ან ქართული, როგორც გვერდით ფაილშია.
- **კომიტები:** მოკლე, მიზნის მთქმელი სათაური; ერთი PR — ერთი თემა.
- **რეალური სისტემა:** `install.sh`-ის ცვლილება სასურველია სუფთა VM-ზე გამოსცადო (Ubuntu 24.04 და Rocky 9);
  PR-ში მიუთითე, რაზე გაიარა.

## რისი გამოცდა გვჭირდება

[README](README.md#რა-არის-გადამოწმებული)-ში წერია, რა არ არის ჯერ გადამოწმებული (Debian 12, Fedora, AlmaLinux,
PostgreSQL 16/18, APK/iOS ბილდი, k8s-ის გასუფთავება რეალურ კლასტერზე და სხვ.). ასეთი შედეგის შეტყობინებაც
(თუნდაც კოდის გარეშე) ძალიან გვეხმარება.

## ქცევა და უსაფრთხოება

მონაწილეობით იცავთ [ქცევის კოდექსს](CODE_OF_CONDUCT.md). დაუცველობა **არ** გამოაქვეყნოთ ღია issue-ში —
იხილეთ [SECURITY.md](SECURITY.md).
