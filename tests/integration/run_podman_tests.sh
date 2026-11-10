#!/usr/bin/env bash
# Run the Podman integration tests against a REAL Podman engine inside a privileged systemd container (for machines that have Docker but no Podman).
#   tests/integration/run_podman_tests.sh [unittest names...]     default: tests.integration.test_stack_backup_podman
# First run pulls the Podman image and the PostGIS / GeoServer images into the named volume gspod-store (kept between runs).
# The container is removed afterwards; the volume is not (docker volume rm gspod-store).
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
command -v docker >/dev/null || { echo "docker is needed to run the Podman engine container" >&2; exit 1; }
docker build -q -t gisstack-podman-runner:2 -f tests/integration/podman-runner.Dockerfile tests/integration >/dev/null
name="gspod-$$"
docker run -d --name "$name" --privileged --cgroupns=host --tmpfs /run --tmpfs /run/lock -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v gspod-store:/var/lib/containers -v "$PWD":/src:ro -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/src gisstack-podman-runner:2 >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do docker exec "$name" systemctl is-system-running 2>/dev/null | grep -qE "running|degraded" && break; sleep 1; done
docker exec "$name" podman rm -af >/dev/null 2>&1 || true            # leftovers of an earlier interrupted run kept in the volume
docker exec -e GISSTACK_TEST_PODMAN=1 -w /src "$name" python3 -W error::ResourceWarning -m unittest -v "${@:-tests.integration.test_stack_backup_podman}"
