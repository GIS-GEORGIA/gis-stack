#!/bin/sh
# /usr/bin/gisstack launcher (installed by the deb/rpm package)
export PYTHONPATH="/usr/lib/gisstack${PYTHONPATH:+:$PYTHONPATH}"
export GISSTACK_HOME="${GISSTACK_HOME:-$( [ "$(id -u)" = 0 ] && echo /var/lib/gisstack-installer || echo "$HOME/.gisstack" )}"
exec python3 -m gisstack "$@"
