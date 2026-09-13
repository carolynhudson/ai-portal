"""Deployment tooling (app.deploy) + the logging setup (app.core.logsetup).

Deploys are exercised against a throwaway target directory; the real ~/ai and
the real systemd units are never touched (nothing here calls install_unit).
"""
import json
import os
import subprocess
import tempfile

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-dep-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

from app import deploy  # noqa: E402
from app.core import logsetup, paths, serviceconf, units  # noqa: E402


# -------------------------------------------------------------------- plan
def test_plan_reports_target_and_sha():
    p = deploy.plan(deploy.TARGET_DEFAULT)
    assert p["sha"] and len(p["sha"]) == 40
    assert p["target"] == str(deploy.TARGET_DEFAULT)
    assert p["unit"] == paths.PORTAL_UNIT
    assert p["env_file"].endswith("portal.env")
    assert p["wrapper"].endswith("ai-portal.sh")
    assert isinstance(p["first_deploy"], bool)
    assert isinstance(p["would_change"], bool)


def test_plan_resolves_refs_and_rejects_nonsense():
    head = deploy.resolve_ref("HEAD")
    assert head == deploy.resolve_ref("HEAD~0")
    try:
        deploy.resolve_ref("definitely-not-a-ref-12345")
    except RuntimeError as e:
        assert "definitely-not-a-ref" in str(e)
    else:
        raise AssertionError("bogus ref must raise RuntimeError")


def test_dirty_returns_paths_or_empty():
    d = deploy.dirty()
    assert isinstance(d, list)
    assert all(isinstance(line, str) for line in d)


# ------------------------------------------------------------------ export
def test_export_ref_lands_a_runnable_tree(tmp_path):
    sha = deploy.resolve_ref("HEAD")
    target = tmp_path / "ai-portal"
    deploy.export_ref(sha, target)
    assert (target / "app" / "main.py").is_file()
    assert (target / "web" / "templates" / "settings.html").is_file()
    assert (target / "pyproject.toml").is_file()
    # exported, not cloned: no .git, no venv, no scratch dirs left behind
    assert not (target / ".git").exists()
    assert not list(tmp_path.glob(".ai-portal.*"))


def test_export_ref_rejects_a_tree_without_the_app(tmp_path):
    repo = tmp_path / "other-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "readme.txt").write_text("nothing to deploy\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c",
                    "user.name=t", "commit", "-qm", "empty"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    target = tmp_path / "deploy-target"
    try:
        deploy.export_ref(sha, target, repo=repo)
    except RuntimeError as e:
        assert "not a deployable ai-portal tree" in str(e)
    else:
        raise AssertionError("exporting a tree without app/ must fail")
    # failure must not leave a half-written deployment or scratch dirs behind
    assert not target.exists()
    assert not list(tmp_path.glob(".deploy-target.*"))


# ------------------------------------------------------------ launch files
def test_portal_wrapper_is_valid_shell_and_carries_the_config():
    serviceconf.set_bind("portal", "all")
    env = {**serviceconf.ensure_defaults("portal"),
           "AI_PORTAL_DIR": "/srv/ai-portal",
           "AI_PORTAL_PYTHON": "/srv/ai-portal/.venv/bin/python"}
    text = deploy.render_portal_wrapper(env)
    assert "/srv/ai-portal/.venv/bin/python" in text
    assert "uvicorn app.main:app" in text
    assert "--host" in text and "--port" in text
    compiled = subprocess.run(["sh", "-n"], input=text, capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr


def test_router_and_comfy_wrappers_are_valid_shell():
    from app.providers.comfyui import COMFY_WRAPPER
    from app.providers.llamacpp import render_router_wrapper, router_env
    for text in (COMFY_WRAPPER, render_router_wrapper(router_env())):
        r = subprocess.run(["sh", "-n"], input=text, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    router = render_router_wrapper(router_env())
    assert "AI_PORTAL_PRINT_ARGV" in router, "needed to verify without loading a model"
    assert "--api-key-file" in router, "router auth must be launchable from the env"


def test_render_unit_matches_install_unit_shape():
    text = units.render_unit("ai-portal", "portal", "AI Portal", "/srv/ai-portal")
    assert f"EnvironmentFile={paths.AI_ENV_FILE}" in text
    assert f"EnvironmentFile={units.env_path('portal')}" in text
    assert f"ExecStart={units.wrapper_path('portal')}" in text
    assert "WorkingDirectory=/srv/ai-portal" in text
    assert "Restart=on-failure" in text


def test_print_units_covers_all_three_services(capsys):
    assert deploy.print_units() == 0
    out = capsys.readouterr().out
    for unit in (paths.COMFY_UNIT, paths.ROUTER_UNIT, paths.PORTAL_UNIT):
        assert f"===== {unit}.service =====" in out
    assert "GENERATED" in out


# ----------------------------------------------------------- deployment.json
def test_deployment_record_keeps_history_for_rollback():
    deploy.write_deployment({"sha": "a" * 40, "ref": "a", "target": "/t1",
                             "deployed_at": "t1"})
    deploy.write_deployment({"sha": "b" * 40, "ref": "b", "target": "/t1",
                             "deployed_at": "t2"})
    rec = deploy.read_deployment()
    assert rec["sha"] == "b" * 40
    hist = rec["history"]
    assert hist and hist[0]["sha"] == "a" * 40, "previous sha is the rollback target"
    assert json.loads(deploy.DEPLOYMENT_JSON.read_text())["sha"] == "b" * 40
    # history is bounded
    for i in range(15):
        deploy.write_deployment({"sha": f"{i}" * 40, "ref": "x", "target": "/t1"})
    assert len(deploy.read_deployment()["history"]) <= 10


def test_rollback_without_history_explains_itself(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "DEPLOYMENT_JSON", tmp_path / "none.json")
    try:
        deploy.rollback()
    except SystemExit as e:
        assert "deployment.json" in str(e) or "history" in str(e)
    else:
        raise AssertionError("rollback with no record must exit with a message")


# ------------------------------------------------------------------- logging
def test_resolve_level():
    assert logsetup.resolve_level("debug") == 10
    assert logsetup.resolve_level("WARNING") == 30
    assert logsetup.resolve_level("nonsense") == 20      # falls back to INFO
    assert logsetup.resolve_level("trace") == 1
    assert logsetup.resolve_level(40) == 40


def test_setup_creates_a_rotating_file(tmp_path, monkeypatch):
    monkeypatch.setattr(logsetup, "_configured", False)
    out = tmp_path / "logs" / "portal.log"
    log = logsetup.setup(level="debug", log_path=out, console=False)
    log.warning("hello-from-test")
    assert out.is_file() and "hello-from-test" in out.read_text()
    handlers = list(log.handlers)
    logsetup.setup(level="debug", log_path=out, console=False)
    assert len(log.handlers) == len(handlers), "setup() must be idempotent"


def test_setup_survives_an_unwritable_log_dir(tmp_path):
    # a block file where the log dir should be: logging must degrade, not raise
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    import logging
    monkey = logging.getLogger(logsetup.LOGGER_NAME)
    before = list(monkey.handlers)
    logsetup._configured = False
    log = logsetup.setup(level="info",
                         log_path=blocker / "logs" / "portal.log", console=False)
    log.info("still fine")
    assert isinstance(log, logging.Logger)
    monkey.handlers = before
    logsetup._configured = True
