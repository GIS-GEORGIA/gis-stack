# Installation guide

How to get gis-stack onto a machine and use it to install a GIS stack. Georgian version: [../ka/INSTALL.md](../ka/INSTALL.md).

## Requirements

* Linux with systemd for the **native** target: Ubuntu 22.04/24.04, Debian 12, Rocky/Alma 9 (tested in CI containers); Docker or Podman with Compose for **Compose**; a cluster and `kubectl` for **Kubernetes**.
* Python 3.9 or newer on the machine that runs gis-stack (the packages bring their own dependencies).
* Root, or a user with passwordless `sudo`, on the server you install to.
* Enough RAM and disk for PostgreSQL and the GeoServer heap (preflight checks this before anything is installed).

## 1. Install gis-stack

From a package (recommended on servers):

```bash
sudo apt install ./gisstack-installer_<version>_all.deb          # Debian / Ubuntu
sudo dnf install ./gisstack-installer-<version>-1.noarch.rpm     # Rocky / Alma / Fedora (python3-flask comes from EPEL)
```

From source:

```bash
git clone https://github.com/GIS-GEORGIA/gis-stack.git && cd gis-stack
sudo ./install.sh
```

Check: `gisstack --version`.

## 2. Check the target first

```bash
gisstack preflight --mode ssh --ssh root@server --key ~/.ssh/id_ed25519
```

Preflight checks disk, memory, OS, network and clock, and reports ports that other programs already use. `gisstack scan` lists programs that are already installed.

## 3. Configure and deploy

With the web GUI (it listens on `127.0.0.1:8765` only):

```bash
gisstack web
```

Open the printed link (it contains a one-time token). On a remote server tunnel it first: `ssh -L 8765:127.0.0.1:8765 user@server`.

Without a GUI:

```bash
gisstack defaults > stack.json
gisstack generate -c stack.json -o ./bundle
gisstack deploy -c stack.json --mode ssh --ssh root@server --key ~/.ssh/id_ed25519
```

`generate` only writes files, so you can read them (and run `native/install.sh` yourself) before anything is installed. Deploying is safe to repeat: every step is idempotent.

## 4. Verify

```bash
gisstack status -c stack.json
```

Every program should be running and answering. The generated passwords are in `/etc/gisstack/credentials.txt` on the server (root only).

## Remove

```bash
gisstack clean -c stack.json --only tomcat,caddy
```

Without `--purge` the databases, GeoServer data and caches stay. See the [backup guide](BACKUP.md) before deleting anything.
