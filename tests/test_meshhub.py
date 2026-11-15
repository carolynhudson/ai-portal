"""Portal meshhub client tests.

Unit tests stub meshctl (argv-recording shell that prints canned
JSON); the live integration test skips when no hub/identity is
reachable (CI stays hermetic).
"""
import json
import os
import stat
import sys

import pytest

from app.core import meshhub

STUB = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log = os.environ["STUB_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\\n")
cmd = args[0]
if cmd == "nodesjson":
    print(json.dumps({"nodes": [{"name": "halo", "fp": "a" * 64, "agepub": "age1x"}]}))
elif cmd == "lsjson":
    print(json.dumps({"secrets": [{"name": "forge-key", "kind": "secret", "size": 12,
                                   "recipients": ["halo"], "updated": 1, "owner": "a" * 64,
                                   "id": "i", "sha": "s", "created": 0}]}))
elif cmd == "get":
    sys.stdout.buffer.write(b"top\\nsuper-secret\\n")
elif cmd in ("put", "rm", "share"):
    print("stored ok")
elif args[:2] == ["fail", "x"]:
    sys.stderr.write("stub failure\\n")
    sys.exit(1)
"""


@pytest.fixture()
def stub(tmp_path, monkeypatch):
    exe = tmp_path / "meshctl"
    exe.write_text(STUB)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    log.touch()
    monkeypatch.setenv("AIPP_MESHCTL", str(exe))
    monkeypatch.setenv("STUB_LOG", str(log))
    return log


def _calls(log):
    return [json.loads(l) for l in log.read_text().splitlines()]


def test_nodes_parses(stub):
    ns = meshhub.nodes()
    assert ns[0]["name"] == "halo"


def test_listing_fields(stub):
    rows = meshhub.listing()
    assert rows == [{"name": "forge-key", "kind": "secret", "size": 12,
                     "recipients": ["halo"], "updated": 1}]


def test_get_text(stub):
    assert meshhub.get_text("forge-key") == "top\nsuper-secret\n"


def test_put_temp_file_args_and_cleanup(stub):
    meshhub.put("forge-key", b"abc", recipients=["halo", "forge"])
    (args,) = _calls(stub)
    assert args[0] == "put" and args[1] == "forge-key"
    assert args[2] == "--from" and args[3].startswith("/tmp/meshput-")
    assert args[4] == "--to" and args[5] == "halo,forge"
    assert not os.path.exists(args[3])  # material purged after call


def test_put_blob_flag(stub):
    meshhub.put("big", b"x", recipients=None, kind="blob")
    (args,) = _calls(stub)
    assert "--blob" in args
    assert "--to" not in args


def test_errors_carry_redacted_message(stub):
    with pytest.raises(meshhub.MeshHubError, match="stub failure"):
        meshhub._run(["fail", "x"])


def test_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("AIPP_MESHCTL", str(tmp_path / "nope"))
    with pytest.raises(meshhub.MeshHubError):
        meshhub.nodes()


def test_live_hub_integration():
    if not meshhub.available():
        pytest.skip("no mesh hub reachable (LAN) / no local identity")
    names = [n["name"] for n in meshhub.nodes()]
    assert "halo" in names
    meshhub.put("portal-test-key", b"portal-e2e", recipients=["halo"])
    assert meshhub.get_text("portal-test-key") == "portal-e2e"
    assert any(r["name"] == "portal-test-key" for r in meshhub.listing())
    meshhub.rm("portal-test-key")
    assert not any(r["name"] == "portal-test-key" for r in meshhub.listing())
