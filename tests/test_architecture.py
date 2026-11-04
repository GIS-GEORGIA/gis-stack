"""Phase 2 exit criteria, enforced: module boundaries stay clean as the code base grows.

* no module reaches into another gisstack module's private names (`other._helper`, `from .other import _helper`);
* the import graph has no cycles;
* `core` (errors, paths, events, log) depends on nothing above it, and the transport/runner layer does not depend on feature modules.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "gisstack"
MODULES = {"gisstack." + p.relative_to(ROOT).with_suffix("").as_posix().replace("/", "."): p for p in ROOT.rglob("*.py") if "vendor" not in p.parts}


def _imports(path: Path, mod: str):
    """Yield (imported gisstack module, set of imported names or None for `import x`) for relative imports."""
    pkg = mod.split(".")[:-1] if path.name != "__init__.py" else mod.split(".")
    if path.name == "__init__.py":
        mod = mod.rsplit(".__init__", 1)[0]
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = pkg[: len(pkg) - node.level + 1]
            target = ".".join(base + (node.module.split(".") if node.module else []))
            for a in node.names:
                full = f"{target}.{a.name}".strip(".")
                yield (full if full in MODULES else target), a.name, node
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("gisstack"):
            yield node.module, None, node


def _graph():
    g = {m: set() for m in MODULES}
    for m, p in MODULES.items():
        for target, _name, _node in _imports(p, m):
            if target in MODULES and target != m:
                g[m].add(target)
    return g


class ArchitectureTests(unittest.TestCase):
    def test_no_private_names_are_imported_from_other_modules(self):
        bad = []
        for m, p in MODULES.items():
            for target, name, _node in _imports(p, m):
                if name and name.startswith("_") and not name.startswith("__") and target in MODULES and target != m:
                    bad.append(f"{m}: from {target} import {name}")
        self.assertEqual(bad, [])

    def test_no_attribute_access_to_private_names_of_imported_modules(self):
        bad = []
        for m, p in MODULES.items():
            tree = ast.parse(p.read_text())
            aliases = set()                            # local names bound to gisstack modules (`from . import ops as ops_mod`)
            for target, name, node in _imports(p, m):
                if name and target in MODULES and target.endswith("." + name) or (name is None and target in MODULES):
                    for a in node.names:
                        if a.name == name:
                            aliases.add(a.asname or a.name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases \
                        and node.attr.startswith("_") and not node.attr.startswith("__"):
                    bad.append(f"{m}:{node.lineno}: {node.value.id}.{node.attr}")
        self.assertEqual(bad, [])

    def test_import_graph_is_acyclic(self):
        g = _graph()
        state = {}

        def visit(n, stack):
            if state.get(n) == 2:
                return
            if state.get(n) == 1:
                self.fail("import cycle: " + " -> ".join(stack[stack.index(n):] + [n]))
            state[n] = 1
            for d in sorted(g[n]):
                visit(d, stack + [n])
            state[n] = 2
        for n in sorted(g):
            visit(n, [])

    def test_layers(self):
        g = _graph()
        for m in g:
            if m.startswith("gisstack.core."):
                outside = {d for d in g[m] if not d.startswith("gisstack.core.") and d != "gisstack.fsutil"}
                self.assertEqual(outside, set(), f"{m} must not depend on higher layers")
        for low in ("gisstack.runner", "gisstack.transport", "gisstack.fsutil", "gisstack.journal"):
            heavy = {d for d in g.get(low, ()) if d.split(".")[1] in ("deploy", "ops", "discover", "existing", "generate", "config",
                                                                          "cli", "desktop", "webgui", "preflight", "rotation")}
            self.assertEqual(heavy, set(), f"{low} must stay below the feature modules")


if __name__ == "__main__":
    unittest.main()
