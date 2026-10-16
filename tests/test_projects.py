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

from app.core import db, harnessctl, personality, portalsettings, projects


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
    assert all(census[t].get("hint") for t in projects.INSTALL_HINT), \
        "an absent tool must come with the install statement"


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
    assert [r["name"] for r in projects.list_projects()] == ["half-built"], \
        "a half project is a fact the page can show"


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


def test_a_failed_init_does_not_leave_the_derived_ignores_behind(sandbox, monkeypatch):
    """The derivation is a consequence of the boxes that ran. A folder whose init failed is
    reported as failed, and the file the operator never ticked is not what a half-built folder
    should be left with."""
    real = projects._run

    def fake(argv, cwd, env):
        if argv[0] == "npm":
            return {"code": 127, "stdout": "", "stderr": "npm: command not found", "seconds": 0.0}
        return real(argv, cwd, env)

    monkeypatch.setattr(projects, "_run", fake)
    out = projects.create("broken", ["git", "node", "readme"], root=str(sandbox))
    assert out["ok"] is False
    assert not (sandbox / "broken" / ".gitignore").exists(), \
        "a failed init left a derived .gitignore in a folder the report calls failed"


# --- the registry knows a project is served by a personality (§6.14.1n, the queue 33 connection)
#
# The queue 33 brief states the connection in both directions: the harness launches a project with a
# personality, and the personality editor names the projects that use it. The second half is a fact
# the portal can already see and does not record — the harness host is fixed and a project's cwd is
# its path, so "project X is being served now" is observable. Record it, and the connection becomes
# two reads instead of a guess.

def test_a_project_being_served_is_a_fact_the_registry_records(sandbox, monkeypatch):
    """The page asks "is this project running?" and answers from the host. The registry has to be
    able to carry the answer, because a fact the portal can measure and cannot store is a fact the
    projects page will answer from the browser's memory."""
    target = sandbox / "served"
    projects.create("served", ["git", "readme"], root=str(sandbox))

    def fake_live(directories=None):
        return {"ok": True, "byDirectory": {str(sandbox / "served"): [{"id": "ses_1"}]}, "count": 1}

    monkeypatch.setattr(projects, "_live_sessions", fake_live)
    snap = projects.serve_facts()
    row = [r for r in snap["projects"] if r["name"] == "served"][0]
    assert row["serve"]["served"] is False, "nothing is live, so nothing is recorded as served"
    assert row["serve"]["reason"]
    assert snap["served"]["count"] == 0

    projects.mark_served(str(target), "ses_1")
    snap = projects.serve_facts()
    row = [r for r in snap["projects"] if r["name"] == "served"][0]
    assert row["serve"]["served"] is True and row["serve"]["session"] == "ses_1"
    assert snap["served"]["count"] == 1
    assert "served" in snap["served"]["names"]


def test_serving_facts_name_the_host_that_answered(sandbox, monkeypatch):
    """"No project is served" and "the host did not answer" are different answers, and only one of
    them is a fact about the projects."""
    monkeypatch.setattr(projects, "_live_sessions",
                        lambda directories=None: {"ok": False, "error": "connection refused"})
    snap = projects.serve_facts()
    assert snap["served"]["hostOk"] is False
    assert snap["served"]["unknown"] is True
    assert all(r["serve"]["served"] is not True for r in snap["projects"])


def test_serving_a_project_is_recorded_by_name_and_path(sandbox):
    """Adoption and launch both know a project by a path; the page launches by name. A verb that
    took only one of them would be a verb the two callers would each use a different way."""
    target = sandbox / "named"
    projects.create("named", ["git"], root=str(sandbox))
    out = projects.mark_served("named", "ses_9")
    assert out["path"] == str(target) and out["session"] == "ses_9"
    assert projects.mark_served(str(target), "ses_9")["name"] == "named"
    with pytest.raises(projects.ProjectError):
        projects.mark_served("not-a-project", "ses_9")


def test_launching_records_the_project_the_session_serves(sandbox, monkeypatch):
    """The launch call site is the only place that knows both halves — the session id the host
    returned and the personality the operator picked. `mark_served` exists for that call site, and
    a launch that does not record is the fact this slice exists to record.

    The harness launch is queue 32's endpoint (the page's Launch button is labelled inert, §6.14.1n);
    the requirement is recorded here so the slice that builds it cannot lose the write."""
    target = sandbox / "launched"
    projects.create("launched", ["git"], root=str(sandbox))
    monkeypatch.setattr(projects, "_live_sessions",
                        lambda directories=None: {"ok": True, "byDirectory": {str(target): [{"id": "ses_new"}]}, "count": 1})
    projects.mark_served(str(target), "ses_new", personality="work")
    row = [r for r in projects.serve_facts()["projects"] if r["name"] == "launched"][0]
    assert row["serve"] == {"served": True, "session": "ses_new", "personality": "work", "sessions": 1}
    assert projects.serve_facts()["served"] == {"count": 1, "names": ["launched"],
                                                "hostOk": True, "unknown": False}


# --- the personality's projects, read from the serving record (queue 33's second half) --------
#
# The queue 33 brief states the personality↔project link in both directions. The launch half is
# tested above. The other half is the personality editor's question — *given a personality, which
# projects run with it* — and it is answerable from the record the launch already writes. A record
# readable only as "is this project served" answers one page and not the other.

def test_the_personalitys_projects_are_read_from_the_records_the_launches_wrote(sandbox, monkeypatch):
    """The personality editor cannot ask the projects page. It needs the inverse view, built from
    the records, with a host that answered: a personality with no project listed because the host
    was unreachable is a page that states a fact about the host as a fact about the personality."""
    for name, pers in (("alpha", "Agent-Swift"), ("beta", "Agent-Swift"), ("gamma", "Other")):
        projects.create(name, ["git"], root=str(sandbox))
    monkeypatch.setattr(projects, "_live_sessions",
                        lambda directories=None: {"ok": True, "byDirectory": {
                            str(sandbox / name): [{"id": f"ses_{i}"}] for i, name in
                            enumerate(("alpha", "beta", "gamma"))}, "count": 3})
    projects.mark_served("alpha", "ses_0", "Agent-Swift")
    projects.mark_served("beta", "ses_1", "Agent-Swift")
    projects.mark_served("gamma", "ses_2", "Other")

    snap = projects.serve_facts()
    by = projects.by_personality(snap)
    assert by["hostOk"] is True, by
    assert sorted(by["projects"]["Agent-Swift"]) == ["alpha", "beta"], by
    assert by["projects"]["Other"] == ["gamma"], by
    assert "" not in by["projects"], "an unserved project has no personality and is not a row"
    assert "reason" not in by, "a host that answered needs no reason attached to its answer"


def test_the_inverse_view_says_when_the_host_could_not_confirm_any_of_it(sandbox, monkeypatch):
    """A personality that *was* launched with a project, on a host that is down, is listed with the
    reason the host could not confirm it. A page that showed an empty list there would be claiming
    it checked."""
    projects.create("quiet", ["git"], root=str(sandbox))
    projects.mark_served("quiet", "ses_7", "Agent-Swift")
    monkeypatch.setattr(projects, "_live_sessions",
                        lambda directories=None: {"ok": False, "error": "connection refused"})
    snap = projects.serve_facts()
    by = projects.by_personality(snap)
    assert by["hostOk"] is False
    # The registry knows the launch; the host could not confirm it. That is the case in question.
    assert by["projects"]["Agent-Swift"] == ["quiet"], by
    assert by["reason"], by


# --------------------------------- the payload's facts, from the endpoint the page reads (queue 33)
#
# The page assembles its personality list, its disabled Launch, and its row wording out of the
# payload. Those are page concerns, and tests/test_ui_contract.py checks them. What this file checks
# is the payload's side of the same line: the portal answers the personality names it guarantees,
# answers a list of names, and answers the launch verb the row posts to. A payload that dropped any
# of the three leaves the page assembling the fact from its own memory.

def test_the_projects_summary_offers_the_personalities_the_portal_guarantees():
    out = projects.summary()
    assert "personalities" in out, "the projects summary has no personality list"
    assert isinstance(out["personalities"], list), out["personalities"]
    assert all(isinstance(n, str) and n for n in out["personalities"]), out["personalities"]
    # The names the portal guarantees. `Agent-Swift`/`Agent-Deep` are test fixtures invented for the
    # serving tests; the personalities component's own guarantees are these, and the list that the
    # page offers has to contain them — a launch with no personality to pick is no launch.
    assert set(personality.RESERVED_NAMES) <= set(out["personalities"]), out["personalities"]


def test_the_personalities_offered_are_the_names_the_personalities_component_knows():
    """The list is the personalities component's names, not a copy the projects component keeps.

    The personality the launch records has to be a name the registry can resolve: a list assembled
    from a template file, a directory, or a literal in this file drifts from the names the
    personalities page stores, and the row ends up recording a personality that no longer exists.
    """
    from app.core import personality

    out = projects.summary()
    assert set(out["personalities"]) == {p["name"] for p in personality.all_personalities()}, \
        "the projects page offers names the personalities component does not know"


def test_the_launch_verb_is_stated_with_the_host_state_it_depends_on():
    """The Launch button's state is the host's state, stated by the portal.

    `available` is a bool and the endpoint is named, because the page has to post to the verb that
    records the pair: a page that invented an endpoint would keep the row inert while the real verb
    existed, and a page that invented `available` would launch against a host that is not answering.
    """
    out = projects.launch_facts()
    assert {"endpoint", "available"} <= set(out), out
    assert isinstance(out["available"], bool), out
    assert out["endpoint"].startswith("/api/projects/"), out


def test_a_shadowed_host_is_not_a_host_a_launch_can_be_recorded_on(monkeypatch):
    """Queue 30's measurement, applied to queue 33's record: when the unit is not the listener, every
    session on the port belongs to a process the portal does not supervise.

    The projects page must therefore *disable* Launch on a shadowed host, not merely warn: a pair
    recorded there is a fact about a session the portal cannot list, and the row would show the
    project as served. The refusal's words are the harness page's own (`status()["note"]`), so the
    two pages give the operator one explanation rather than two.
    """
    from app.core import harnessctl

    shadowed = {
        "unit": {"ActiveState": "active", "MainPID": "111"},
        "listening": True,
        "listener_pid": "999",
        "shadowed": True,
        "health": {"ok": True, "auth": "yes"},
        "note": "port 4096 is held by pid 999, not by the harness unit (MainPID 111); a request to "
                "this port is answered by that process",
    }
    monkeypatch.setattr(harnessctl, "status", lambda *a, **k: dict(shadowed))
    out = projects.launch_facts()
    assert out["available"] is False, out
    assert out["shadowed"] is True, out
    assert out["reason"] == shadowed["note"], out


def test_the_summary_says_when_a_launch_has_no_personality_to_be_recorded_with(tmp_path, monkeypatch,
                                                                               sandbox=None):
    """A store with no personalities is a real state on a fresh box, and the launch resolves `default`
    from the registry defaults rather than from a stored personality (§6.14.1n).

    The page's row offers `default`, so the launch is possible — what is missing is the *statement*
    that what gets recorded names no stored personality. Without it the projects page shows a project
    "served with default" on a box where the personalities page has no personality named `default`,
    and the two pages disagree about a fact the operator can see on both.
    """
    from app.core import personality
    sandbox = tmp_path / "projects"
    monkeypatch.setattr(projects.portalsettings, "projects_root", lambda *a, **k: str(sandbox))
    monkeypatch.setattr(projects.portalsettings, "validate_projects_root",
                        lambda value, **k: {"ok": True, "path": str(sandbox), "exists": True})
    monkeypatch.setattr(personality, "all_personalities", list)

    out = projects.summary()
    assert out["personalities"] == [], out
    assert out["personalities_named"] is False, "the payload hides that there is no personality"


# ------- how the host is asked about serving (§6.14.1v) — the measured reason the unscoped read fails
#
# Measured on the live 1.19.0 host: `GET /session` with no directory answers the folder the host was
# started in. The projects page launches into ~/ai-projects, which is not that folder, so the
# unscoped read returns a list that does not contain the session the portal created a minute
# earlier — and the page calls a live session "not on the host". The read has to name the folders.

def test_the_serving_read_asks_the_host_about_each_registered_folder(sandbox, monkeypatch):
    target = sandbox / "live"
    projects.create("live", ["git"], root=str(sandbox))
    projects.mark_served(str(target), "ses_in_project_folder")
    asked: list = []

    def fake_list(cwd=None, port=4096):
        asked.append(cwd)
        return [{"id": "ses_in_project_folder", "directory": str(target)}] if cwd == str(target) else []

    monkeypatch.setattr(harnessctl, "list_sessions", fake_list)
    snap = projects.serve_facts()
    row = [r for r in snap["projects"] if r["name"] == "live"][0]
    assert str(target) in asked, f"the host was asked about {asked!r}, not about {target}"
    assert row["serve"]["served"] is True, \
        f"a session the host reports for the project's own folder read as {row['serve']!r}"
    assert None not in asked, \
        "the host was asked its unscoped list, which is the answer that does not contain project sessions"


def test_a_project_the_host_refuses_to_list_is_not_reported_as_running(sandbox, monkeypatch):
    """A folder the host will not list is a folder whose sessions are unknown. Read as an empty
    list it is 'not served', and the page would be naming a running session finished."""
    target = sandbox / "refused"
    projects.create("refused", ["git"], root=str(sandbox))
    projects.mark_served(str(target), "ses_x")

    def fake_list(cwd=None, port=4096):
        if cwd == str(target):
            raise harnessctl.HarnessError("the host refused to list this project")
        return []

    monkeypatch.setattr(harnessctl, "list_sessions", fake_list)
    row = [r for r in projects.serve_facts()["projects"] if r["name"] == "refused"][0]
    assert row["serve"]["served"] is not True, row["serve"]
    assert row["serve"].get("unknown") is True, \
        f"a folder the host refused to list has to read as unknown, not as finished: {row['serve']!r}"


# ---------------------------- the session view and quitting a project (§6.14.1v)

def test_the_session_view_reads_the_record_the_launch_wrote(sandbox, monkeypatch):
    target = sandbox / "viewed"
    projects.create("viewed", ["git"], root=str(sandbox))
    projects.mark_served(str(target), "ses_view", personality="work")
    seen: dict = {}

    def fake_view(session_id, cwd, context_limit=None, port=4096):
        seen.update(session=session_id, directory=cwd, context_limit=context_limit)
        return {"ok": True, "found": True, "title": "Run", "busy": True, "turns": 3,
                "context_used": 9305, "context_limit": context_limit, "transcript": []}

    monkeypatch.setattr(harnessctl, "session_view", fake_view)
    out = projects.session_view("viewed", context_limit=200000)
    assert seen == {"session": "ses_view", "directory": str(target), "context_limit": 200000}, seen
    assert out["project"] == "viewed" and out["personality"] == "work", out
    assert out["session"] == "ses_view" and out["context_used"] == 9305, out
    assert out["served"] is True, out


def test_a_project_with_no_record_says_there_is_nothing_to_view_without_asking_the_host(sandbox,
                                                                                         monkeypatch):
    """The row's Launch button was just pressed and the launch failed, and the page asks to view: the
    answer is 'this project has no session', stated by the registry. Asking the host about a session
    id the portal does not have is a request that can only answer 404."""
    calls: list = []
    monkeypatch.setattr(harnessctl, "session_view", lambda *a, **k: calls.append(a))
    projects.create("bare", ["git"], root=str(sandbox))
    out = projects.session_view("bare")
    assert calls == [], f"the host was asked {calls!r} about a session the portal never recorded"
    assert out["served"] is False and out["session"] is None, out
    assert out["reason"], "the page needs the reason it has nothing to show"


def test_the_view_keeps_the_hosts_own_words_about_the_session_it_cannot_find(sandbox, monkeypatch):
    """The portal's record and the host's answer can disagree: the session ended, the host was
    restarted, the record is stale. The page has to print the host's answer, not the record's hope."""
    projects.create("gone", ["git"], root=str(sandbox))
    projects.mark_served(str(sandbox / "gone"), "ses_gone", personality="work")
    monkeypatch.setattr(harnessctl, "session_view",
                        lambda session_id, cwd, context_limit=None, port=4096: {
                            "ok": True, "found": False, "session": session_id, "directory": cwd,
                            "busy": None, "transcript": [],
                            "reason": "the host has no session ses_gone"})
    out = projects.session_view("gone")
    assert out["found"] is False, out
    assert out["served"] is False, \
        "the host says the session is not there — the registry's record cannot override that"
    assert "ses_gone" in out["reason"], out


def test_quitting_a_project_drops_the_record_and_leaves_the_session_for_the_harness_page(sandbox,
                                                                                           monkeypatch):
    # The host answers an empty list here, so what is under test is the record, not the host's silence.
    monkeypatch.setattr(projects, "_live_sessions",
                        lambda directories=None: {"ok": True, "byDirectory": {}, "count": 0})
    """Queue 32: 'quitting takes it back to the harness page, where the session can be resumed.'

    So quitting deletes only the portal's claim. The session and its transcript stay on the host,
    which is why the answer names the session it is leaving behind: the operator is about to look
    for it on the other page."""
    projects.create("quitme", ["git"], root=str(sandbox))
    projects.mark_served(str(sandbox / "quitme"), "ses_q", personality="work")
    out = projects.quit_project("quitme")
    assert out["ok"] is True and out["session"] == "ses_q", out
    assert "harness" in out["reason"], f"the answer has to say where the session went: {out!r}"
    row = [r for r in projects.serve_facts()["projects"] if r["name"] == "quitme"][0]
    assert row["serve"]["served"] is False and "no session" in row["serve"]["reason"], row["serve"]
    with pytest.raises(projects.ProjectError):
        projects.quit_project("no-such-project")


def test_quitting_a_project_that_never_launched_says_there_is_no_record_to_drop(sandbox):
    projects.create("never", ["git"], root=str(sandbox))
    out = projects.quit_project("never")
    assert out["ok"] is True and out["session"] is None, out
    assert out["reason"], "the page asked to quit a project with no session; it needs that said back"


# ------------------- the page's endpoints for the serving view (§6.14.1v)
#
# Queue 32's handoff: Launch is the point where the Projects page stops being a list of projects and
# becomes the session it is serving. The page can only do that if the view is readable and the send
# box has an endpoint to post to — and the answer to a send is the host's answer, which is a 400 for
# a session that is compacting, not a portal failure.

def test_the_serving_endpoint_answers_the_view_the_page_painting_needs(sandbox):
    c = _client()
    c.post("/api/projects", json={"name": "api-view", "steps": ["git"]})
    r = c.get("/api/projects/session", params={"name": "api-view"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["served"] is False and d["session"] is None, \
        f"a project that never launched has nothing to view; the endpoint said {d!r}"
    assert d["reason"], "the endpoint answered with no reason to show"


def test_the_serving_endpoint_names_the_project_it_cannot_answer(sandbox):
    c = _client()
    r = c.get("/api/projects/session", params={"name": "no-such-project"})
    assert r.status_code == 404, r.text
    assert "no-such-project" in r.json()["detail"], r.json()


def test_quitting_through_the_endpoint_leaves_the_session_the_harness_page_has_to_find(sandbox):
    c = _client()
    c.post("/api/projects", json={"name": "api-quit", "steps": ["git"]})
    projects.mark_served(str(sandbox / "api-quit"), "ses_api_quit", personality="work")
    r = c.post("/api/projects/quit", json={"name": "api-quit"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["session"] == "ses_api_quit", d
    assert "harness" in d["reason"], d
    row = [p for p in c.get("/api/projects").json()["projects"] if p["name"] == "api-quit"][0]
    # The host is not answering in this sandbox, so the payload's answer for every project is
    # "unknown" — asserted against that fact, not against a host that is not present here. What the
    # quit has to have changed is the portal's own record: no session id left on the row.
    assert not (row["serve"].get("session")), \
        f"the page quit the project and the payload still carries its session: {row['serve']!r}"


def test_the_send_box_posts_through_an_endpoint_that_answers_with_the_hosts_words(sandbox):
    """The send box's endpoint. `send_prompt` already answers instead of raising, so the endpoint
    must not turn that answer back into a 500: a session that is compacting has to reach the page as
    the host's sentence, with the status the page can print."""
    c = _client()
    c.post("/api/projects", json={"name": "api-send", "steps": ["git"]})
    projects.mark_served(str(sandbox / "api-send"), "ses_api_send", personality="work")

    from app import main as main_mod

    seen: dict = {}

    def fake_send(session_id, text, cwd):
        seen.update(session=session_id, text=text, cwd=cwd)
        return {"ok": False, "session": session_id, "accepted": None,
                "reason": "the host refused the text (HTTP 400: session is compacting)"}

    monkey = main_mod.harnessctl
    old = monkey.send_prompt
    monkey.send_prompt = fake_send
    try:
        r = c.post("/api/harness/session/prompt", json={"name": "api-send", "text": "continue"})
    finally:
        monkey.send_prompt = old
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False and "compacting" in d["reason"], d
    assert seen == {"session": "ses_api_send", "text": "continue",
                    "cwd": str(sandbox / "api-send")}, seen


def test_the_send_box_cannot_be_told_to_post_into_a_project_the_registry_does_not_know():
    c = _client()
    r = c.post("/api/harness/session/prompt", json={"name": "not-registered", "text": "x"})
    assert r.status_code == 404, r.text
    assert "not-registered" in r.json()["detail"], r.json()


def test_the_serving_and_quit_endpoints_require_auth():
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)
    assert c.get("/api/projects/session", params={"name": "x"},
                 follow_redirects=False).status_code == 401
    assert c.post("/api/projects/quit", json={"name": "x"},
                  follow_redirects=False).status_code == 401
    assert c.post("/api/harness/session/prompt", json={"name": "x", "text": "y"},
                  follow_redirects=False).status_code == 401


# ------------------- the meter's denominator (§6.14.1v)
#
# The design's context meter is `tokens used / model context limit`. The used half comes from the
# host. The limit half is the portal's fact — the ctx the personality's models are served at — and a
# meter with a made-up denominator is a gauge that lies, so it is read from the same rows the
# personalities page prices the deck with.

def test_the_meters_denominator_is_the_personalitys_own_context_ceiling(sandbox, monkeypatch):
    from app.core import personality as pers

    def fake_targets(p):
        return [{"model": "A.gguf", "config": None}, {"model": "B.gguf", "config": None}]

    def fake_rows():
        return [{"id": "A.gguf", "ctx_trained": 262144}, {"id": "B.gguf", "ctx_trained": 32768}]

    monkeypatch.setattr(pers, "find", lambda name: {"name": name, "models": ["A.gguf", "B.gguf"]})
    monkeypatch.setattr(pers, "resolve_targets", fake_targets)
    monkeypatch.setattr("app.core.modelindex.rows", fake_rows)
    assert projects.serving_context_limit("any") == 32768, \
        "the meter has to divide by the ceiling the session can actually be served at"


def test_the_meter_says_unknown_when_no_ceiling_is_readable(sandbox, monkeypatch):
    from app.core import personality as pers

    monkeypatch.setattr(pers, "find", lambda name: {"name": name, "models": ["X.gguf"]})
    monkeypatch.setattr(pers, "resolve_targets", lambda p: [{"model": "X.gguf", "config": None}])
    monkeypatch.setattr("app.core.modelindex.rows", lambda: [{"id": "X.gguf", "ctx_trained": 0}])
    assert projects.serving_context_limit("any") is None, \
        "a ceiling of 0 is not a ceiling; the meter has to say it does not know"


def test_the_meter_says_unknown_when_the_personality_cannot_be_resolved(sandbox, monkeypatch):
    from app.core import personality as pers

    def boom(p):
        raise pers.PersonalityError("personality 'gone' names models with no library entry")

    monkeypatch.setattr(pers, "find", lambda name: {"name": name, "models": ["gone"]})
    monkeypatch.setattr(pers, "resolve_targets", boom)
    assert projects.serving_context_limit("gone") is None, \
        "the meter's denominator is a read; an unresolvable personality is unknown, not a crash"


# ------------------- the record the send box and the meter read (§6.14.1v)

def test_the_record_answers_the_session_and_folder_a_send_needs_together(sandbox):
    (sandbox / "record-send").mkdir()
    projects.register(str(sandbox / "record-send"), "record-send")
    projects.mark_served(str(sandbox / "record-send"), "ses_record", personality="work")
    assert projects.served_session("record-send") == ("ses_record", str(sandbox / "record-send")), \
        "a send needs the session id and the folder in one read; read apart, they can disagree"


def test_the_record_refuses_to_answer_for_a_project_it_does_not_know(sandbox):
    try:
        projects.served_session("not-a-project")
    except projects.ProjectError as e:
        assert "not-a-project" in str(e), e
        return
    raise AssertionError("the send box accepted a project the registry has never heard of")


def test_the_record_says_nothing_is_serving_when_nothing_is_serving(sandbox):
    (sandbox / "record-none").mkdir()
    projects.register(str(sandbox / "record-none"), "record-none")
    try:
        projects.served_session("record-none")
    except projects.ProjectError as e:
        assert "record-none" in str(e) and ("session" in str(e).lower()), e
        return
    raise AssertionError("a project with no session answered with one")


def test_the_meter_reads_the_personality_the_launch_recorded(sandbox):
    (sandbox / "record-pers").mkdir()
    projects.register(str(sandbox / "record-pers"), "record-pers")
    projects.mark_served(str(sandbox / "record-pers"), "ses_pers", personality="Agentic-PI")
    assert projects.served_personality_of("record-pers") == "Agentic-PI"


def test_the_meter_answers_none_for_a_project_with_no_record(sandbox):
    (sandbox / "record-nopers").mkdir()
    projects.register(str(sandbox / "record-nopers"), "record-nopers")
    assert projects.served_personality_of("record-nopers") is None, \
        "no record means no personality to price a ceiling for, and 'none' is that answer"


# --------------------------------------------------- the operational state's endpoints (§6.14.1v)
#
# Queue 32's two states live on this page: the list, and *the session and nothing else*. The page
# reaches the host through the portal, so the endpoints it needs are the session read, the send
# box, the question form, and Quit. The host is not running in these tests, and that is the useful
# state: it fixes what each endpoint owes the page when the host does not answer — a shape with the
# reason, never a bare 500 the page can only print as `Internal Server Error`.

def test_the_session_endpoints_require_auth():
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)
    assert c.get("/api/projects/session?name=x", follow_redirects=False).status_code == 401
    for path, body in [("/api/projects/quit", {"name": "x"}),
                       ("/api/harness/session/prompt", {"name": "x", "text": "hi"}),
                       ("/api/harness/session/question/reply", {"name": "x", "answers": [["a"]]}),
                       ("/api/harness/session/question/reject", {"name": "x"})]:
        assert c.post(path, json=body, follow_redirects=False).status_code == 401, path


def test_the_session_read_answers_the_page(sandbox):
    target = sandbox / "viewapp"
    target.mkdir()
    projects.register(str(target))
    projects.mark_served(str(target), "ses_view")
    c = _client()
    r = c.get("/api/projects/session", params={"name": "viewapp"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["session"] == "ses_view"
    assert d["directory"] == str(target)
    # The host is not running here: the page must be able to say *why* the transcript is empty.
    assert d["ok"] is False and d["found"] is None and d["reason"]
    assert isinstance(d["transcript"], list) and isinstance(d["turns"], int)
    assert "context_used" in d and "context_limit" in d and "question" in d


def test_the_session_read_names_the_project_it_does_not_know(sandbox):
    c = _client()
    assert c.get("/api/projects/session", params={"name": "nope"}).status_code == 404
    assert c.get("/api/projects/session", params={"name": ""}).status_code == 400


def test_quitting_drops_the_claim_and_leaves_the_session_for_resume(sandbox):
    target = sandbox / "quitter"
    target.mkdir()
    projects.register(str(target))
    projects.mark_served(str(target), "ses_quit")
    c = _client()
    r = c.post("/api/projects/quit", json={"name": "quitter"})
    assert r.status_code == 200, r.text
    d = r.json()
    # Queue 32: quitting returns to the inactive state *with the session id visible for resume*.
    assert d["session"] == "ses_quit" and d["reason"] and "harness" in d["reason"]
    # The portal's claim is gone, so the page's operational state is the inactive one.
    assert projects.session_view("quitter")["served"] is False
    assert projects.session_view("quitter")["session"] is None
    assert c.post("/api/projects/quit", json={"name": "never-registered"}).status_code == 404


def test_the_send_box_reports_the_hosts_refusal_as_a_fact(sandbox):
    target = sandbox / "sender"
    target.mkdir()
    projects.register(str(target))
    projects.mark_served(str(target), "ses_send")
    c = _client()
    r = c.post("/api/harness/session/prompt", json={"name": "sender", "text": "  "})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["accepted"] is False and d["session"] == "ses_send" and d["reason"]
    r = c.post("/api/harness/session/prompt", json={"name": "sender", "text": "run the tests"})
    assert r.status_code == 200, r.text
    d = r.json()
    # No host in this test: the refusal is the host's own sentence, and the page prints it.
    # `accepted: None` is the host's refusal and `False` is the box's own emptiness — the page
    # needs the two apart, because "the host will not take text" and "you sent nothing" are
    # different sentences to read.
    assert d["accepted"] is None and d["ok"] is False and "refused" in d["reason"]
    assert c.post("/api/harness/session/prompt",
                  json={"name": "not-a-project", "text": "x"}).status_code == 404


def test_the_question_form_answers_the_project_it_is_serving(sandbox):
    c = _client()
    # Nothing is serving: the form has no session to answer for, and says so at 404.
    assert c.post("/api/harness/session/question/reply",
                  json={"name": "nope", "answers": {"branch": "a"}}).status_code == 404
    assert c.post("/api/harness/session/question/reject",
                  json={"name": "nope"}).status_code == 404
    target = sandbox / "asker"
    target.mkdir()
    projects.register(str(target))
    projects.mark_served(str(target), "ses_ask")
    # The host is down: the answer cannot be delivered, and 503 with the reason is the honest answer.
    r = c.post("/api/harness/session/question/reply",
               json={"name": "asker", "answers": {"branch": "a"}})
    assert r.status_code == 503, r.text
    assert "host" in r.json()["detail"].lower()
    assert c.post("/api/harness/session/question/reject",
                  json={"name": "asker"}).status_code == 503
