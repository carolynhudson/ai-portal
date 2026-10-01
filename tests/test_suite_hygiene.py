"""tests for the suite's own import hygiene.

Two rules that are invisible until they break, and each has already cost a whole
session's worth of confusing failures:

  * no test module imports another test module
  * conftest.py never sets a default AI_PORTAL_PASSWORD

Both were measured. A test file that wanted a GGUF writer imported
`test_modelindex`, which claims `AI_PORTAL_PASSWORD` and a throwaway
`AI_PORTAL_HOME` with `setdefault` at import; the earlier-collecting module won
the race for the session, every later module logged in with the password it
*thought* it had set, the failures were counted in the one shared throttle
table, and after ten of them unrelated pages returned 401 — 56 failures and 12
errors in test_hero, test_ui_contract, test_comfyui and others, none of which
had anything to do with auth. The fixture moved to tests/gguf_fixture.py, which
touches no environment at all.
"""

from __future__ import annotations

import ast
import pathlib

TESTS = pathlib.Path(__file__).resolve().parent


def _module_level_imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: list[str] = []
    for node in tree.body:                      # module level only, not in functions
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


def test_no_test_module_imports_another_test_module():
    """A test module's top-level environment claims are a race; importing one
    from another makes the winner depend on collection order."""
    offenders = {}
    for path in sorted(TESTS.glob("test_*.py")):
        hits = [n for n in _module_level_imports(path)
                if n.split(".")[0].startswith("test_")]
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        "test modules must share helpers through a non-test module "
        f"(see tests/gguf_fixture.py): {offenders}")


def test_conftest_sets_no_default_password():
    """conftest imports before every module, so a default it sets beats every
    module's own `setdefault` — the exact mechanism behind the 401 cascade."""
    src = (TESTS / "conftest.py").read_text()
    for i, line in enumerate(src.splitlines(), 1):
        code = line.split("#", 1)[0]
        assert "AI_PORTAL_PASSWORD" not in code, (
            f"conftest.py:{i} sets the password for the whole session: {line!r}")


def test_the_gguf_fixture_touches_no_environment():
    """The shared fixture module is the safe place to put a helper, so it has to
    stay free of the claims that make test modules unsafe to import early.
    Checked over the syntax tree, not the text: this file's own docstring names
    AI_PORTAL_HOME to explain the rule, and a text scan fails on the explanation."""
    path = TESTS / "gguf_fixture.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {(a.name or "").split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "os" not in imported and "sys" not in imported, imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "environ" not in names
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "environ" not in attrs, f"{path} reaches for os.environ"


def _unused_marker():
    """(kept deliberately: the text scan this replaced is what the docstring
    above warns about — an explanation of a rule must not trip the rule.)"""
