#!/usr/bin/env bash
# Installs the gisstack GUI/CLI from this folder (no .deb/.rpm needed).
#   sudo ./install.sh            -> /opt/gisstack-installer + /usr/local/bin/gisstack
#   ./install.sh                 -> ~/.local/share/gisstack + ~/.local/bin/gisstack
#   ./install.sh --tk            also install python3-tk (desktop GUI) without asking
#   ./install.sh --no-tk         do not offer to install python3-tk
#   ./install.sh --start         start the web GUI afterwards
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TK=0; START=0; NOTK=0
for a in "$@"; do
  case "$a" in --tk) TK=1 ;; --no-tk) NOTK=1 ;; --start) START=1 ;; -h|--help) sed -n '2,7p' "$0"; exit 0 ;; *) echo "unknown option $a" >&2; exit 2 ;; esac
done
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then PREFIX=/opt/gisstack-installer; BIN=/usr/local/bin; SUDO=""
else PREFIX="$HOME/.local/share/gisstack"; BIN="$HOME/.local/bin"; SUDO="sudo"; fi

. /etc/os-release
case " ${ID_LIKE:-} ${ID} " in
  *" debian "*|*" ubuntu "*) PM=apt ;;
  *" rhel "*|*" fedora "*|*" centos "*|*" rocky "*|*" almalinux "*) PM=dnf ;;
  *) PM=none ;;
esac

need_pkgs=()
command -v python3 >/dev/null || need_pkgs+=(python3)
if [ "$PM" = apt ]; then
  python3 -c 'import venv, ensurepip' 2>/dev/null || need_pkgs+=(python3-venv python3-pip)
  [ "$TK" = 1 ] && need_pkgs+=(python3-tk)
elif [ "$PM" = dnf ]; then
  python3 -m pip --version >/dev/null 2>&1 || need_pkgs+=(python3-pip)
  [ "$TK" = 1 ] && need_pkgs+=(python3-tkinter)
fi
if [ "${#need_pkgs[@]}" -gt 0 ]; then
  log "installing system packages: ${need_pkgs[*]}"
  if [ "$PM" = apt ]; then $SUDO apt-get update -qq && $SUDO apt-get install -y "${need_pkgs[@]}"
  elif [ "$PM" = dnf ]; then $SUDO dnf install -y "${need_pkgs[@]}"
  else echo "install these manually: ${need_pkgs[*]}" >&2; exit 1; fi
fi

log "creating virtual environment in $PREFIX"
mkdir -p "$PREFIX" "$BIN"
# --system-site-packages lets the desktop GUI see the distro's tkinter
python3 -m venv --system-site-packages "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet "$HERE"
ln -sfn "$PREFIX/venv/bin/gisstack" "$BIN/gisstack"

# The desktop GUI needs Tkinter. Ask before installing a system package; the web GUI works without it.
if ! "$PREFIX/venv/bin/python" -c 'import tkinter' 2>/dev/null; then
  TKPKG=""; TKCMD=""
  if [ "$PM" = apt ]; then TKPKG=python3-tk; TKCMD="apt-get install -y python3-tk"
  elif [ "$PM" = dnf ]; then TKPKG=python3-tkinter; TKCMD="dnf install -y python3-tkinter"; fi
  if [ -n "$TKPKG" ] && [ "$NOTK" != 1 ]; then
    case "${LC_ALL:-${LANG:-}}" in ka*) Q="Desktop GUI-ს სჭირდება $TKPKG. დავაყენო ახლავე? [y/N] ";; *) Q="The desktop GUI needs $TKPKG. Install it now? [y/N] ";; esac
    if [ -t 0 ] && [ -t 1 ]; then
      read -r -p "$Q" ans || ans=""
      case "$ans" in
        y|Y|yes|YES|კი) log "installing $TKPKG"; if [ "$PM" = apt ]; then $SUDO apt-get update -qq; fi; $SUDO $TKCMD || echo "could not install $TKPKG - run: sudo $TKCMD" >&2 ;;
        *) echo "skipped. Desktop GUI later:  sudo $TKCMD   (or use: gisstack web)" ;;
      esac
    else
      echo "note: the desktop GUI needs $TKPKG (not installed):  sudo $TKCMD   (web GUI works without it)"
    fi
  fi
fi

log "installed: $BIN/gisstack"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: add $BIN to your PATH" ;; esac
echo "  web GUI:      gisstack web"
echo "  desktop GUI:  gisstack desktop"
echo "  headless:     gisstack generate -c stack.json -o bundle && gisstack deploy -c stack.json --mode ssh --ssh root@server"
if [ "$START" = 1 ]; then exec "$BIN/gisstack" web; fi
