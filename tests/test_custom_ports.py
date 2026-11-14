"""Custom HTTP_PORT / HTTPS_PORT in the compose `.env` (audit 2026-11, G-2): Caddy's http->https redirect and the URL GeoServer advertises must name the
port.  The real-Caddy check is tests/integration/test_custom_ports.py."""
import tempfile
import unittest
from pathlib import Path

from gisstack import config, generate


class CustomPortsGenerated(unittest.TestCase):
    def files(self, tls):
        cfg = config.resolve({**config.default_config(), "domain": "gis.test", "tls": tls})
        with tempfile.TemporaryDirectory() as d:
            generate.generate_bundle(cfg, Path(d) / "b")
            return {str(p.relative_to(Path(d) / "b")): p.read_text() for p in (Path(d) / "b").rglob("*") if p.is_file() and p.suffix in ("", ".yml", ".j2", ".env") or p.name in ("Caddyfile", ".env")}

    def test_container_ports_stay_80_and_443_and_caddy_learns_the_published_port(self):
        y = self.files("internal")["compose/docker-compose.yml"]
        self.assertIn('"${HTTP_PORT:-80}:80"', y)                      # the engine-side lookup (providers.common.public_port_prelude) asks for 443/tcp
        self.assertIn('"${HTTPS_PORT:-443}:443"', y)
        self.assertIn("HTTPS_PORT: ${HTTPS_PORT:-443}", y)

    def test_compose_caddy_redirect_names_the_https_port(self):
        c = self.files("internal")["compose/caddy/Caddyfile"]
        self.assertNotIn("https_port", c)
        self.assertIn("auto_https disable_redirects", c)
        self.assertIn("redir https://gis.test:{$HTTPS_PORT:443}{uri} permanent", c)

    def test_geoserver_advertised_url_follows_the_port(self):
        self.assertIn("PROXY_BASE_URL: https://gis.test${HTTPS_PORT:+:${HTTPS_PORT}}/geoserver", self.files("internal")["compose/docker-compose.yml"])
        self.assertIn("PROXY_BASE_URL: http://gis.test${HTTP_PORT:+:${HTTP_PORT}}/geoserver", self.files("none")["compose/docker-compose.yml"])

    def test_tls_none_has_no_redirect(self):
        c = self.files("none")["compose/caddy/Caddyfile"]
        self.assertNotIn("\tredir ", c)
        self.assertIn("auto_https off", c)

    def test_env_keeps_the_port_lines_that_scripts_and_tests_rewrite(self):
        e = self.files("internal")["compose/.env"]
        self.assertRegex(e, r"(?m)^HTTP_PORT=80$")
        self.assertRegex(e, r"(?m)^HTTPS_PORT=443$")

    def test_native_and_kubernetes_caddyfiles_are_unchanged(self):
        for name, content in self.files("internal").items():
            if name.endswith("Caddyfile") and not name.startswith("compose/"):
                self.assertNotIn("HTTPS_PORT", content, name)


if __name__ == "__main__":
    unittest.main()
