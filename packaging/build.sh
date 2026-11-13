#!/usr/bin/env bash
# Build .deb and .rpm packages.
#   packaging/build.sh            both (rpm only if rpmbuild or nfpm is available)
#   packaging/build.sh deb | rpm
# Output: dist/gisstack-installer_<ver>_all.deb , dist/gisstack-installer-<ver>-1.noarch.rpm
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="$(python3 -c 'import re;print(re.search(r"__version__ = \"([^\"]+)\"", open("gisstack/__init__.py").read()).group(1))')"
WHAT="${1:-all}"
STAGE="$ROOT/build/stage"
DIST="$ROOT/dist"
rm -rf "$STAGE"; mkdir -p "$STAGE" "$DIST"

# ---- common payload (same tree for deb and rpm)
install -d "$STAGE/usr/lib/gisstack" "$STAGE/usr/bin" "$STAGE/lib/systemd/system" "$STAGE/usr/share/doc/gisstack-installer"
cp -r gisstack "$STAGE/usr/lib/gisstack/"
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +
python3 -m compileall -q "$STAGE/usr/lib/gisstack" >/dev/null 2>&1 || true
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +   # keep the package arch/py independent
install -m 755 packaging/gisstack.sh "$STAGE/usr/bin/gisstack"
install -m 644 packaging/gisstack-web.service "$STAGE/lib/systemd/system/gisstack-web.service"
install -m 644 packaging/gisstack-web-root.service "$STAGE/lib/systemd/system/gisstack-web-root.service"
install -m 755 packaging/rotate-token-on-upgrade.sh "$STAGE/usr/lib/gisstack/rotate-token-on-upgrade.sh"
install -m 644 README.md "$STAGE/usr/share/doc/gisstack-installer/README.md"
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE/usr/lib/gisstack" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/usr/lib/gisstack/rotate-token-on-upgrade.sh"      # after the blanket chmod: the post-install step tests -x

build_deb() {
  command -v dpkg-deb >/dev/null || { echo "dpkg-deb not found, skipping deb" >&2; return 1; }
  local D="$ROOT/build/deb"; rm -rf "$D"; cp -a "$STAGE" "$D"
  install -d "$D/DEBIAN"
  local SIZE; SIZE="$(du -sk "$D" | cut -f1)"
  cat > "$D/DEBIAN/control" <<EOF
Package: gisstack-installer
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), python3-flask, python3-jinja2, python3-yaml
Recommends: python3-waitress, python3-tk, openssh-client
Installed-Size: $SIZE
Maintainer: GIS-GEORGIA <noreply@example.invalid>
Homepage: https://github.com/GIS-GEORGIA
Description: One-click GIS server stack installer
 PostGIS (with selectable extensions), GeoServer, Tomcat, MapProxy, QGIS Server and
 Caddy, plus a bilingual web map and React Native / Flutter map templates.
 Deploys natively (apt/dnf), with Docker/Podman Compose or on Kubernetes, from a web
 GUI, a desktop GUI or the command line.
EOF
  install -m 755 packaging/postinstall.sh "$D/DEBIAN/postinst"
  dpkg-deb --root-owner-group --build "$D" "$DIST/gisstack-installer_${VERSION}_all.deb" >/dev/null
  echo "built $DIST/gisstack-installer_${VERSION}_all.deb"
}

build_rpm() {
  if command -v rpmbuild >/dev/null; then
    local R="$ROOT/build/rpm"; rm -rf "$R"; mkdir -p "$R"/{SOURCES,SPECS,BUILD,RPMS,SRPMS}
    tar -C "$STAGE" -czf "$R/SOURCES/gisstack-installer-$VERSION.tar.gz" .
    sed "s/@VERSION@/$VERSION/" packaging/gisstack.spec > "$R/SPECS/gisstack.spec"
    rpmbuild --define "_topdir $R" -bb "$R/SPECS/gisstack.spec" >/dev/null
    cp "$R"/RPMS/noarch/*.rpm "$DIST/"
    echo "built $(ls "$DIST"/*.rpm | tail -1)"
  elif command -v nfpm >/dev/null; then
    VERSION="$VERSION" STAGE="$STAGE" nfpm package -f packaging/nfpm.yaml -p rpm -t "$DIST"
  else
    echo "rpmbuild / nfpm not found - skipping rpm. On Fedora/RHEL: sudo dnf install rpm-build" >&2
    return 1
  fi
}

rc=0
case "$WHAT" in
  deb) build_deb || rc=1 ;;
  rpm) build_rpm || rc=1 ;;
  all) build_deb || rc=1; build_rpm || true ;;
  *) echo "usage: $0 [deb|rpm|all]" >&2; exit 2 ;;
esac
exit $rc
