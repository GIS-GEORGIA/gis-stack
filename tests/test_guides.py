"""Georgian and English guides must stay in step (Phase 12): same files, same heading structure, byte-identical code blocks, working relative links."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ROOT / "docs" / "guides"
NAMES = ("INSTALL.md", "UPGRADE.md", "BACKUP.md", "TROUBLESHOOTING.md")


def structure(text: str) -> tuple[list[int], list[str]]:
    """(heading levels in order, fenced code blocks in order) - fences inside code are not headings."""
    levels, blocks, cur, inside = [], [], [], False
    for line in text.splitlines():
        if line.startswith("```"):
            if inside:
                blocks.append("\n".join(cur))
                cur = []
            inside = not inside
        elif inside:
            cur.append(line)
        elif m := re.match(r"^(#{1,6}) ", line):
            levels.append(len(m.group(1)))
    return levels, blocks


class GuideTests(unittest.TestCase):
    def test_every_guide_exists_in_both_languages(self):
        for lang in ("en", "ka"):
            self.assertEqual(sorted(p.name for p in (GUIDES / lang).glob("*.md")), sorted(NAMES), lang)

    def test_structure_and_commands_match(self):
        for n in NAMES:
            en, ka = (GUIDES / "en" / n).read_text(), (GUIDES / "ka" / n).read_text()
            self.assertEqual(structure(en)[0], structure(ka)[0], f"{n}: headings differ")
            self.assertEqual(structure(en)[1], structure(ka)[1], f"{n}: code blocks differ")

    def test_languages_are_what_they_claim(self):
        for n in NAMES:
            self.assertFalse(re.search("[Ⴀ-ჿ]", (GUIDES / "en" / n).read_text()), f"{n}: Georgian text in the English guide")
            ka = (GUIDES / "ka" / n).read_text()
            self.assertGreater(len(re.findall("[ა-ჿ]", ka)), 400, f"{n}: Georgian guide is not translated")

    def test_relative_links_resolve(self):
        for lang in ("en", "ka"):
            for n in NAMES:
                f = GUIDES / lang / n
                for target in re.findall(r"\]\((?!https?:|#)([^)#]+)", f.read_text()):
                    self.assertTrue((f.parent / target).resolve().exists(), f"{f.relative_to(ROOT)}: broken link {target}")

    def test_documented_commands_exist(self):
        """Every `gisstack <command>` a guide shows must be a real CLI sub-command."""
        import contextlib
        import io

        from gisstack import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        known = set(max(re.findall(r"\{([a-z\-,]+)\}", out.getvalue()), key=len).split(","))
        for n in NAMES:
            for block in structure((GUIDES / "en" / n).read_text())[1]:
                for m in re.finditer(r"^(?:sudo )?gisstack (\S+)", block, re.M):
                    if not m.group(1).startswith("-"):
                        self.assertIn(m.group(1), known, f"{n}: unknown command {m.group(1)}")


if __name__ == "__main__":
    unittest.main()
