"""Projects: name rule, toolchain honesty, bounded init, registry plus scan (§6.14.1n).

A project name becomes a directory, so the name rule is the security boundary; the projects root
decides where an agent may write, so it is validated by `portalsettings`; and an init step whose
toolchain is absent is refused *with the install command* rather than offered as a live button.
Every test here runs against a temporary projects root — nothing touches the operator's
`~/ai-projects`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.core import db, portalsettings, projects


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """A home, a portal home, a projects root, and a database in a temporary home."""
    home = tmp_path / "home"
    (home / "ai").mkdir(parents=True)
    root = home / "ai-projects"
    root.mkdir()
    monkeypatch.setattr(portalsettings, "home", lambda: str(home))
    monkeypatch.setattr(portalsettings, "portal_home", lambda: str(home / "ai"))
    # The registry lives in the portal database. `db.init()` creates the table (a core test reaches
    # `db.execute` without the app starting up), and the DELETE isolates the rows: the suite's home
    # is process-wide, so a shared registry would make every "the list is exactly this" assertion
    # depend on which test ran first.
    db.init()
    db.execute("DELETE FROM projects")
    # The root is redirected by *storing the setting*, the way the operator does it from the page.
    # That keeps the real validator in the path; patching `projects_root` would hide the check the
    # tests exist to prove.
    db.set_setting("projects_root", str(root))
    # `projects_root` is deliberately **not** patched: with only `home` redirected, the real
    # function resolves to this fixture's root, so an explicit root passed to `create()` still runs
    # through the real validator. Patching it away would hide the check the tests exist to prove.
    return root


def _tools(**present):
    """A toolchain census where only the named tools are present.

    `gpp` stands in for `g++`, which is not an identifier. A helper that silently accepted a
    misspelled tool name would answer `present: False` for exactly the tool a test was trying to
    simulate, and the test would then pass by proving nothing.
    """
    named = dict(present)
    named["g++"] = named.pop("gpp", False)
    out = {}
    for t in projects.TOOLS:
        entry: dict[str, object] = {"present": bool(named.get(t)),
                                    "path": f"/usr/bin/{t}" if named.get(t) else None}
        if not named.get(t) and t in projects.INSTALL_HINT:
            entry["hint"] = projects.INSTALL_HINT[t]
        out[t] = entry
    return out


# --- the name rule ------------------------------------------------------------------------

def test_a_name_is_a_folder_name(sandbox):
    assert projects.validate_name("my-app") == "my-app"
    assert projects.validate_name("app2") == "app2"


@pytest.mark.parametrize("bad", ["My-App", "-app", "app-", "a", "app--1", "app/x", "../escape",
                                 ".hidden", "app x", "app\x00", "x" * 65])
def test_a_name_that_is_not_a_folder_name_is_refused_with_the_rule(sandbox, bad):
    with pytest.raises(projects.ProjectError) as e:
        projects.validate_name(bad)
    assert "lowercase letters, digits and single hyphens" in str(e.value)


def test_a_non_string_name_is_refused(sandbox):
    with pytest.raises(projects.ProjectError):
        projects.validate_name(None)
    with pytest.raises(projects.ProjectError):
        projects.validate_name(["my-app"])


def test_a_refused_name_never_creates_a_folder(sandbox):
    with pytest.raises(projects.ProjectError):
        projects.create("../escape", ["readme"])
    assert list(sandbox.iterdir()) == []


# --- toolchain honesty --------------------------------------------------------------------

def test_a_tool_that_is_not_on_the_path_is_reported_absent(sandbox, monkeypatch):
    """The probe's negative answer, asserted directly.

    A probe that reports every tool as present passes every "the plan refused cpp" test, because
    those tests feed it a census of their own. This one feeds it the real box.
    """
    monkeypatch.setattr(projects.shutil, "which", lambda tool: None)
    census = projects.toolchains()
    assert all(v["present"] is False for v in census.values()), census
    assert all(census[t].get("hint") for t in projects.INSTALL_HINT), "an absent tool must come with the install statement"


def test_the_plan_runs_what_this_box_can_do_and_refuses_the_rest_with_the_install_command(sandbox):
    plan = projects.plan_init(["cpp", "python"], _tools(git=True, uv=True, cmake=True, gpp=True))
    assert plan["run"] == ["python", "cpp"], plan
    assert plan["refused"] == [], plan

    plan = projects.plan_init(["cpp", "agents"], _tools(uv=True))
    assert plan["run"] == ["agents"], "the box that can run must not be taken by the one that cannot"
    refused = {r["step"]: r["reason"] for r in plan["refused"]}
    assert "cpp" in refused and "cmake" in refused["cpp"], refused


def test_a_missing_toolchain_does_not_abort_the_other_boxes(sandbox):
    plan = projects.plan_init(["node", "readme"], _tools(node=True))
    assert plan["run"] == ["readme"], "a missing toolchain must not take README with it"
    assert {r["step"] for r in plan["refused"]} == {"node"}, plan["refused"]


def test_an_unknown_step_is_refused_and_names_the_ones_the_page_offers(sandbox):
    with pytest.raises(projects.ProjectError) as e:
        projects.plan_init(["deploy_kubernetes"], _tools())
    assert "AGENTS.md" not in str(e.value)
    assert "git" in str(e.value) and "readme" in str(e.value)


def test_a_non_list_of_steps_is_refused(sandbox):
    with pytest.raises(projects.ProjectError):
        projects.plan_init("git", _tools())


def test_the_toolchain_probe_reports_what_it_found_and_the_version_line(sandbox):
    tc = projects.toolchains()
    assert tc["git"]["present"] is True and tc["git"]["version"], "git is on this box; the probe must see it"
    for ghost in ("cargo", "go", "deno"):
        if not tc[ghost]["present"]:
            assert tc[ghost]["hint"], "a missing toolchain must come with the install statement"
        assert set(tc[ghost]) >= {"present"}


# --- creation -----------------------------------------------------------------------------

def test_create_makes_the_folder_and_the_files_and_registers_it(sandbox):
    out = projects.create("spike-app", ["node", "readme", "agents"], root=str(sandbox))
    assert out["ok"], out
    target = sandbox / "spike-app"
    assert (target / "README.md").read_text().startswith("# spike-app")
    assert "node_modules/" in (target / ".gitignore").read_text()
    assert "## Commands" in (target / "AGENTS.md").read_text()
    assert out["refused"] == []
    rows = projects.list_projects()
    assert [r["name"] for r in rows] == ["spike-app"]
    assert rows[0]["facts"]["readme"] is True and rows[0]["facts"]["agents"] is True


def test_the_project_name_is_substituted_into_the_files_it_writes(sandbox):
    projects.create("widget", ["cpp"], root=str(sandbox))
    text = (sandbox / "widget" / "CMakeLists.txt").read_text()
    assert "project(widget CXX)" in text and "${name}" not in text


def test_an_existing_folder_is_refused_and_points_at_adopt(sandbox):
    (sandbox / "taken").mkdir()
    with pytest.raises(projects.ProjectError) as e:
        projects.create("taken", ["readme"], root=str(sandbox))
    assert "already exists" in str(e.value) and "Adopt" in str(e.value)


def test_a_failing_step_leaves_the_folder_and_names_the_step(sandbox, monkeypatch):
    calls = {"n": 0}
    real = projects._run

    def fake(argv, cwd, env):
        calls["n"] += 1
        if argv[0] == "npm":
            return {"code": 127, "stdout": "", "stderr": "npm: command not found (simulated)", "seconds": 0.1}
        return real(argv, cwd, env)

    monkeypatch.setattr(projects, "_run", fake)
    out = projects.create("half-built", ["node", "readme"], root=str(sandbox))
    assert not out["ok"]
    assert out["failed"]["step"] == "node" and out["failed"]["code"] == 127
    target = sandbox / "half-built"
    assert target.is_dir(), "nothing is deleted behind a failed step"
    assert (target / "tsconfig.json").is_file()
    assert not (target / "README.md").exists(), "steps after the failure do not run"
    assert "nothing was deleted" in out["warning"] and "npm" in out["warning"]
    assert [r["name"] for r in projects.list_projects()] == ["half-built"], "a half project is a fact the page can show"


def test_git_init_runs_with_the_project_as_its_working_directory(sandbox):
    projects.create("repo-app", ["git"], root=str(sandbox))
    assert (sandbox / "repo-app" / ".git").is_dir()
    assert projects.list_projects()[0]["facts"]["git"] is True


def test_create_refuses_a_root_the_validator_refuses(sandbox):
    """A relative root is the case the plan warns about: the service starts in its own working
    directory, so a relative root resolves to a folder the page cannot see."""
    with pytest.raises(projects.ProjectError) as e:
        projects.create("x-app", ["readme"], root="apps/x-app")
    assert "projects root is not usable" in str(e.value) and "relative path" in str(e.value)
    assert list(sandbox.iterdir()) == []


def test_a_name_the_shortlist_writes_can_never_escape_the_project_folder(sandbox):
    for bad in ("../escape", "/etc/passwd", "a/../../b", "x\x00"):
        with pytest.raises(projects.ProjectError):
            projects._safe_join(str(sandbox / "p"), bad)
    assert projects._safe_join(str(sandbox / "p"), "src").endswith(os.path.join("p", "src"))


# --- registry plus scan -------------------------------------------------------------------

def test_a_folder_created_by_hand_is_discovered_and_adoptable(sandbox):
    (sandbox / "by-hand").mkdir()
    found = projects.scan(str(sandbox))
    assert [f["path"] for f in found] == [os.path.realpath(str(sandbox / "by-hand"))]
    assert found[0]["adoptable"] is True

    out = projects.adopt(str(sandbox / "by-hand"))
    assert out["ok"]
    assert [r["name"] for r in projects.list_projects()] == ["by-hand"]
    assert projects.scan(str(sandbox)) == [], "an adopted folder stops being unadopted"


def test_scan_skips_the_registrys_own_rows_and_dotfolders(sandbox):
    projects.create("registered", ["readme"], root=str(sandbox))
    (sandbox / ".cache").mkdir()
    (sandbox / "loose-file.txt").write_text("x")
    assert projects.scan(str(sandbox)) == []


def test_a_folder_whose_name_is_not_a_project_name_is_reported_as_not_adoptable(sandbox):
    (sandbox / "Bad Name").mkdir()
    found = projects.scan(str(sandbox))
    assert [f for f in found if f["name"] == "Bad Name"][0]["adoptable"] is False
    with pytest.raises(projects.ProjectError):
        projects.adopt(str(sandbox / "Bad Name"))


def test_adoption_refuses_a_folder_outside_the_projects_root(sandbox):
    outside = sandbox.parent / "outside-app"
    outside.mkdir()
    with pytest.raises(projects.ProjectError) as e:
        projects.adopt(str(outside))
    assert "not inside the projects root" in str(e.value)


def test_adoption_refuses_the_root_itself(sandbox):
    with pytest.raises(projects.ProjectError) as e:
        projects.adopt(str(sandbox))
    assert "projects root itself" in str(e.value)


def test_forgetting_a_project_deletes_nothing(sandbox):
    projects.create("keepme", ["readme"], root=str(sandbox))
    target = sandbox / "keepme"
    out = projects.remove(str(target))
    assert out["ok"] and out["deleted_from_disk"] is False
    assert target.is_dir() and (target / "README.md").is_file()
    assert projects.list_projects() == []


def test_the_summary_reports_the_root_and_what_is_missing_from_it(sandbox):
    projects.create("one", ["readme"], root=str(sandbox))
    (sandbox / "two").mkdir()
    s = projects.summary()
    assert s["registered"] == 1 and s["unadopted"] == 1
    assert s["root_ok"] is True and s["root"] == str(sandbox)


def test_notes_record_the_steps_that_ran_and_the_ones_refused(sandbox, monkeypatch):
    monkeypatch.setattr(projects, "toolchains", lambda: _tools(readme=True))
    projects.create("noted", ["readme", "cpp"], root=str(sandbox))
    row = projects.list_projects()[0]
    assert row["notes"]["init"] == ["readme"]
    assert row["notes"]["refused"][0]["step"] == "cpp"


def test_a_notes_column_that_is_not_json_survives_the_read(sandbox):
    projects.create("legacy", ["readme"], root=str(sandbox))
    row = projects.list_projects()[0]
    db.execute("UPDATE projects SET notes='not json' WHERE path=?", (row["path"],))
    assert projects.list_projects()[0]["notes"] == {}


# --- the endpoints (D1: every read is also an endpoint) ------------------------------------

def _client():
    from fastapi.testclient import TestClient

    from app.core import config as cfg
    from app.main import app
    c = TestClient(app)
    c.cookies.set(cfg.COOKIE_NAME, cfg.make_token(cfg.load()))
    return c


def test_the_project_endpoints_require_auth():
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)
    assert c.get("/api/projects", follow_redirects=False).status_code == 401
    for path, body in [("/api/projects", {"name": "x"}), ("/api/projects/plan", {"steps": []}),
                       ("/api/projects/adopt", {"path": "/x"}),
                       ("/api/projects/forget", {"path": "/x", "confirm": True})]:
        assert c.post(path, json=body, follow_redirects=False).status_code == 401, path


def test_the_read_endpoint_answers_the_page(sandbox):
    c = _client()
    r = c.get("/api/projects")
    assert r.status_code == 200, r.text
    d = r.json()
    for key in ("projects", "scan", "toolchains", "steps", "order", "summary"):
        assert key in d, key
    assert d["steps"]["git"]["label"] == "Git repository"
    assert "git" in d["toolchains"] and d["toolchains"]["git"]["present"] is True


def test_the_plan_endpoint_answers_without_creating(sandbox):
    c = _client()
    r = c.post("/api/projects/plan", json={"steps": ["cpp", "readme"]})
    assert r.status_code == 200, r.text
    assert list(sandbox.iterdir()) == [], "plan creates nothing"
    assert "readme" in r.json()["run"]


def test_the_plan_endpoint_refuses_an_unknown_step_by_name(sandbox):
    c = _client()
    r = c.post("/api/projects/plan", json={"steps": ["nope"]})
    assert r.status_code == 409
    assert "nope" in r.json()["detail"] and "readme" in r.json()["detail"]


def test_create_through_the_endpoint_answers_409_with_the_name_rule(sandbox):
    c = _client()
    r = c.post("/api/projects", json={"name": "Bad Name", "steps": []})
    assert r.status_code == 409, r.text
    assert "lowercase letters" in r.json()["detail"]
    assert list(sandbox.iterdir()) == []


def test_create_through_the_endpoint_creates_and_logs_the_event(sandbox):
    c = _client()
    r = c.post("/api/projects", json={"name": "api-app", "steps": ["readme"]})
    assert r.status_code == 200, r.text
    assert (sandbox / "api-app" / "README.md").is_file()
    events = db.query("SELECT kind, detail FROM events WHERE actor='project' ORDER BY id")
    assert events and events[-1]["kind"] == "create"
    detail = events[-1]["detail"]
    assert "name=api-app" in detail and "ran=readme" in detail, detail


def test_forgetting_requires_confirm(sandbox):
    projects.create("confirmme", ["readme"], root=str(sandbox))
    c = _client()
    r = c.post("/api/projects/forget", json={"path": str(sandbox / "confirmme"), "confirm": False})
    assert r.status_code == 409 and "confirm" in r.json()["detail"]
    assert projects.list_projects() != []

    r = c.post("/api/projects/forget", json={"path": str(sandbox / "confirmme"), "confirm": True})
    assert r.status_code == 200 and projects.list_projects() == []
    assert (sandbox / "confirmme").is_dir(), "the endpoint forgets; it does not delete"


def test_adoption_through_the_endpoint_refuses_a_path_outside_the_root(sandbox):
    c = _client()
    r = c.post("/api/projects/adopt", json={"path": str(sandbox.parent)})
    assert r.status_code == 409
    assert "projects root" in r.json()["detail"]


def test_a_half_created_project_is_answered_as_a_failure_the_operator_can_act_on(sandbox, monkeypatch):
    """The endpoint's side of §6.14.1n: a failed step is 409 with the step named and the folder
    declared, never a 200 over a half-built project."""
    real = projects._run

    def fake(argv, cwd, env):
        if argv[0] == "npm":
            return {"code": 127, "stdout": "", "stderr": "npm: command not found (simulated)", "seconds": 0.1}
        return real(argv, cwd, env)

    monkeypatch.setattr(projects, "_run", fake)
    c = _client()
    r = c.post("/api/projects", json={"name": "half-api", "steps": ["node", "readme"]})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "node" in detail and "nothing was deleted" in detail, detail
    assert (sandbox / "half-api").is_dir(), "the folder is reported, not hidden"
    assert not (sandbox / "half-api" / "README.md").exists()


# --- opening text and a git remote (§6.14.1n, the operator's additions) --------------------
#
# The operator added the project to GitHub and asked for: initial text for README.md and AGENTS.md,
# and a remote field with the option to pull first. The pull order is the design: the remote's
# files arrive **before** the init templates are written, and the file loop leaves an existing file
# alone — which is exactly the merge rule a fresh clone needs.

def _git(cwd, *argv):
    return subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True, text=True,
                          check=True, stdin=subprocess.DEVNULL)


@pytest.fixture
def upstream(tmp_path):
    """A real git repository with its own README, on its default branch `main`."""
    src = tmp_path / "upstream"
    src.mkdir()
    _git(src, "init", "-q", ".")
    _git(src, "config", "user.email", "upstream@example.invalid")
    _git(src, "config", "user.name", "Upstream")
    (src / "README.md").write_text("# Upstream\n\nthe repository's own text\n")
    (src / "LICENSE").write_text("MIT\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "seed")
    _git(src, "branch", "-m", "main")
    return src


def test_the_text_fields_replace_the_templates(sandbox):
    out = projects.create("with-text", ["git", "readme"], root=str(sandbox),
                          texts={"readme": "# My project\n\nBuilt on the portal.\n"})
    readme = Path(out["path"], "README.md").read_text()
    assert readme.startswith("# My project")
    assert "the repository's" not in readme
    # a box that was not ticked is not written: text implies its own file, not the whole set
    assert not Path(out["path"], "AGENTS.md").exists()


def test_supplying_text_implies_the_box(sandbox):
    out = projects.create("text-only", [], root=str(sandbox),
                          texts={"agents": "# Rules\n\nrun the tests\n"})
    assert "agents" in out["ran"]
    assert "run the tests" in Path(out["path"], "AGENTS.md").read_text()
    assert not Path(out["path"], "README.md").exists()


def test_text_length_is_bounded(sandbox):
    with pytest.raises(projects.ProjectError) as exc:
        projects.create("long-text", [], root=str(sandbox), texts={"readme": "x" * 30000})
    assert "over the 20000 limit" in str(exc.value)
    assert not (sandbox / "long-text").exists()


@pytest.mark.parametrize("url", ["ext::sh -c whoami", "ext::true", "https://user:pw@host/a.git",
                                 "", "   ", "github.com/a/b", "https://host/a b", "file://",
                                 "a" * 600])
def test_a_remote_is_refused_before_git_sees_it(url):
    with pytest.raises(projects.ProjectError):
        projects.validate_remote(url)


@pytest.mark.parametrize("url", ["git@github.com:you/your-repo.git", "https://github.com/a/b.git",
                                 "ssh://git@github.com/you/repo.git", "git@host:/srv/repo.git",
                                 "https://user@host/a.git"])
def test_the_remote_shapes_the_portal_accepts(url):
    assert projects.validate_remote(url) == url


def test_the_remote_is_added_as_origin_with_its_own_default_branch(sandbox, upstream):
    out = projects.create("attached", ["git", "readme"], root=str(sandbox),
                          remote=f"file://{upstream}")
    assert out["ok"] and out["remote"]["added"] and not out["remote"]["pulled"]
    assert out["remote"]["branch"] == "main"
    assert _git(Path(out["path"]), "remote", "-v").stdout.count("origin") == 2
    # no pull: the portal's own README template is what the folder has
    assert "What this project is" in Path(out["path"], "README.md").read_text()


def test_a_remote_implies_the_git_box(sandbox, upstream):
    out = projects.create("no-git-box", ["readme"], root=str(sandbox), remote=f"file://{upstream}")
    assert "git" in out["ran"] and out["remote"]["added"]


def test_pulling_first_keeps_the_repositorys_files(sandbox, upstream):
    out = projects.create("pulled", ["git", "readme", "agents"], root=str(sandbox),
                          texts={"readme": "# MINE\n\nmy text"}, remote=f"file://{upstream}",
                          pull_first=True)
    assert out["ok"] and out["remote"]["pulled"]
    target = Path(out["path"])
    # the repository's README wins over the operator's opening text, and its files are in place
    assert (target / "README.md").read_text().startswith("# Upstream")
    assert (target / "LICENSE").exists()
    # the templates fill only the gaps
    assert (target / "AGENTS.md").exists() and (target / ".gitignore").exists()
    assert "seed" in _git(target, "log", "--oneline").stdout


def test_a_remote_that_does_not_answer_leaves_a_usable_project(sandbox):
    out = projects.create("dead-remote", ["git", "readme"], root=str(sandbox),
                          remote="file:///does/not/exist.git")
    assert not out["ok"] and out["remote"]["added"] is False
    assert (sandbox / "dead-remote" / "README.md").exists()
    assert "The remote is not configured" in out["warning"]
    # the project is in the registry: a remote is not a reason to forget a folder
    assert [r["path"] for r in projects.list_projects()] == [out["path"]]


# --- the endpoint carries the text and the remote (D1: the page's verbs are the API's verbs) ---

def test_the_create_endpoint_carries_text_and_a_remote(sandbox, upstream):
    c = _client()
    r = c.post("/api/projects", json={
        "name": "api-pulled",
        "steps": ["git", "readme"],
        "readme_text": "# From the page\n\nthe operator's own first line\n",
        "remote": f"file://{upstream}",
        "pull_first": True,
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["remote"]["added"] and out["remote"]["pulled"]
    readme = Path(out["path"], "README.md").read_text()
    # the pull happens before the files are written, so the repository's README is what survives
    assert readme.startswith("# Upstream")

    bad = c.post("/api/projects", json={"name": "api-refused", "steps": ["git"],
                                        "remote": "ext::sh -c whoami"})
    assert bad.status_code == 409
    assert "ext::" in bad.json()["detail"]
    assert not (sandbox / "api-refused").exists()


def test_a_remote_that_does_not_answer_is_a_warning_not_a_refusal(sandbox):
    c = _client()
    r = c.post("/api/projects", json={"name": "api-dead-remote", "steps": ["git", "readme"],
                                      "remote": "file:///no/such/repo.git"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "The remote is not configured" in detail
    # the folder and its files are left: the warning is a statement, not an undo
    assert (sandbox / "api-dead-remote" / "README.md").exists()


# --- .gitignore is a consequence of the boxes, not a box (the operator's rule) ---------
#
# The operator's words: checking "git repository" should produce .gitignore, filled in
# from the stacks that were picked, and checking the other boxes should fill it further.
# A checkbox asks the operator to remember that a git repository without ignores will
# commit its own .venv; the portal knows which stacks it was asked to install, so the
# file is derived. The file is also derived on a project that arrives from a remote —
# `git clone` carries no ignores the portal can rely on, and a project whose first
# commit includes node_modules is the failure the file exists to prevent.

def test_gitignore_is_not_a_checkbox_the_operator_has_to_think_of():
    steps = list(projects.INIT_STEPS)
    assert "gitignore" not in steps, "the shortlist still asks the operator to tick the ignores"
    assert projects.INIT_ORDER == ("git", "python", "node", "cpp", "readme", "agents")


def test_checking_git_alone_writes_the_base_ignores(sandbox):
    target = sandbox / "base-only"
    projects.create("base-only", ["git"], root=str(sandbox))
    text = (target / ".gitignore").read_text()
    for needle in projects._GITIGNORE_BASE:
        assert needle in text.splitlines(), f"the base ignores are missing {needle!r}"
    # No stack section: the operator picked nothing that needs one.
    assert "node_modules/" not in text
    assert "__pycache__/" not in text


def test_the_ignores_are_filled_from_the_stacks_that_were_picked(sandbox):
    projects.create("stacks", ["git", "python", "node"], root=str(sandbox))
    text = (sandbox / "stacks" / ".gitignore").read_text()
    assert "node_modules/" in text and "__pycache__/" in text and ".venv/" in text
    # C++ was not picked, so its build output is not in a C++-less project.
    assert "CMakeCache.txt" not in text
    # Labelled sections, so the file reads as an account of the choices and an agent can
    # extend one section without guessing which stack a line belongs to.
    for label in ("Python", "Node"):
        assert f"# {label}" in text


def test_the_ignores_are_written_even_when_git_is_not_ticked(sandbox):
    """A folder with no .gitignore is a folder that will get a git repository later. The
    file is cheap, it is derived, and writing it costs nothing the operator asked to avoid."""
    target = sandbox / "no-git"
    projects.create("no-git", ["readme"], root=str(sandbox))
    assert (target / ".gitignore").exists()
    assert ".env" in (target / ".gitignore").read_text()


def test_adopted_steps_are_what_the_ignores_are_composed_from(sandbox):
    """Adoption records the init steps a folder was built from. A portal that adopted a Node
    project and then composed its ignores without that fact would write a file that ignores
    nothing the folder actually produces."""
    target = sandbox / "adopted-node"
    target.mkdir()
    projects.register(str(target))
    assert projects.init_of(str(target)) == []

    projects.register(str(target), notes={"init": ["git", "node"]})
    assert projects.init_of(str(target)) == ["git", "node"]
    text = projects.compose_gitignore(projects.init_of(str(target)))
    assert "node_modules/" in text and "__pycache__/" not in text

    # A folder the registry does not know composes nothing: inventing steps for a folder the
    # portal never saw is the invention this whole page refuses everywhere else.
    assert projects.init_of(str(sandbox / "unknown")) == []
    assert projects.compose_gitignore([]) == "\n".join(projects._GITIGNORE_BASE) + "\n"


def test_the_derived_ignores_are_the_same_whether_the_stacks_came_from_ticks_or_from_adoption():
    text = projects.compose_gitignore(["git", "node"])
    assert "node_modules/" in text and "__pycache__/" not in text


def test_a_project_pulled_from_a_remote_gets_the_ignores_too(sandbox, upstream):
    """`git clone` carries no ignores the portal can rely on. The pull lands first, the
    derived file goes in after, and a repository that already has its own keeps it."""
    projects.create("from-remote", ["git", "node"], root=str(sandbox),
                    remote=f"file://{upstream}", pull_first=True)
    text = (sandbox / "from-remote" / ".gitignore").read_text()
    assert "node_modules/" in text
