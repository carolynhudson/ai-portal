"""The GPU-stack update gate: polkit-scoped apt behind a desktop password.

The design (owner-approved, GUI App Center model): the portal lists GPU-stack
pending from the health probe, and applying runs `pkexec gpu-update-apply` —
the desktop polkit agent asks for the admin password, so the OS is the gate
and the portal never sees or stores it. The tests pin the layers:

* the route accepts ONLY names currently reported as gpu_pending;
* the gate binary's presence is required before a job starts;
* the job runs pkexec with the exact validated args, streams its output,
  and fails (not silently passes) when pkexec fails;
* the root-side helper refuses to run non-root, and its whitelist regex is
  kept textually identical to health.GPU_PKG_RE (the sync contract);
* the polkit policy is scoped to the exact helper path and demands admin.

Everything is scripted: a fake apt-get, a fake pkexec, a temp gate binary.
Nothing touches the system package manager.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-gpuupd-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

from fastapi.testclient import TestClient  # noqa: E402

from app.core import db, health, updater  # noqa: E402
from app.main import app  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
APTSIM = """Inst firefox [1.0] (2.0 Ubuntu:resolute [amd64])
Inst amdrocm-base10.0 [10.0.0~pre4] (10.0.0~pre5 AMD:resolute [amd64])
Inst mesa-amdgpu-vulkan-drivers [1.0] (2.0 AMD:resolute [amd64])
Inst linux-image-6.17.0-14-generic [1] (2 Ubuntu:resolute [amd64])
Inst libhsa-runtime64-1 [1] (2 AMD:resolute [amd64])
"""


def _client(tmp_path, monkeypatch, pkexec="ok"):
    """Fake apt-get + pkexec on PATH, gate binary in tmp, caches reset."""
    bin = tmp_path / "bin"
    bin.mkdir()
    # SHIM-RAN canary at the top: a silent fall-through to the REAL apt-get is
    # how this module first failed (34 host packages leaked into a fixture
    # assertion). Shims must carry a shebang: an extensionless no-shebang file
    # hits Python's ENOEXEC exec fallback, and the probe answers with real apt.
    (bin / "apt-get").write_text("#!/bin/sh\necho SHIM-RAN\n" + f"cat <<'EOF'\n{APTSIM}EOF\n")
    (bin / "apt-get").chmod(0o755)
    if pkexec == "ok":
        (bin / "pkexec").write_text('#!/bin/sh\necho "fake pkexec: $*"; exit 0\n')
        (bin / "pkexec").chmod(0o755)
    elif pkexec == "fail":
        (bin / "pkexec").write_text('#!/bin/sh\necho "fake pkexec: denied" >&2; exit 126\n')
        (bin / "pkexec").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin}{os.pathsep}{os.environ['PATH']}")
    gate = tmp_path / "gpu-update-apply"
    if pkexec != "no-gate":
        gate.write_text("#!/bin/sh\ntrue\n")
        gate.chmod(0o755)
    monkeypatch.setenv("AI_PORTAL_GPU_APPLY_BIN", str(gate))
    monkeypatch.setenv("AI_PORTAL_APT_STAMP", str(tmp_path / "stamp"))
    (tmp_path / "stamp").write_text("")
    health.reset_cache()
    db.init()
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    return c


def _wait_job(c, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("job never finished")


def test_gpu_gate_get_shape(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    db.execute("DELETE FROM jobs")
    r = c.get("/api/updates/gpu")
    assert r.status_code == 200
    d = r.json()
    # GPU-stack separated from ordinary/kernel, and libhsa-runtime* counts
    # (the whitelist is search-style, mirroring health.GPU_PKG_RE)
    assert d["gpu_pending"] == ["amdrocm-base10.0",
                                "mesa-amdgpu-vulkan-drivers",
                                "libhsa-runtime64-1"]
    assert d["kernel_pending"] == ["linux-image-6.17.0-14-generic"]
    assert d["gate_installed"] is True
    assert d["job"] is None


def test_gpu_post_validates_against_pending(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    db.execute("DELETE FROM jobs")
    r = c.post("/api/updates/gpu", json={"packages": ["firefox"]})
    assert r.status_code == 400, "ordinary package must be refused"
    r = c.post("/api/updates/gpu", json={"packages": []})
    assert r.status_code == 400
    r = c.post("/api/updates/gpu", json={"packages": ["linux-image-6.17.0-14-generic"]})
    assert r.status_code == 400, "kernel is not auto-appliable through this gate"


def test_gpu_post_requires_installed_gate(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, pkexec="no-gate")
    db.execute("DELETE FROM jobs")
    r = c.post("/api/updates/gpu", json={"packages": ["amdrocm-base10.0"]})
    assert r.status_code == 409
    assert "gpu_update_gate_install" in r.json()["detail"]


def test_gpu_job_runs_pkexec_with_validated_args(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    db.execute("DELETE FROM jobs")
    r = c.post("/api/updates/gpu",
               json={"packages": ["amdrocm-base10.0", "libhsa-runtime64-1"]})
    assert r.status_code == 200
    j = _wait_job(c, r.json()["job"])
    assert j["status"] == "success"
    assert j["kind"] == "gpuupdate"
    # the fake pkexec echoes its argv: proves exact pass-through, no options
    assert "fake pkexec:" in j["log"]
    assert "amdrocm-base10.0 libhsa-runtime64-1" in j["log"]
    # and GET shows the finished job (the pane reads state back from here)
    d = c.get("/api/updates/gpu").json()
    assert d["job"]["status"] == "success"


def test_gpu_job_fails_loudly_when_pkexec_fails(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, pkexec="fail")
    db.execute("DELETE FROM jobs")
    r = c.post("/api/updates/gpu", json={"packages": ["amdrocm-base10.0"]})
    assert r.status_code == 200
    j = _wait_job(c, r.json()["job"])
    assert j["status"] == "failed"
    assert "denied" in j["log"], "pkexec stderr must land in the job log"


def test_gate_helper_refuses_non_root():
    """The root-side enforcer must not run for an ordinary user."""
    r = subprocess.run(["sh", str(REPO / "deploy/gpu_update/gpu-update-apply"),
                        "amdrocm-base10.0"],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "pkexec" in r.stderr


def test_gate_whitelist_mirrors_health_regex():
    """Sync contract: the helper installs exactly what the portal SHOWS as
    GPU-stack, byte-for-byte, so the two lists can never drift."""
    helper = (REPO / "deploy/gpu_update/gpu-update-apply").read_text()
    line = [l for l in helper.splitlines() if l.startswith("WHITELIST=")]
    assert len(line) == 1
    pat = line[0].split("=", 1)[1].strip().strip("'")
    assert pat == health.GPU_PKG_RE.pattern


def test_polkit_policy_scoped_to_helper():
    """The policy must pin the exact exec path and demand admin (no stored
    secrets, no allow for inactive sessions)."""
    root = ElementTree.parse(
        REPO / "deploy/gpu_update/com.aiportal.gpuupdate.policy").getroot()
    action = root.find("./action")
    assert action is not None and action.get("id") == "com.aiportal.gpuupdate.apply"
    d = action.find("./defaults")
    got = {e.tag: e.text for e in d}
    assert got == {"allow_any": "no", "allow_inactive": "no",
                   "allow_active": "auth_admin_keep"}
    ann = {e.get("key"): e.text
           for e in action.findall("./annotate")}
    assert ann["org.freedesktop.policykit.exec.path"] == \
        "/usr/local/lib/aiportal/gpu-update-apply"


def test_gpuupdate_is_disruptive():
    """apt install takes the dpkg lock and restarts services: it owns the
    machine, so the kind must be in the disruptive set."""
    assert "gpuupdate" in updater.DISRUPTIVE
