#!/usr/bin/env bash
# Refresh the vendored (offline) Leaflet copy used by the generated web map.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="${LEAFLET_VERSION:-1.9.4}"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
(cd "$T" && npm pack "leaflet@$VER" >/dev/null && tar xzf leaflet-*.tgz)
D="$ROOT/gisstack/vendor/leaflet"
rm -rf "$D"; mkdir -p "$D"
cp -r "$T/package/dist/images" "$D/images"
cp "$T/package/dist/leaflet.js" "$T/package/dist/leaflet.css" "$D/"
cp "$T/package/LICENSE" "$D/LICENSE.txt"
echo "Leaflet $VER vendored into $D"
