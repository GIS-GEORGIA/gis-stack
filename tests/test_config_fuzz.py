"""A malformed configuration must produce a ConfigError, never a Python traceback (audit 2026-11: wrong element types in `mapproxy.layers`,
`mapproxy.grids` and `postgres.extensions` raised AttributeError / TypeError).  Every setting - including every list element - is replaced by values
of the wrong type."""
import copy
import unittest

from gisstack import config

VALUES = [None, 123, -5, 3.5, True, [], {}, ["x"], "", {"a": 1}, "x\ny", "../x", "0"]


def paths(o, p=()):
    yield p
    if isinstance(o, dict):
        for k, v in o.items():
            yield from paths(v, p + (k,))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from paths(v, p + (i,))


class WrongTypesNeverCrash(unittest.TestCase):
    def test_every_setting_replaced_by_wrong_values(self):
        base = config.default_config()
        unexpected = {}
        cases = 0
        for p in paths(base):
            if not p:
                continue
            for v in VALUES:
                c = copy.deepcopy(base)
                d = c
                for k in p[:-1]:
                    d = d[k]
                d[p[-1]] = v
                cases += 1
                try:
                    config.resolve(c)
                except config.ConfigError:
                    pass
                except Exception as e:  # noqa: BLE001 - this is the thing under test
                    unexpected.setdefault((".".join(map(str, p)), type(e).__name__), repr(v)[:20])
        self.assertGreater(cases, 1000)
        self.assertEqual(unexpected, {})

    def test_the_messages_name_the_setting(self):
        for key, bad in (("mapproxy.layers", ["x"]), ("mapproxy.grids", [[]]), ("postgres.extensions", [{}, "postgis"])):
            sec, name = key.split(".")
            c = config.default_config()
            c[sec][name] = bad
            with self.assertRaises(config.ConfigError) as cm:
                config.resolve(c)
            self.assertIn(key, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
