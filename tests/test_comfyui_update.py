"""The ComfyUI updater: what it runs, in which venv, and what it refuses to do.

The updater has to do what ComfyUI's own README says — update the checkout, then
``pip install -r requirements.txt`` — while knowing things the README cannot:

* the venv is the one **the service runs**, read from ``COMFY_PYTHON`` in its
  launch env, because installing into some other venv is how an update succeeds
  and changes nothing;
* ``requirements.txt`` lists ``torch``, ``torchvision``, ``torchaudio``
  **unpinned**, and this machine's builds of them are ROCm local versions that do
  not exist on PyPI. Without a constraint file an upgrade installs the CUDA
  wheels, and ComfyUI wakes up with no GPU;
* the venv is verified **before** the service is restarted into it, while the old
  process is still up and the damage is a log line rather than an outage.

Everything here is a scripted ``subprocess.run``: no git remote, no pip, no
systemd, no network.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# Isolation (same contract as tests/test_comfyui.py): never touch the real ~/ai.
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-comfyupd-"))

import pytest  # noqa: E402

from app.core import paths, systemd, updater  # noqa: E402
from app.providers import comfyui  # noqa: E402

C = comfyui.ComfyUI()

OLD = "7" * 40
NEW = "f" * 40
ROCM_TORCH = "2.12.0a0+rocm7.13.0a20260411"
VERIFY_OK = json.dumps({"torch": ROCM_TORCH, "hip": "7.13.60980", "cuda": None})


class FakeCmd:
    """Scripts git / pip / the torch check, and records every command line.

    Stateful where the updater is stateful: ``git rev-parse HEAD`` answers OLD
    until ``git pull`` runs, then NEW — so "the pull happened" cannot pass by
    accident. ``pip_effect`` is what a successful install does to the installed
    set, which is how before/after reporting is testable.
    """

    def __init__(self, installed=None, dirty="", pull_ok=True, pip_ok=True,
                 verify=VERIFY_OK, behind="3", pip_effect=None):
        self.installed = dict(installed if installed is not None else {
            "torch": ROCM_TORCH,
            "torchvision": "0.27.0a0+rocm7.13.0a20260411",
            "torchaudio": "2.11.0+rocm7.13.0a20260426",
            "triton": "3.7.0+git8108eb2c.rocm7.13.0a20260411",
            "comfyui-frontend-package": "1.52.4",
            "numpy": "2.4.3",
        })
        self.dirty, self.pull_ok, self.pip_ok = dirty, pull_ok, pip_ok
        self.verify, self.behind = verify, behind
        self.pip_effect = dict(pip_effect or {})
        self.pulled = False
        self.calls: list[list[str]] = []

    @staticmethod
    def result(stdout="", returncode=0, stderr=""):
        return type("R", (), {"returncode": returncode, "stdout": stdout,
                              "stderr": stderr})()

    def __call__(self, args, **kw):
        """Dispatch on command *structure*, never on a substring of the whole
        command line: the interpreter path contains the temp dir, and the temp
        dir contains the test's own name — `test_pip_installs…` once made every
        command look like a pip install."""
        cmd = [str(a) for a in args]
        self.calls.append(cmd)
        is_git = cmd and cmd[0] == "git"
        is_pip = len(cmd) > 2 and cmd[1] == "-m" and cmd[2] == "pip"
        code = cmd[-1] if len(cmd) > 2 else ""
        if is_git:
            if "status" in cmd and "--porcelain" in cmd:
                return self.result(self.dirty)
            if "rev-list" in cmd:
                return self.result(self.behind + "\n")
            if "rev-parse" in cmd:
                return self.result((NEW if self.pulled else OLD) + "\n")
            if "pull" in cmd:
                if not self.pull_ok:
                    return self.result("", returncode=1,
                                       stderr="not a fast-forward")
                self.pulled = True
                return self.result()
        elif is_pip:
            if "list" in cmd:
                return self.result(json.dumps(
                    [{"name": n, "version": v}
                     for n, v in self.installed.items()]))
            if "install" in cmd:
                if not self.pip_ok:
                    return self.result("", returncode=1,
                                       stderr="ERROR: cannot resolve dependencies")
                self.installed.update(self.pip_effect)
                return self.result()
        elif "torch.version.hip" in code:      # the post-update verification
            return self.result((self.verify + "\n") if self.verify else "",
                               returncode=0 if self.verify else 1)
        return self.result()

    def pip_installs(self) -> list[list[str]]:
        return [c for c in self.calls if "pip" in c and "install" in c]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake ComfyUI checkout, launch env and (running) service."""
    (paths.COMFY_REPO / ".git").mkdir(parents=True, exist_ok=True)
    (paths.COMFY_REPO / "requirements.txt").write_text(
        "comfyui-frontend-package==1.52.7\ntorch\ntorchvision\ntorchaudio\n"
        "numpy>=1.25.0\ntransformers>=4.50.3\n")
    (paths.COMFY_REPO / "manager_requirements.txt").write_text("GitPython\n")
    service_py = tmp_path / "service-venv" / "bin" / "python"
    service_py.parent.mkdir(parents=True, exist_ok=True)
    service_py.write_text("#!/bin/sh\nexit 0\n")
    paths.CONFIG.mkdir(parents=True, exist_ok=True)
    (paths.CONFIG / "comfyui.env").write_text(
        f'COMFY_PYTHON="{service_py}"\nCOMFY_HOST="127.0.0.1"\nCOMFY_PORT="8188"\n')
    monkeypatch.setattr(systemd, "unit_state",
                        lambda unit: {"ActiveState": "active"})
    restarts: list[tuple] = []
    monkeypatch.setattr(systemd, "service_action",
                        lambda key, action: restarts.append((key, action)) or {"ok": True})
    monkeypatch.setattr(C, "_wait_healthy", lambda log, wait=120: True)
    updater.db.init()
    return {"restarts": restarts, "python": str(service_py)}


@pytest.fixture
def script(monkeypatch, repo):
    """Install a scripted command runner; the returned object records it.

    A test that calls this must also take `repo` — the fixtures run in signature
    order, and a scripted command runner installed over a *real* git checkout
    would shell out for real.
    """
    def install(**kw) -> FakeCmd:
        f = FakeCmd(**kw)
        monkeypatch.setattr(comfyui.subprocess, "run", f)
        return f
    return install


def test_a_scripted_test_never_runs_a_real_command():
    """Meta-test for the rule above: every test that scripts commands must
    install them over the fake repo, or it would act on the host."""
    import inspect
    for name, fn in list(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        params = list(inspect.signature(fn).parameters)
        if "script" in params:
            assert "repo" in params, f"{name} scripts commands without the repo"


def run_update(**kw) -> dict:
    logs: list[str] = []
    res = C.update(log=lambda m: logs.append(str(m)), **kw)
    res["_log"] = "\n".join(logs)
    return res


# -------------------------------------------------------- the venv question

def test_pip_installs_use_the_service_python(script, repo):
    f = script()
    run_update()
    installs = f.pip_installs()
    assert installs, "requirements were never installed"
    assert all(c[0] == repo["python"] for c in installs), installs
    assert "--upgrade" not in installs[0]
    # and the job log says where that interpreter came from
    assert "COMFY_PYTHON from ~/ai/config/comfyui.env" in run_update()["_log"]


def test_the_venv_it_will_use_is_reported_in_update_status(script, repo):
    script()
    st = C.update_status()
    assert st["venv"]["python_bin"] == repo["python"]
    assert "COMFY_PYTHON" in st["venv"]["python_src"]


def test_a_service_python_that_no_longer_exists_falls_back_and_says_so(
        script, repo, monkeypatch, tmp_path):
    script()
    gone = tmp_path / "gone" / "bin" / "python"
    (paths.CONFIG / "comfyui.env").write_text(f'COMFY_PYTHON="{gone}"\n')
    path, source = comfyui.service_python()
    assert path.endswith("venv/bin/python")
    assert "does not exist" in source, "silently picking another venv is the bug"


# ------------------------------------- this machine's torch must survive

def test_this_machines_builds_are_held_by_a_constraints_file(script, repo):
    f = script()
    res = run_update()
    installs = f.pip_installs()
    assert installs
    assert all("-c" in c for c in installs), "no constraints file was passed"
    pinned = Path(installs[0][installs[0].index("-c") + 1]).read_text()
    assert f"torch=={ROCM_TORCH}" in pinned
    assert "torchvision==0.27.0a0+rocm7.13.0a20260411" in pinned
    assert "triton==3.7.0+git8108eb2c.rocm7.13.0a20260411" in pinned
    # the hold is announced: "held" has to be visible to be trusted
    assert "holding 4 machine-specific package(s)" in res["_log"]


def test_upgrade_mode_adds_pip_upgrade_and_keeps_the_holds(script, repo):
    f = script()
    run_update(upgrade=True)
    for c in f.pip_installs():
        assert "--upgrade" in c and "-c" in c


def test_default_mode_does_not_upgrade(script, repo):
    f = script()
    run_update()
    assert all("--upgrade" not in c for c in f.pip_installs())


def test_a_torch_that_got_replaced_anyway_fails_before_the_restart(script, repo):
    """The constraints hold; if pip moves it regardless, that is the job failing
    — not the service restarting onto a CUDA build."""
    script(pip_effect={"torch": "2.6.0"})
    with pytest.raises(RuntimeError) as e:
        run_update()
    assert "torch was replaced" in str(e.value)
    assert "Restore" in str(e.value)
    assert repo["restarts"] == []


# ------------------------------------------------------ verification gate

def test_the_venv_is_verified_before_the_service_is_restarted(script, repo):
    f = script()
    run_update()
    checks = [c for c in f.calls if "torch.version.hip" in " ".join(c)]
    assert checks, "the venv was never checked"
    assert repo["restarts"] == [("comfyui", "restart")]


def test_a_cuda_torch_means_no_restart_and_a_clear_error(script, repo):
    script(verify="")          # the import fails: no torch, or the wrong build
    with pytest.raises(RuntimeError) as e:
        run_update()
    assert "post-update check failed" in str(e.value)
    assert repo["restarts"] == []
    # the way back is spelled out, because the checkout has already moved
    assert f"git -C {paths.COMFY_REPO} reset --hard {OLD}" in str(e.value)


def test_restart_is_skipped_when_asked_or_when_the_service_is_down(script, monkeypatch, repo):
    script()
    assert run_update(restart=False)["restarted"] is False
    assert repo["restarts"] == []
    monkeypatch.setattr(systemd, "unit_state",
                        lambda unit: {"ActiveState": "inactive"})
    assert run_update(restart=True)["restarted"] is False
    assert repo["restarts"] == []


# ---------------------------------------------------------- git behaviour

def test_a_dirty_repo_is_refused_naming_its_files(monkeypatch, repo):
    """Regression: this path raised NameError (`diry`), which surfaced as an
    unexplained failed job — precisely "the update did nothing"."""
    f = FakeCmd(dirty="M custom_nodes/weird.py\n? output/forgotten.png\n")
    monkeypatch.setattr(comfyui.subprocess, "run", f)
    with pytest.raises(RuntimeError) as e:
        C.update(log=lambda m: None)
    assert "local changes" in str(e.value)
    assert "custom_nodes/weird.py" in str(e.value)
    assert not [c for c in f.calls if "pull" in " ".join(c)], "pulled on a dirty repo"


def test_noop_when_the_checkout_is_up_to_date(script, repo):
    script(behind="0")
    f = comfyui.subprocess.run
    res = run_update()
    assert res["status"] == "noop"
    assert f.pip_installs() == [], "nothing to do means no pip"
    assert repo["restarts"] == []


def test_a_failed_pull_is_reported_without_touching_the_venv(script, repo):
    script(pull_ok=False)
    f = comfyui.subprocess.run
    with pytest.raises(RuntimeError) as e:
        run_update()
    assert "git pull failed" in str(e.value)
    assert f.pip_installs() == []


def test_a_failed_pip_is_reported_with_its_output(script, repo):
    script(pip_ok=False)
    f = comfyui.subprocess.run
    with pytest.raises(RuntimeError) as e:
        run_update()
    assert any("requirements.txt" in " ".join(c) for c in f.pip_installs())
    assert "requirements sync failed" in str(e.value)
    assert "cannot resolve dependencies" in str(e.value)
    assert repo["restarts"] == []


# ------------------------------------------------------------- reporting

def test_the_job_log_says_which_packages_changed(script, repo):
    script(pip_effect={"comfyui-frontend-package": "1.52.7"})
    res = run_update()
    assert "comfyui-frontend-package 1.52.4 -> 1.52.7" in res["_log"]
    assert res["changed"]["comfyui-frontend-package"]["to"] == "1.52.7"


def test_a_sync_that_changes_nothing_says_so(script, repo):
    script()
    assert "no package versions changed" in run_update()["_log"]


# ------------------------------------------------------------------- result

def test_the_job_result_names_the_venv_and_the_verification(script, repo):
    script()
    """The two facts an operator needs afterwards, in the job's own result:
    which interpreter this touched, and what it proved before restarting."""
    res = run_update()
    assert res["python"] == repo["python"]
    assert "COMFY_PYTHON" in res["python_src"]
    assert res["verified"]["ok"] and res["verified"]["hip"] == "7.13.60980"
    assert res["restarted"] is True


def test_the_manager_requirements_are_installed_too(script, repo):
    f = script()
    run_update()
    joined = [" ".join(c) for c in f.pip_installs()]
    assert any("manager_requirements.txt" in j for j in joined)
    assert any(j.endswith("requirements.txt") for j in joined)
