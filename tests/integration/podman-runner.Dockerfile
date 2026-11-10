# A real Podman engine under systemd for machines that have Docker but not Podman (healthchecks need systemd). Used by run_podman_tests.sh.
# rootful Podman inside a privileged container - a nested setup; a rootless host install is what the CI workflow podman.yml exercises.
FROM quay.io/podman/stable:latest
RUN dnf -y -q install systemd python3 python3-flask python3-jinja2 python3-pyyaml podman-compose gnupg2 which tar gzip findutils procps-ng >/dev/null 2>&1 && dnf clean all >/dev/null 2>&1
STOPSIGNAL SIGRTMIN+3
CMD ["/usr/sbin/init"]
