"""A secret is masked in every form it commonly appears in - raw, JSON-escaped, URL-encoded, shell-quoted (audit 2026-11: only the raw text was masked, so a
password with a quote, a backslash or a space leaked through a JSON error body or a URL in a log line)."""
import json
import shlex
import unittest
import urllib.parse

from gisstack import runner

PASSWORDS = ['p"ss\\w0rd', "sp ace&amp=1", "it's-a-secret", "ünï-çödé-pw", "plain-Passw0rd"]


class Forms(unittest.TestCase):
    def test_every_form_is_masked(self):
        for pw in PASSWORDS:
            mask = runner.redactor([pw])
            shown = {
                "raw": pw,
                "json": json.dumps({"password": pw}),
                "json-ascii": json.dumps({"password": pw}, ensure_ascii=True),
                "url": "https://u:" + urllib.parse.quote(pw, safe="") + "@host/x",
                "query": "?password=" + urllib.parse.quote_plus(pw),
                "shell": "psql -W " + shlex.quote(pw),
            }
            for name, text in shown.items():
                out = mask(text)
                self.assertNotIn(pw, out, (pw, name))
                for piece in (json.dumps(pw)[1:-1], urllib.parse.quote(pw, safe=""), urllib.parse.quote_plus(pw)):
                    if len(piece) >= 4:
                        self.assertNotIn(piece, out, (pw, name, piece))

    def test_short_or_empty_secrets_are_not_used_as_masks(self):
        mask = runner.redactor(["", "abc", None])
        self.assertEqual(mask("abc abcabc"), "abc abcabc")

    def test_text_without_secrets_is_unchanged(self):
        self.assertEqual(runner.redactor(["longsecret1"])("nothing to hide here"), "nothing to hide here")


if __name__ == "__main__":
    unittest.main()
