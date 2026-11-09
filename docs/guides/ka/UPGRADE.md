# განახლების ინსტრუქცია

თავად gis-stack-ის განახლება და დაყენებული სტეკის გადაყვანა ახალ ვერსიებზე. ინგლისური ვერსია: [../en/UPGRADE.md](../en/UPGRADE.md).

## 1. განაახლე gis-stack

```bash
gisstack update --check          # exit code 10 when a newer release exists
gisstack update                  # installs it when gis-stack was installed by install.sh
gisstack update --download ~/Downloads     # for deb/rpm installs: fetch the verified package, then install it yourself
```

ფონზე არაფერი მოწმდება; ბრძანება GitHub-ს მხოლოდ მაშინ უკავშირდება, როცა თავად გაუშვებ.

### რა მოწმდება

1. ჩამოტვირთვა უნდა მოდიოდეს ამ რეპოზიტორიის release ფაილებიდან.
2. ფაილის SHA-256 უნდა ემთხვეოდეს გამოშვების `SHA256SUMS`-ს.
3. თავად `SHA256SUMS`-ს უნდა ჰქონდეს სწორი [Sigstore](https://www.sigstore.dev/) ხელმოწერა, რომელიც ამ რეპოზიტორიის release workflow-მ დადო. ამისთვის მანქანაზე `cosign` პროგრამა სჭირდება. მის გარეშე განახლება გაფრთხილების შემდეგ გრძელდება, რადგან ჰეში იმავე ადგილიდან მოდის, საიდანაც ფაილი. რომ ხელმოწერის არარსებობა ან ვერშემოწმება შეცდომად ჩაითვალოს:

```bash
export GISSTACK_REQUIRE_SIGNATURE=1
```

ხელითაც შეგიძლია შეამოწმო:

```bash
cosign verify-blob --bundle SHA256SUMS.sigstore.json \
  --certificate-identity-regexp '^https://github\.com/GIS-GEORGIA/gis-stack/\.github/workflows/release\.yml@refs/tags/v[0-9.]+$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing
```

ხელმოწერის დანერგვამდე გამოშვებულ ვერსიებს ხელმოწერის ფაილი არ აქვთ; ისინი „ხელმოუწერელად“ ჩანს.

## 2. სტეკის განახლებამდე

1. წაიკითხე გამოშვების აღწერა (`CHANGELOG.md`, `gisstack update`-იც აჩვენებს). იქ წერია ცვლილებები, რომლებიც შენგან მოქმედებას მოითხოვს.
2. გააკეთე გადამოწმებული ბექაფი: `gisstack backup create --mode ssh --ssh root@server` (იხ. [ბექაფის ინსტრუქცია](BACKUP.md)).
3. გაუშვი `gisstack preflight` სერვერზე.

## 3. განაახლე სტეკი

ინსტალერის ხელახლა გაშვება განაახლებს პაკეტებს და თავიდან დაწერს გენერირებულ ფაილებს. შენახული კონფიგურაცია და გენერირებული პაროლები რჩება.

```bash
gisstack deploy -c stack.json --mode ssh --ssh root@server       # everything
gisstack apply  -c stack.json mapproxy                           # just one program
```

PostgreSQL-ის **მთავარი** ვერსიის განახლებას gis-stack თავად არ აკეთებს: ვერსია შეცვალე მხოლოდ dump/restore გეგმასთან ერთად (ბექაფი, ახალი ვერსიის გვერდიგვერდ დაყენება, აღდგენა, გადართვა).

## 4. თუ რამე ვერ გამოვიდა

აღადგინე მე-2 ნაბიჯის ბექაფი (`gisstack backup restore ID`); აღდგენის შეცდომისას ის თავად უკან ბრუნდება. gis-stack-ის საკუთარი virtualenv-ის ძველ ვერსიაზე დასაბრუნებლად იმავე გარემოში გაუშვი `pip install gisstack-installer==<old version>`.
