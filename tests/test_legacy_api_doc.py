"""docs/LEGACY-API.md must list exactly the legacy /api routes that exist (so the retirement plan cannot go stale).  Unit tested."""
import re
import unittest
from pathlib import Path

try:
    from gisstack.api import routes, routes_geoserver, routes_mapproxy, routes_monitor, routes_postgis, routes_security, routes_servers, routes_stackbackup  # noqa: F401 - importing registers the routes
    from gisstack.api.registry import registry
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False

DOC = Path(__file__).resolve().parents[1] / "docs" / "LEGACY-API.md"


@unittest.skipUnless(HAVE_FLASK, "flask needed")
class LegacyDocTests(unittest.TestCase):
    def test_every_legacy_route_is_documented_with_its_replacement_and_nothing_else(self):
        text = DOC.read_text(encoding="utf-8")
        rows = re.findall(r"^\| `(/api/[^`]+)` \| (\w+) \| `(/api/v1[^`]+)` \|", text, re.M)
        documented = {(old, method, new) for old, method, new in rows}
        actual = {(old, r.method, "/api/v1" + r.path) for r in registry.routes for old in r.legacy}
        self.assertEqual(documented, actual)
        self.assertIn("/api/v1/session/logout", text)           # even the last route that had no successor now has one

    def test_the_plan_states_the_facts_it_relies_on(self):
        text = DOC.read_text(encoding="utf-8")
        for needle in ("Deprecation: true", "successor-version", "unsafe-inline", "0.3.0"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
