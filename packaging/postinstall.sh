#!/bin/sh
# Post-install step shared by the deb (DEBIAN/postinst) and the nfpm rpm (the spec carries the same lines).
set -e
command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload || true
[ -x /usr/lib/gisstack/rotate-token-on-upgrade.sh ] && /usr/lib/gisstack/rotate-token-on-upgrade.sh || true
echo "gisstack installed. Start the web GUI:  gisstack web    (or the unprivileged service: systemctl enable --now gisstack-web; installing on THIS machine from the GUI needs gisstack-web-root)"
exit 0
