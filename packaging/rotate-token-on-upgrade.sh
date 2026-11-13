#!/bin/sh
# Replace the web access token ONCE when this package is installed over a previous installation that exposed it.
#
# Releases up to 0.1.7 printed the token to the systemd journal and kept it for ever; an upgrade used to keep it valid. This script is run (as root)
# by the package's post-install step; it is safe to run any number of times:
#   * marker present            -> nothing (the installation is already protected; ordinary restarts and later upgrades never rotate)
#   * no marker, token present  -> the old token is deleted, the marker is written, running services are restarted so that the old token held in
#                                  their memory dies too; the (new, unprivileged) service creates a fresh token itself on start: owned by the
#                                  service user, mode 0600, never printed
#   * no marker, no token       -> fresh install: only the marker is written
# If the token cannot be deleted the marker is NOT written, so the next run retries, and the script still exits 0 (it must never make the package
# installation fail).  The token's content is never read, printed or passed in an argument.
#
# Recovery by hand (any time):  systemctl stop gisstack-web; rm -f /var/lib/gisstack-installer/web.token; systemctl start gisstack-web
STATE="${GISSTACK_STATE_DIR:-/var/lib/gisstack-installer}"
MARK_DIR="${GISSTACK_MARK_DIR:-/etc/gisstack-installer}"
MARK="$MARK_DIR/token-protected"

[ -e "$MARK" ] && exit 0
mkdir -p "$MARK_DIR" 2>/dev/null || { echo "gisstack: cannot create $MARK_DIR; the web token was not checked" >&2; exit 0; }

rotated=0
if [ -e "$STATE/web.token" ]; then
  if ! rm -f "$STATE/web.token" 2>/dev/null || [ -e "$STATE/web.token" ]; then
    echo "gisstack: could not delete the old web token in $STATE; run: systemctl stop gisstack-web; rm -f $STATE/web.token; systemctl start gisstack-web" >&2
    exit 0
  fi
  rotated=1
fi
( umask 022; : > "$MARK" ) 2>/dev/null || { echo "gisstack: cannot write $MARK" >&2; exit 0; }

if [ "$rotated" = 1 ]; then
  command -v systemctl >/dev/null 2>&1 && systemctl try-restart gisstack-web.service gisstack-web-root.service >/dev/null 2>&1 \
    || echo "gisstack: restart the web service to use the new token: systemctl restart gisstack-web" >&2
  echo "gisstack: the web access token of the previous installation was replaced (older releases wrote it to the journal). The new token is created when the service starts: /var/lib/gisstack-installer/web.token (read it as root)."
fi
exit 0
