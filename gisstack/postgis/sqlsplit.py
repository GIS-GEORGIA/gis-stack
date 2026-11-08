"""A small SQL tokenizer used for *user experience only*: counting statements, stripping a trailing `;`, spotting what a statement starts with.

It is deliberately NOT a security boundary (roadmap rule): safety comes from the database role and the read-only transaction.  It fails closed on
what it cannot read (unterminated quote or comment -> error) so the user gets a clear message instead of a confusing server error."""
from __future__ import annotations

import re

_DOLLAR = re.compile(r"\$([A-Za-z_\x80-￿][A-Za-z0-9_\x80-￿]*)?\$")


class SqlSyntax(ValueError):
    pass


def split_statements(sql: str) -> list[str]:
    """Statements of `sql` (text without the separating `;`), skipping empty ones and comment-only ones."""
    if "\x00" in sql:
        raise SqlSyntax("NUL character in statement")
    out: list[str] = []
    i, n, start = 0, len(sql), 0
    has_code = False
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif c == "/" and sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise SqlSyntax("unterminated /* comment")
        elif c in "'\"":
            has_code = True
            esc = c == "'" and i > 0 and sql[i - 1] in "eE" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            i += 1
            while True:
                if i >= n:
                    raise SqlSyntax("unterminated quoted string" if c == "'" else "unterminated quoted identifier")
                if esc and sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == c:
                    if i + 1 < n and sql[i + 1] == c:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif c == "$":
            m = _DOLLAR.match(sql, i)
            if m:
                has_code = True
                tag = m.group(0)
                j = sql.find(tag, m.end())
                if j < 0:
                    raise SqlSyntax(f"unterminated {tag} quote")
                i = j + len(tag)
            else:
                has_code = True
                i += 1
        elif c == ";":
            if has_code:
                out.append(sql[start:i].strip())
            start, has_code, i = i + 1, False, i + 1
        else:
            if not c.isspace():
                has_code = True
            i += 1
    if has_code:
        out.append(sql[start:].strip())
    return out


_LEAD = re.compile(r"^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.S)


def first_keyword(stmt: str) -> str:
    """Lower-case first word after comments/whitespace/opening parentheses ('' if none)."""
    s = _LEAD.sub("", stmt).lstrip("( \t\r\n")
    m = re.match(r"[A-Za-z_]+", s)
    return m.group(0).lower() if m else ""


PAGEABLE = {"select", "with", "values", "table"}
