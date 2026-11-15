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
key = " ".join(sys.argv[1:])
for pat, rc, out, err in reversed(plan):
    if key.startswith(pat):
        sys.stdout.write(out); sys.stderr.write(err)
        sys.exit(rc)
sys.exit(0)
"""


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    bin_ = tmp_path / "git"
    bin_.write_text(FAKE)
    bin_.chmod(bin_.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    log.touch()
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))

    def set_plan(plan):
        monkeypatch.setenv("FAKE_GIT", json.dumps(plan))

    def calls():
        return [json.loads(l) for l in log.read_text().splitlines()]
    return set_plan, calls


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
    set_plan, _ = fake
    set_plan(OK_BASE)
    st = pushgate.status()
    assert st["ok"] and st["branch"] == "main" and st["ahead"] == 3
    assert st["behind"] == 0 and st["remote_branch"] == "main"
    assert st["remote_live"] and len(st["commits"]) == 2


def test_status_degrades_honestly(fake):
    set_plan, _ = fake
    set_plan(OK_BASE + [["ls-remote", 128, "", "ssh: connect timeout"]])
    st = pushgate.status()
    assert st["ok"] and not st["remote_live"]
    assert "timeout" in st["remote_note"]


def test_refuses_non_main(fake):
    set_plan, _ = fake
    set_plan([["rev-parse --abbrev-ref HEAD", 0, "feature", ""]] + OK_BASE[1:])
    r = pushgate.push("msg")
    assert not r["ok"] and "feature" in r["why"]


def test_refuses_behind(fake):
    set_plan, _ = fake
    set_plan(OK_BASE + [["rev-list --count HEAD..origin/main", 0, "5", ""]])
    r = pushgate.push("msg")
    assert not r["ok"] and "behind" in r["why"]


def test_refuses_dead_remote(fake):
    set_plan, _ = fake
    set_plan(OK_BASE + [["ls-remote", 128, "", "Permission denied (publickey)."]])
    r = pushgate.push("msg")
    assert not r["ok"] and "remote" in r["why"].lower()


def test_refuses_nonmain_remote_default(fake):
    set_plan, _ = fake
    set_plan(OK_BASE + [["ls-remote --symref origin HEAD", 0,
                         "ref: refs/heads/master\tHEAD\nabc\tHEAD", ""]])
    r = pushgate.push("msg")
    assert not r["ok"] and "master" in r["why"]


def test_happy_path_stage_commit_push(fake):
    set_plan, calls = fake
    set_plan(OK_BASE + [
        ["fetch --quiet origin", 0, "", ""],
        ["add -A", 0, "", ""],
        ["commit -q -m", 0, "", ""],
        ["push origin HEAD:refs/heads/main", 0, "abc1234..def9999 main -> main", ""],
        ["rev-parse --short HEAD", 0, "def9999", ""],
    ])
    r = pushgate.push("my message")
    assert r["ok"] and r["head"] == "def9999"
    argv = calls()
    assert ["push", "origin", "HEAD:refs/heads/main"] in argv
    assert any(a[:2] == ["commit", "-q"] and "my message" in a for a in argv)


def test_nothing_to_commit_still_pushes(fake):
    set_plan, _ = fake
    set_plan(OK_BASE + [
        ["fetch --quiet origin", 0, "", ""],
        ["add -A", 0, "", ""],
        ["commit -q -m", 1, "nothing to commit, working tree clean", ""],
        ["push origin HEAD:refs/heads/main", 0, "ok", ""],
        ["rev-parse --short HEAD", 0, "abc1234", ""],
    ])
    r = pushgate.push("")
    assert r["ok"] and any("nothing to commit" in l for l in r["log"])


def test_redaction_of_token_shaped_secrets(fake):
    set_plan, _ = fake
    set_plan(OK_BASE + [
        ["fetch --quiet origin", 0, "", ""],
        ["add -A", 0, "", ""],
        ["commit -q -m", 0, "", ""],
        ["push origin HEAD:refs/heads/main", 1, "",
         "remote: refused ghp_SECRETSECRETSECRETSECRET1"],
    ])
    r = pushgate.push("msg")
    assert not r["ok"]
    assert "ghp_SECRET" not in json.dumps(r)


# ---------------------------------------------------------------- route gate

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
