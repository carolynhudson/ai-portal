"""The push gate: owner-approved pushes made mechanical.

Unit rules run against a scripted fake `git` (no network, no real repo);
the route tests assert the confirm gate; one render test proves the page
is honest about the real checkout (read-only).
"""
import json
import os
import stat
import tempfile

# throwaway home, before the app is imported (the suite's founding law)
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-push-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import pushgate  # noqa: E402
from app.main import app  # noqa: E402

# scripted git: responds to each verb from a JSON plan in FAKE_GIT;
# records argv to FAKE_LOG (one JSON list per call)
FAKE = """#!/usr/bin/env python3
import json, os, sys
plan = json.loads(os.environ["FAKE_GIT"])
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
with open(os.environ["FAKE_LOG"] + ".env", "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:],
                        "askpass": os.environ.get("GIT_ASKPASS"),
                        "sshcmd": os.environ.get("GIT_SSH_COMMAND"),
                        "term": os.environ.get("GIT_TERMINAL_PROMPT")}) + "\\n")
key = " ".join(sys.argv[1:])
for pat, rc, out, err in reversed(plan):
    if key.startswith(pat):
        sys.stdout.write(out); sys.stderr.write(err)
        sys.exit(rc)
sys.exit(0)
"""


class FakeGit:
    def __init__(self, plan_setter, log):
        self._set_plan = plan_setter
        self._log = log

    def plan(self, plan):
        self._set_plan(plan)

    def calls(self):
        return [json.loads(l) for l in self._log.read_text().splitlines()]

    def envs(self):
        p = self._log.parent / (self._log.name + ".env")
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    bin_ = tmp_path / "git"
    bin_.write_text(FAKE)
    bin_.chmod(bin_.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    log.touch()
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    # the hub is REAL for this suite (it holds github-pat now): unit tests
    # must not inherit its state -- lane tests set _pat explicitly.
    monkeypatch.setattr(pushgate, "_pat", lambda: None)
    return FakeGit(lambda plan: monkeypatch.setenv("FAKE_GIT", json.dumps(plan)), log)


OK_BASE = [
    ["rev-parse --abbrev-ref HEAD", 0, "main", ""],
    ["remote get-url origin", 0, "git@github.com:carolynhudson/ai-portal.git", ""],
    ["rev-list --count origin/main..HEAD", 0, "3", ""],
    ["rev-list --count HEAD..origin/main", 0, "0", ""],
    ["status --porcelain", 0, "", ""],
    ["log --format=%h %s origin/main..HEAD -n 50", 0, "abc1234 work one\ndef5678 work two", ""],
    ["ls-remote --symref origin HEAD", 0, "ref: refs/heads/main\tHEAD\nabc123\tHEAD", ""],
]


def test_status_fields(fake):
    g = fake
    g.plan(OK_BASE)
    st = pushgate.status()
    assert st["ok"] and st["branch"] == "main" and st["ahead"] == 3
    assert st["behind"] == 0 and st["remote_branch"] == "main"
    assert st["remote_live"] and len(st["commits"]) == 2


def test_status_degrades_honestly(fake):
    g = fake
    g.plan(OK_BASE + [["ls-remote", 128, "", "ssh: connect timeout"]])
    st = pushgate.status()
    assert st["ok"] and not st["remote_live"]
    assert "timeout" in st["remote_note"]


def test_refuses_non_main(fake):
    g = fake
    g.plan([["rev-parse --abbrev-ref HEAD", 0, "feature", ""]] + OK_BASE[1:])
    r = pushgate.push("msg")
    assert not r["ok"] and "feature" in r["why"]


def test_refuses_behind(fake):
    g = fake
    g.plan(OK_BASE + [["rev-list --count HEAD..origin/main", 0, "5", ""]])
    r = pushgate.push("msg")
    assert not r["ok"] and "behind" in r["why"]


def test_refuses_dead_remote(fake):
    g = fake
    g.plan(OK_BASE + [["ls-remote", 128, "", "Permission denied (publickey)."]])
    r = pushgate.push("msg")
    assert not r["ok"] and "remote" in r["why"].lower()


def test_refuses_nonmain_remote_default(fake):
    g = fake
    g.plan(OK_BASE + [["ls-remote --symref origin HEAD", 0,
                         "ref: refs/heads/master\tHEAD\nabc\tHEAD", ""]])
    r = pushgate.push("msg")
    assert not r["ok"] and "master" in r["why"]


def test_happy_path_stage_commit_push(fake):
    g = fake
    g.plan(OK_BASE + [
        ["fetch --quiet origin", 0, "", ""],
        ["add -A", 0, "", ""],
        ["commit -q -m", 0, "", ""],
        ["push origin HEAD:refs/heads/main", 0, "abc1234..def9999 main -> main", ""],
        ["rev-parse --short HEAD", 0, "def9999", ""],
    ])
    r = pushgate.push("my message")
    assert r["ok"] and r["head"] == "def9999"
    argv = g.calls()
    assert ["push", "origin", "HEAD:refs/heads/main"] in argv
    assert any(a[:2] == ["commit", "-q"] and "my message" in a for a in argv)


def test_nothing_to_commit_still_pushes(fake):
    g = fake
    g.plan(OK_BASE + [
        ["fetch --quiet origin", 0, "", ""],
        ["add -A", 0, "", ""],
        ["commit -q -m", 1, "nothing to commit, working tree clean", ""],
        ["push origin HEAD:refs/heads/main", 0, "ok", ""],
        ["rev-parse --short HEAD", 0, "abc1234", ""],
    ])
    r = pushgate.push("")
    assert r["ok"] and any("nothing to commit" in l for l in r["log"])


def test_redaction_of_token_shaped_secrets(fake):
    g = fake
    g.plan(OK_BASE + [
        ["fetch --quiet origin", 0, "", ""],
        ["add -A", 0, "", ""],
        ["commit -q -m", 0, "", ""],
        ["push origin HEAD:refs/heads/main", 1, "",
         "remote: refused ghp_SECRETSECRETSECRETSECRET1"],
    ])
    r = pushgate.push("msg")
    assert not r["ok"]
    assert "ghp_SECRET" not in json.dumps(r)


# ---------------------------------------------------------------- net env + hints

def test_net_env_batchmode_on_status_probe(fake, monkeypatch):
    g = fake
    g.plan(OK_BASE)
    monkeypatch.setattr(pushgate, "_pat", lambda: None)
    st = pushgate.status()
    assert st["ok"] and st["remote_live"]
    lr = [e for e in g.envs() if e["argv"][:1] == ["ls-remote"]][0]
    assert "BatchMode=yes" in (lr["sshcmd"] or "")
    assert lr["askpass"] is None  # ssh lane: no askpass scaffolding


def test_ssh_lane_push_rides_batchmode(fake, monkeypatch):
    g = fake
    g.plan(OK_BASE + [
        ["fetch --quiet origin", 0, "", ""],
        ["add -A", 0, "", ""],
        ["commit -q -m", 0, "", ""],
        ["push origin HEAD:refs/heads/main", 0, "abc1234..def9999 main -> main", ""],
        ["rev-parse --short HEAD", 0, "def9999", ""],
    ])
    monkeypatch.setattr(pushgate, "_pat", lambda: None)
    assert pushgate.push("msg")["ok"]
    p = [e for e in g.envs() if e["argv"][:1] == ["push"]][0]
    assert "BatchMode=yes" in (p["sshcmd"] or "")
    assert p["term"] == "0"


def test_hint_on_agent_refusal(fake, monkeypatch):
    g = fake
    g.plan([p for p in OK_BASE if not p[0].startswith("ls-remote")] + [
        ["ls-remote --symref origin HEAD", 128, "",
         "sign_and_send_pubkey: signing failed for ED25519 from agent: "
         "agent refused operation\ngit@github.com: Permission denied (publickey)."],
    ])
    monkeypatch.setattr(pushgate, "_pat", lambda: None)
    st = pushgate.status()
    assert not st["remote_live"] and st["lane"] == "ssh"
    assert "ssh-add" in st["hint"] and "id_ed25519" in st["hint"]
    assert "github-pat" in st["hint"]  # the terminal-free alternative


def test_hint_absent_when_remote_live(fake, monkeypatch):
    g = fake
    g.plan(OK_BASE)
    monkeypatch.setattr(pushgate, "_pat", lambda: None)
    assert pushgate.status()["hint"] == ""


# ---------------------------------------------------------------- PAT lane

def test_pat_shape_from_hub(monkeypatch):
    from app.core import meshhub
    monkeypatch.setattr(meshhub, "get_text",
                        lambda n: "ghp_" + "A" * 36 + "\n")
    assert pushgate._pat() == ("ghp_" + "A" * 36, "hub")


def test_pat_absent_is_none(monkeypatch):
    from app.core import meshhub
    monkeypatch.setattr(meshhub, "get_text", lambda n: "not-a-token")
    assert pushgate._pat() is None


def test_pat_lane_https_askpass_purge_and_no_token_in_argv(fake, monkeypatch):
    g = fake
    g.plan(OK_BASE + [
        ["fetch --quiet https://x-access-token@github.com/", 0, "", ""],
        ["add -A", 0, "", ""],
        ["commit -q -m", 0, "", ""],
        ["push https://x-access-token@github.com/carolynhudson/ai-portal.git HEAD:refs/heads/main",
         0, "abc1234..zzz main -> main", ""],
        ["rev-parse --short HEAD", 0, "zzz9999", ""],
    ])
    monkeypatch.setattr(pushgate, "_pat", lambda: ("ghp_" + "S" * 36, "hub"))
    r = pushgate.push("pat push")
    assert r["ok"] and r["head"] == "zzz9999"
    argv = g.calls()
    assert any(a[:2] == ["push", "https://x-access-token@github.com/carolynhudson/ai-portal.git"]
               for a in argv), "https lane not used"
    assert all("ghp_" not in json.dumps(a) for a in argv), "token rode argv"
    envs = g.envs()
    ap = [e for e in envs if e["argv"][:1] == ["push"]][0]
    assert ap["askpass"] and os.path.exists(ap["askpass"]) is False  # purged
    assert ap["term"] == "0"
    assert any("lane: pat/https" in l for l in r["log"])


def test_status_reports_pat_lane_when_ssh_dead(fake, monkeypatch):
    g = fake
    g.plan([p for p in OK_BASE if not p[0].startswith("ls-remote")] + [
        ["ls-remote --symref origin HEAD", 128, "", "agent refused operation"],
        ["ls-remote --symref https://x-access-token@github.com/carolynhudson/ai-portal.git HEAD",
         0, "ref: refs/heads/main\tHEAD\nx\tHEAD", ""],
    ])
    monkeypatch.setattr(pushgate, "_pat", lambda: ("ghp_" + "S" * 36, "hub"))
    st = pushgate.status()
    assert st["ok"] and st["remote_live"] and st["lane"] == "pat/https"


def test_status_ssh_lane_when_agent_ok(fake, monkeypatch):
    g = fake
    g.plan(OK_BASE)
    monkeypatch.setattr(pushgate, "_pat", lambda: None)
    st = pushgate.status()
    assert st["lane"] == "ssh"


def test_pat_age_first_sight(fake, tmp_path, monkeypatch):
    g = fake
    g.plan([p for p in OK_BASE if not p[0].startswith("ls-remote")] + [
        ["ls-remote --symref origin HEAD", 128, "", "agent refused operation"],
        ["ls-remote --symref https://x-access-token@github.com/carolynhudson/ai-portal.git HEAD",
         0, "ref: refs/heads/main\tHEAD\nx\tHEAD", ""],
    ])
    monkeypatch.setattr(pushgate, "_pat", lambda: ("ghp_" + "S" * 36, "hub"))
    seen = tmp_path / "github-pat-seen"
    monkeypatch.setattr(pushgate, "_PAT_SEEN", seen)
    st = pushgate.status()
    assert st["lane"] == "pat/https" and st["pat_age_d"] == 0
    assert seen.exists() and st["pat_note"] == ""


def test_pat_nudge_at_75_days(fake, tmp_path, monkeypatch):
    import os as _os
    import time as _t
    g = fake
    g.plan([p for p in OK_BASE if not p[0].startswith("ls-remote")] + [
        ["ls-remote --symref origin HEAD", 128, "", "agent refused operation"],
        ["ls-remote --symref https://x-access-token@github.com/carolynhudson/ai-portal.git HEAD",
         0, "ref: refs/heads/main\tHEAD\nx\tHEAD", ""],
    ])
    monkeypatch.setattr(pushgate, "_pat", lambda: ("ghp_" + "S" * 36, "hub"))
    seen = tmp_path / "github-pat-seen"
    seen.touch()
    old = _t.time() - 80 * 86400
    _os.utime(seen, (old, old))
    monkeypatch.setattr(pushgate, "_PAT_SEEN", seen)
    st = pushgate.status()
    assert st["pat_age_d"] == 80
    assert "80 d old" in st["pat_note"] and "mesh_migrate.py github-pat" in st["pat_note"]


# ---------------------------------------------------------------- route gate

def test_resolve_repo_env_override(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("AI_PORTAL_REPO", str(tmp_path))
    assert pushgate.resolve_repo() == tmp_path


def test_resolve_repo_code_tree_when_checkout(monkeypatch):
    # the dev repo is a checkout: the code tree wins without env
    monkeypatch.delenv("AI_PORTAL_REPO", raising=False)
    if (pushgate.REPO / ".git").exists():
        assert pushgate.resolve_repo() == pushgate.REPO


@pytest.fixture()
def client():
    c = TestClient(app)
    r = c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]},
               follow_redirects=False)
    assert r.status_code in (200, 302, 303), r.text
    return c


def test_page_renders_real_repo(client):
    r = client.get("/push")
    assert r.status_code == 200
    assert "Push gate" in r.text


def test_route_requires_confirm(client, monkeypatch):
    monkeypatch.setattr(pushgate, "push", lambda **kw: pytest.fail("push ran without confirm"))
    r = client.post("/api/push", json={"message": "x", "confirm": False})
    assert r.status_code == 400


def test_route_refusal_is_data(client, monkeypatch):
    monkeypatch.setattr(pushgate, "push", lambda **kw: {"ok": False, "why": "behind by 2", "log": []})
    r = client.post("/api/push", json={"message": "x", "confirm": True})
    assert r.status_code == 422
    assert "behind" in r.json()["detail"]["why"]


def test_route_success(client, monkeypatch):
    monkeypatch.setattr(pushgate, "push",
                        lambda **kw: {"ok": True, "head": "abc1234", "remaining_ahead": 0, "log": ["push: ok"]})
    r = client.post("/api/push", json={"message": "x", "confirm": True})
    assert r.status_code == 200 and r.json()["head"] == "abc1234"
