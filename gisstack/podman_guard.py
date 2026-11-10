"""`podman compose` is only a wrapper: it runs whichever compose provider it finds (the Docker Compose plugin, `docker-compose`, or `podman-compose`).
The Docker Compose providers talk to the Podman *API socket*; if that socket is not running they fail with the provider's own, misleading message
("Cannot connect to the Docker daemon ..."), which cost two red CI runs.  This bash snippet names the provider in use and refuses exactly that
combination - **it never switches provider silently**: the operator picks (start the socket, or select podman-compose with PODMAN_COMPOSE_PROVIDER).
Used by the generated up.sh and by the control script, so every Podman compose call is covered."""
from __future__ import annotations

GUARD = r'''# --- compose provider check (Podman): name the provider, refuse a combination that cannot work, never switch silently
gs_podman_provider_guard() {
  local out provider sock
  out="$(podman compose version 2>&1 || true)"
  provider="$(printf '%s\n' "$out" | sed -n 's/.*Executing external compose provider "\([^"]*\)".*/\1/p' | head -n1)"
  [ -n "$provider" ] || return 0                       # provider not reported by this Podman version: nothing to judge
  echo "compose provider: $provider" >&2
  case "$(basename "$provider")" in
    docker-compose)
      # `podman info` reports RemoteSocket.Exists=true even when no socket file is there, so the file itself is checked
      sock="$(podman info --format '{{.Host.RemoteSocket.Path}}' 2>/dev/null || true)"
      sock="${sock#unix://}"
      if [ -n "$sock" ] && [ -S "$sock" ]; then return 0; fi
      {
        echo "ERROR: 'podman compose' is using the Docker Compose provider ($provider), which needs the Podman API socket, and that socket is not running${sock:+ ($sock)}."
        echo "  Nothing was started. Choose one (gis-stack does not switch provider by itself):"
        echo "    - start the socket:  systemctl --user enable --now podman.socket    (rootless)   |   sudo systemctl enable --now podman.socket   (rootful)"
        echo "    - or use podman-compose instead:  pip install podman-compose && export PODMAN_COMPOSE_PROVIDER=podman-compose"
      } >&2
      return 3 ;;
  esac
  return 0
}
'''
