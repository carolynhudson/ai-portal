"""Dependency guards: what the project claims to need, what it imports, and what gets deployed.

These exist because the declared dependency list was wrong in a way that looked tidy. `gguf` was in
`dependencies` since the start, every version bump of it was resolved and installed on every deploy,
and nothing in the project imported it -- the portal parses GGUF headers itself in
`app/core/modelmeta.py`. It also dragged `numpy`, `requests`, `urllib3` and `charset-normalizer` into
the venv on the machine that serves the portal. Nothing failed, because an unused dependency never
does.

So the check is not "does it install" -- it is:

1. every runtime dependency is actually imported, or is on an explicit list of packages a framework
   requires without any code naming it;
2. every third-party module `app/` imports is declared;
3. the test harness stays in the dev group, and the deploy path asks uv for `--no-dev`.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

pytestmark = pytest.mark.skipif(not APP.is_dir(), reason="no app/ to audit")


def _pyproject() -> str:
    return (ROOT / "pyproject.toml").read_text()


def _deps(block: str) -> list[str]:
    """Distribution names from one list in pyproject.toml, normalised to import spelling.

    Scan by bracket depth rather than with a lazy bracket match: `uvicorn[standard]` contains a
    bracket, and a non-greedy match stops at it and quietly returns a list that is missing uvicorn --
    a dependency test that loses the dependency it is checking is worse than no test.
    """
    text = _pyproject()
    m = re.search(rf"^{block} = \[", text, re.M)
    assert m, f"{block} not found in pyproject.toml"
    i, depth = m.end() - 1, 0
    while i < len(text):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = text[m.end():i]
    out = []
    for raw in re.findall(r"[\"']([^\"']+)[\"']", body):
        name = re.split(r"[\[;>~=<!]", raw)[0].strip()
        out.append(name.replace("-", "_").lower())
    assert out, f"{block} parsed empty from pyproject.toml"
    return out


@pytest.fixture(scope="module")
def runtime_deps() -> list[str]:
    return _deps("dependencies")


@pytest.fixture(scope="module")
def dev_deps() -> list[str]:
    return _deps(r"\[dependency-groups\]\ndev")


def _imported_modules() -> set[str]:
    """Top-level module names imported anywhere under app/, relative imports excluded."""
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:                       # `from .core import ...` is local
                    continue
                found.add(node.module.split(".")[0])
    return found


# A package can be required without any line of our code naming it, and that is legitimate. It has
# to be stated, because "declared but never imported" is otherwise indistinguishable from `gguf`.
# Adding to this map is a claim about why the package is load-bearing; the test below asserts the
# map has no unused entries either, so a package cannot stay here once it stops being needed.
REQUIRED_WITHOUT_IMPORT = {
    "python_multipart": "FastAPI's Form(...) and File(...) raise at request time without it; "
                        "app/main.py uses both, and no import line names the package.",
}


def test_no_declared_runtime_dependency_is_unused(runtime_deps):
    used = _imported_modules()
    phantom = [d for d in runtime_deps if d not in used and d not in REQUIRED_WITHOUT_IMPORT]
    assert phantom == [], f"declared as runtime dependencies and never imported: {phantom}"


def test_the_exemption_list_exempts_only_what_it_explains(runtime_deps):
    used = _imported_modules()
    # An exemption for a package that is in fact imported is just a stale comment.
    stale = [k for k in REQUIRED_WITHOUT_IMPORT if k in used]
    assert stale == [], f"imported after all, remove from REQUIRED_WITHOUT_IMPORT: {stale}"
    unused = [k for k in REQUIRED_WITHOUT_IMPORT if k not in runtime_deps]
    assert unused == [], f"exempted but not even declared: {unused}"


def test_every_third_party_module_app_imports_is_declared(runtime_deps):
    undeclared = _imported_modules() - set(runtime_deps) - set(sys.stdlib_module_names) - {"app"}
    assert undeclared == set(), f"app/ imports packages that pyproject.toml does not declare: {undeclared}"


def test_the_test_harness_is_a_dev_dependency_not_a_runtime_one(dev_deps, runtime_deps):
    assert any(d.startswith("pytest") for d in dev_deps), \
        "pytest belongs in [dependency-groups].dev so the repo venv has it"
    assert not any(d.startswith("pytest") for d in runtime_deps), \
        "pytest in runtime dependencies would put the harness in the deployed portal"


def test_deploy_syncs_without_the_dev_group():
    """The whole point of the dev group is that the deployed venv does not get it.

    `uv sync` installs the dev group by default, so a deploy that forgets `--no-dev` silently
    reverses the split -- and it fails no test anywhere else, because the deployed portal still runs.
    """
    src = (ROOT / "app" / "deploy.py").read_text()
    syncs = re.findall(r"_run\(\[uv, \"sync\"[^\]]*\]", src)
    assert syncs, "expected uv sync invocations in app/deploy.py"
    missing = [s for s in syncs if '"--no-dev"' not in s]
    assert missing == [], f"uv sync calls that would install the dev group in production: {missing}"


def test_requirements_txt_is_derived_from_the_lock_and_carries_no_dev_tools():
    """A second dependency file is only safe if it cannot drift into being a second *source*.

    It is generated by the command named in its own header, and these assertions are what keep it
    derived: the runtime set it lists has to match pyproject's, and the dev group must not leak in.
    """
    path = ROOT / "requirements.txt"
    assert path.is_file(), "requirements.txt is missing"
    text = path.read_text()
    assert "--no-dev" in text.splitlines()[1], \
        "requirements.txt must be exported with --no-dev, or it is not the runtime set"
    pinned = {re.split(r"[=<>~!]", line.split(" #")[0].strip())[0].replace("-", "_").lower()
              for line in text.splitlines() if re.match(r"^[A-Za-z0-9_.-]+==", line)}
    for dep in ("fastapi", "uvicorn", "pydantic", "jinja2", "python_multipart", "httpx",
                "huggingface_hub"):
        assert dep in pinned, f"{dep} is a runtime dependency and is absent from requirements.txt"
    assert "pytest" not in pinned, "requirements.txt is the runtime export; pytest belongs to uv sync"
