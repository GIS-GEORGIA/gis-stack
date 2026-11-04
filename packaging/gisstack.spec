Name:           gisstack-installer
Version:        @VERSION@
Release:        1%{?dist}
Summary:        One-click GIS server stack installer
License:        MIT
URL:            https://github.com/GIS-GEORGIA
Source0:        gisstack-installer-%{version}.tar.gz
BuildArch:      noarch
# python3-flask lives in EPEL on RHEL/Rocky/Alma: dnf install epel-release
Requires:       python3 >= 3.9
Requires:       python3-flask
Requires:       python3-jinja2
Requires:       python3-pyyaml
Recommends:     python3-tkinter
Recommends:     openssh-clients
%global debug_package %{nil}
%global __python %{__python3}
# the payload is plain python + templates; do not byte-compile or strip
%global __os_install_post %{nil}

%description
PostGIS (with selectable extensions), GeoServer, Tomcat, MapProxy, QGIS Server and
Caddy, plus a bilingual web map and React Native / Flutter map templates. Deploys
natively (apt/dnf), with Docker/Podman Compose or on Kubernetes, from a web GUI,
a desktop GUI or the command line.

%prep
%setup -q -c -T
tar -xzf %{SOURCE0}

%install
mkdir -p %{buildroot}
cp -a usr lib %{buildroot}/

%post
systemctl daemon-reload >/dev/null 2>&1 || :
echo "gisstack installed. Start the web GUI:  gisstack web   (or: systemctl enable --now gisstack-web)"

%files
/usr/lib/gisstack
/usr/bin/gisstack
/lib/systemd/system/gisstack-web.service
%doc /usr/share/doc/gisstack-installer/README.md
