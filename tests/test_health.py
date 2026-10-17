"""Health pane probes (app/core/health.py).

Everything here runs against a throwaway `AI_PORTAL_HOME`, a temp ROCm root, and
stand-in `apt-get` / `dpkg-query` / `hipcc` / `du` on PATH: no package manager,
no /opt/rocm, no network, nothing installed or restarted.

The tests are written against the shapes the page renders, because a health
pane that reports the wrong thing is worse than one that reports nothing.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path

# Before app.core.paths is imported: every fixture below writes into the tree
# `paths` resolves to, so it has to be a throwaway one.
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-health-"))

import pytest  # noqa: E402

from app.core import health, paths  # noqa: E402

assert str(paths.AI_ROOT).startswith(tempfile.gettempdir()), (
    f"these fixtures must not write into a real AI tree ({paths.AI_ROOT})")


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    health.reset_cache()
    yield
    health.reset_cache()


@pytest.fixture
def shims(tmp_path, monkeypatch):
    """Put stand-in commands on PATH; return a helper that writes them."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def write(name: str, body: str, calls_file: Path | None = None):
        script = bindir / name
        prelude = "#!/bin/sh\n"
        if calls_file:
            prelude += f'echo "{name} $*" >> "{calls_file}"\n'
        script.write_text(prelude + body)
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        return script
    return write


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Clear what the probes read, per test — but not the whole AI tree.

    ``state/`` holds the portal database and other test modules in this session
    have it open, so a wipe of AI_ROOT takes the session down with it. What gets
    cleared is exactly the surface the probes touch: no test inherits another's
    model directory, torch venv, Vulkan SDK or release registry.
    """
    import shutil
    for p in (paths.LLAMA_MODELS, paths.COMFY_VENV, paths.AI_BIN,
              paths.AI_ROOT / "sdk", paths.BUILD_ROOT, paths.RELEASES):
        shutil.rmtree(p, ignore_errors=True)
        p.mkdir(parents=True, exist_ok=True)
    # gone, and stays gone: an empty .git directory would read as a repo
    shutil.rmtree(paths.COMFY_REPO / ".git", ignore_errors=True)
    paths.PRESETS_INI.parent.mkdir(parents=True, exist_ok=True)
    paths.PRESETS_INI.unlink(missing_ok=True)
    paths.RELEASES_JSON.unlink(missing_ok=True)
    # two tests set LD_LIBRARY_PATH to simulate a moved ROCm; the shared service
    # env is read by every other module in this session, so put it back
    env_file = paths.AI_ENV_FILE
    env_before = env_file.read_text() if env_file.is_file() else None
    (paths.AI_ROOT / "sdk" / "vulkan" / "1.4.341.0").mkdir(parents=True)
    rocm = tmp_path / "rocm"
    (rocm / "core-9.9" / "lib").mkdir(parents=True)
    (rocm / "core").mkdir(parents=True)
    monkeypatch.setenv("AI_PORTAL_ROCM_ROOT", str(rocm))
    monkeypatch.setenv("AI_PORTAL_APT_STAMP", str(tmp_path / "apt-stamp"))
    # the NPU probe runs on every bundle; hermetic means "no NPU" by default —
    # a real /dev/accel/accel0 + real xrt-smi on the test box would make the
    # fixture report the box, not the fixture (the disk_usage comment above is
    # the same lesson, learned once already)
    monkeypatch.setenv("AI_PORTAL_ACCEL_NODE", str(tmp_path / "accel0"))
    monkeypatch.setenv("AI_PORTAL_XRT_SMI", str(tmp_path / "xrt-smi"))
    monkeypatch.setenv("AI_PORTAL_MODVER", str(tmp_path / "modver"))
    # the hip compile probe is hermetic too: a missing clang means the probe
    # is inert, so the fixture box never shells out to the real ROCm
    monkeypatch.setenv("AI_PORTAL_CLANG_HIP", str(tmp_path / "clang++"))
    # and the embed-endpoint probes must report the fixture, not the box:
    # a live ai-embed sidecar on 8390 is exactly the /dev/accel lesson again
    monkeypatch.setattr(health, "_embed_live", lambda *a, **k: False)
    # Pin the disk probe. `health.disk()` calls `shutil.disk_usage(paths.AI_ROOT)`, and AI_ROOT
    # lives under /tmp — a tmpfs whose free space depends on how many multi-gigabyte fake models
    # earlier test runs left behind, so "a quiet machine has nothing to report" was reading this
    # machine's tmpfs occupancy rather than a fixture. It passed while /tmp was roomy and failed
    # once enough test runs had filled it.
    #
    # A test that wants a scarce disk still overrides this locally (see
    # test_a_scarce_disk_is_a_warning); what the default has to guarantee is that the ordinary
    # "nothing to report" assertion is about the fixture, not about the box running pytest.
    monkeypatch.setattr(health.shutil, "disk_usage",
                        lambda p: type("u", (), {"total": 2_000 * 1024**3,
                                                 "free": 900 * 1024**3,
                                                 "used": 1100 * 1024**3})())
    yield paths.AI_ROOT
    if env_before is None:
        env_file.unlink(missing_ok=True)
    else:
        env_file.write_text(env_before)


def model(root: Path, name: str, *files: str):
    d = paths.LLAMA_MODELS / name
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_text("x")
    return d


# ---------------------------------------------------------------------------- apt

def test_apt_separates_the_gpu_stack_from_ordinary_updates(shims, tree, tmp_path):
    shims("apt-get", f"""cat <<'EOF'
Reading package lists...
Building dependency tree...
The following packages will be upgraded:
  firefox amdrocm-base10.0 linux-image-6.17.0-14-generic
Inst firefox [1:1snap1-0ubuntu8] (1:1snap1-0ubuntu9.1 Ubuntu:26.04/resolute-updates [amd64])
Inst amdrocm-base10.0 [10.0.0~pre4] (10.0.0~pre5 AMD:resolute [amd64])
Inst linux-image-6.17.0-14-generic [6.17.0-14] (6.17.0-15 Ubuntu:26.04/resolute-security [amd64])
Inst mesa-amdgpu-vulkan-drivers [25.0] (25.1 AMD:resolute [amd64])
EOF
""")
    (tmp_path / "apt-stamp").touch()
    a = health.apt()
    assert a["available"] is True and a["pending"] == 4
    assert a["gpu_pending"] == ["amdrocm-base10.0", "mesa-amdgpu-vulkan-drivers"]
    assert a["kernel_pending"] == ["linux-image-6.17.0-14-generic"]
    assert a["lists_age_h"] < 1


def test_apt_that_cannot_run_is_reported_not_raised(shims, tree):
    shims("apt-get", """echo "E: Could not get lock /var/lib/dpkg/lock-frontend" >&2; exit 100""")
    a = health.apt()
    assert a["pending"] == 0
    assert "lock" in (a["error"] or "")


def test_missing_apt_is_a_state_not_a_crash(shims, tree, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    a = health.apt()
    assert a["available"] is False and "not found" in a["error"]


# --------------------------------------------------------------------------- ROCm

def test_rocm_installed_hip_and_the_launch_dir_are_three_different_things(
        shims, tree, monkeypatch):
    shims("dpkg-query", """cat <<'EOF'
amdrocm-base10.0 10.0.0~pre4-32424980240
amdrocm-blas10.0 10.0.0~pre4-32424980240
rocm-core 6.4.0-1
firefox 1.2.3-4
EOF
""")
    shims("hipcc", """echo 'HIP version: 7.15.26333-0000000'""")
    from app.core import serviceenv
    serviceenv.set_var("LD_LIBRARY_PATH", "/opt/other/lib")     # not ROCm
    r = health.rocm()
    assert r["installed"] == "10.0.0"       # the majority amdrocm version, not rocm-core
    assert r["hip"] == "7.15.26333"
    assert r["cores"] == ["core-9.9", "core"]
    assert r["launch_dir"] is None          # LD_LIBRARY_PATH has no ROCm entry


def test_a_launch_path_left_behind_by_an_upgrade_is_called_out(shims, tree, monkeypatch):
    from app.core import serviceenv
    rocm = Path(os.environ["AI_PORTAL_ROCM_ROOT"])
    serviceenv.set_var("LD_LIBRARY_PATH", f"{rocm}/core-8.0/lib:/elsewhere")
    r = health.rocm()
    assert r["launch_dir_ok"] is False
    assert r["launch_dirs_missing"] == [f"{rocm}/core-8.0/lib"]
    text = " | ".join(a["text"] for a in health.advisories())
    assert "no longer exist" in text
    assert any(a["level"] == "bad" for a in health.advisories())


def test_rocm_falls_back_to_core_directory_names(shims, tree, monkeypatch):
    shims("dpkg-query", """exit 1""")            # not a dpkg system, or no rocm pkgs
    r = health.rocm()
    assert r["installed"] == "9.9"               # newest core dir
    assert r["error"] is None


# ------------------------------------------------------------------------ torch

def test_torch_is_read_from_its_dist_info_name_never_imported(tree):
    site = paths.COMFY_VENV / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    (site / "torch-2.12.0a0+rocm7.13.0a20260411.dist-info").mkdir()
    (paths.COMFY_VENV / "pyvenv.cfg").write_text("home = /usr\nversion = 3.14.4\n")
    t = health.torch()
    assert t["version"] == "2.12.0a0+rocm7.13.0a20260411"
    assert (t["rocm_built"], t["series"], t["python"]) == ("7.13.0", "7.13", "3.14.4")


def test_the_comfyui_commit_is_read_locally(shims, tree):
    shims("git", "echo 'a1b2c3d 2026-09-01'")
    paths.COMFY_REPO.mkdir(parents=True, exist_ok=True)
    (paths.COMFY_REPO / ".git").mkdir()
    c = health.comfy_install()
    assert (c["commit"], c["committed"]) == ("a1b2c3d", "2026-09-01")


def test_a_missing_comfyui_repo_is_an_answer_too(tree):
    c = health.comfy_install()
    assert c["commit"] is None and "no git repo" in c["error"]


def test_no_torch_is_a_plain_answer(tree):
    assert health.torch()["installed"] is False


def test_torch_on_a_different_rocm_series_gets_an_advisory_not_an_alarm(tree, monkeypatch):
    site = paths.COMFY_VENV / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    (site / "torch-2.10.0+rocm7.13.0.dist-info").mkdir()
    monkeypatch.setattr(health, "rocm", lambda *a, **k: {"installed": "10.0.0",
                                                         "root_exists": True})
    items = health.advisories()
    torch_notes = [a for a in items if "torch" in a["text"]]
    assert torch_notes and torch_notes[0]["level"] == "info"
    assert "not urgent" in torch_notes[0]["text"]


# ---------------------------------------------------------------------- Vulkan

def _registry(current, **extra):
    entry = {"name": current, "commit": "abc", "path": str(paths.RELEASES / current)}
    entry.update(extra)
    paths.RELEASES_JSON.parent.mkdir(parents=True, exist_ok=True)
    paths.RELEASES_JSON.write_text(json.dumps(
        {"current": current, "releases": [entry]}))


def test_an_older_sdk_than_what_is_installed_offers_a_rebuild(tree, monkeypatch):
    _registry("llama.cpp-b10913", vulkan_sdk="/home/x/ai/sdk/vulkan/1.4.300.0")
    monkeypatch.setattr(health, "_fetch_latest_vulkan",
                        lambda: {"latest": "1.4.357.0", "checked_at": time.time()})
    v = health.vulkan()
    assert v["newest"] == "1.4.341.0"
    assert v["latest_known"] == "1.4.357.0"
    texts = [a["text"] for a in health.advisories(release={"name": "llama.cpp-b10913"})
             if "Vulkan" in a["text"] or "glslc" in a["text"]]
    # two separate facts: the deployed build used an older SDK than what is
    # installed, *and* a newer SDK exists upstream
    assert any("1.4.300.0" in t for t in texts), texts
    assert any("1.4.357.0" in t for t in texts), texts


def test_a_failed_version_check_is_not_retried_every_page_load(tree, monkeypatch):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return {"latest": None, "checked_at": time.time(), "error": "ConnectError"}

    monkeypatch.setattr(health, "_fetch_latest_vulkan", flaky)
    health.vulkan()
    health.vulkan()
    health.vulkan()
    assert calls["n"] == 1, "an offline check should be cached, not retried per request"


# ------------------------------------------------------------------------ disk

def test_disk_shows_the_big_trees_and_the_disposable_ones(shims, tree, tmp_path):
    sizes = {str(paths.AI_ROOT): 581_000_000_000,
             str(paths.LLAMA_MODELS): 443_000_000_000,
             str(paths.BUILD_ROOT): 5_000_000_000,
             str(paths.RELEASES): 2_500_000_000}
    # health runs `du -sxb <path>`, so the path lands in $2 of the shim
    body = 'case "$2" in\n'
    for path, size in sizes.items():
        body += f'  "{path}") echo "{size}\t{path}" ;;\n'
    body += '  *) echo "0\t$2" ;;\nesac\n'
    shims("du", body)
    for rel in (paths.LLAMA_MODELS, paths.BUILD_ROOT, paths.RELEASES):
        rel.mkdir(parents=True, exist_ok=True)
    d = health.disk()
    by_label = {r["label"]: r for r in d["trees"]}
    assert by_label["AI tree"]["bytes"] == 581_000_000_000
    assert by_label["cmake build trees"]["bytes"] == 5_000_000_000
    assert d["free_bytes"] > 0 and d["used_pct"] is not None


def test_a_scarce_disk_is_a_warning(shims, tree, monkeypatch):
    monkeypatch.setattr(health.shutil, "disk_usage",
                        lambda p: type("u", (), {"total": 2_000 * 1024**3,
                                                 "free": 20 * 1024**3,
                                                 "used": 1980 * 1024**3})())
    d = health.disk()
    assert d["free_bytes"] == 20 * 1024**3
    assert any("free" in a["text"] for a in health.advisories())


# ----------------------------------------------------------------- model store

def test_the_model_store_checks_what_the_router_can_actually_see(tree):
    model(tree, "Good-Model", "good.gguf")
    hidden = model(tree, "Big-Model", "big.gguf")
    (hidden / "draft").mkdir()
    (hidden / "draft" / "mtp-draft.gguf").write_text("x")     # never scanned
    model(tree, "hub")                                        # HF cache leftovers
    paths.PRESETS_INI.parent.mkdir(parents=True, exist_ok=True)
    paths.PRESETS_INI.write_text("[*]\nthreads = 16\n\n[Vanished-Model]\nfit = on\n")
    (paths.LLAMA_MODELS / "INDEX.json").write_text(json.dumps(
        {"index_version": "5.0", "scan": {"files_on_disk": 20, "total_bytes": 1234}}))
    link = paths.AI_BIN / "llama-server"
    link.symlink_to(paths.RELEASES / "gone" / "llama-server")   # dangling

    m = health.model_store()
    assert m["model_dirs"] == 3
    assert m["invisible_models"] == ["Big-Model/draft"]
    assert m["dirs_without_model"] == ["hub"]
    assert m["stale_presets"] == ["Vanished-Model"]
    assert m["broken_links"] == ["llama-server"]
    assert m["index"]["files_on_disk"] == 20


def test_a_stale_index_is_called_out_with_its_age(tree):
    index = paths.LLAMA_MODELS / "INDEX.json"
    index.write_text(json.dumps({"index_version": "5.0", "scan": {}}))
    old = time.time() - 12 * 24 * 3600
    os.utime(index, (old, old))
    assert health.model_store()["index"]["age_h"] > 24 * 7
    assert any("days old" in a["text"] for a in health.advisories())


def test_no_index_at_all_says_so(tree):
    assert health.model_store()["index"]["exists"] is False
    assert any("never run" in a["text"] for a in health.advisories())


# ------------------------------------------------------------------ advisories

def test_advisories_are_sorted_worst_first_and_point_somewhere(tree, monkeypatch):
    from app.core import systemd
    monkeypatch.setattr(systemd, "reboot_pending", lambda: {"reboot_required": True})
    link = paths.AI_BIN / "llama-bench"
    link.symlink_to(paths.RELEASES / "nope")
    items = health.advisories()
    assert items[0]["level"] == "bad", items          # broken farm before a pending reboot
    assert any(a["level"] == "warn" and "reboot" in a["text"].lower() for a in items)
    assert all("text" in a and "level" in a for a in items)


def test_a_quiet_machine_has_nothing_to_report(shims, tree, monkeypatch):
    from app.core import systemd
    shims("apt-get", """cat <<'EOF'
Reading package lists...
EOF
""")
    monkeypatch.setattr(systemd, "reboot_pending", lambda: {"reboot_required": False})
    model(tree, "Good-Model", "good.gguf")
    (paths.LLAMA_MODELS / "INDEX.json").write_text(
        json.dumps({"index_version": "5.0", "scan": {"files_on_disk": 1}}))
    monkeypatch.setattr(health, "_fetch_latest_vulkan",
                        lambda: {"latest": None, "checked_at": time.time(),
                                 "error": "offline"})
    items = health.advisories(service={"state": "active"}, comfy={"state": "active"})
    assert items == [], items


def test_a_service_that_is_down_is_reported(tree, monkeypatch):
    items = health.advisories(service={"state": "failed"}, comfy={"state": "active"})
    assert any("router" in a["text"] and "failed" in a["text"] for a in items)


# ---------------------------------------------------------------- caching

def test_refresh_forces_a_recheck(shims, tree, tmp_path):
    counter = tmp_path / "calls"
    shims("apt-get", """echo "Inst firefox [1] (2 Ubuntu [amd64])\"""", counter)
    health.apt()
    health.apt()
    assert counter.read_text().strip().count("\n") + 1 == 1
    health.apt(refresh=True)
    assert counter.read_text().strip().count("\n") + 1 == 2


def test_summary_always_answers_even_when_every_tool_is_gone(tree, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(health, "_fetch_latest_vulkan",
                        lambda: {"latest": None, "error": "offline"})
    s = health.summary()
    for section in ("apt", "rocm", "torch", "comfyui", "vulkan", "disk",
                    "model_store"):
        assert section in s, f"summary lost the {section} section"
    assert isinstance(s["advisories"], list)


# ------------------------------------------------------------- the page + JSON

@pytest.fixture
def client(tree, shims, monkeypatch):
    """An authenticated portal client with every probe pinned to the sandbox.

    The route tests below assert what a person sees, so nothing here may reach
    the host's package manager, ROCm, or the network.
    """
    shims("apt-get", """printf 'Inst amdrocm-base9.9 [9.9.0~pre3] (9.9.0~pre4 AMD:resolute [amd64])\\n'""")
    shims("dpkg-query", "echo 'amdrocm-base9.9 9.9.0-1'")
    monkeypatch.setattr(health, "_fetch_latest_vulkan",
                        lambda: {"latest": "1.4.357.0", "checked_at": time.time(),
                                 "source": "test"})
    from fastapi.testclient import TestClient
    from app.core import config as cfg
    from app.main import app
    c = TestClient(app)
    # mint the session cookie instead of logging in: the effective password is
    # whichever test module set AI_PORTAL_PASSWORD first, and this is a health
    # test, not an auth test
    c.cookies.set(cfg.COOKIE_NAME, cfg.make_token(cfg.load()))
    return c


def test_health_page_requires_auth():
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/health", follow_redirects=False).status_code == 302


def test_health_page_renders_every_pane(client):
    r = client.get("/health")
    assert r.status_code == 200, r.text[:800]
    for sel in ("id=\"attention\"", "id=\"gpu-stack\"", "id=\"deployed\"",
                "id=\"vulkan\"", "id=\"updates\"", "id=\"disk\"",
                "id=\"model-store\""):
        assert sel in r.text, sel
    for needle in ("Needs attention", "ROCm installed (apt)", "HIP (hipcc)",
                   "amdrocm-base9.9", "cmake build trees",
                   "invisible to llama.cpp"):
        assert needle in r.text, needle
    assert "{{" not in r.text, "unrendered template markup"


def test_the_pane_tells_you_what_it_will_not_do(client):
    """The whole point of phase F: read the machine without touching it."""
    body = client.get("/health").text
    assert "portal never runs" in body
    assert "<form" not in body.split("<main>")[1].split("</main>")[0], \
        "the health page must offer no mutation of its own"


def test_detail_json_is_the_same_bundle_as_the_page(client):
    j = client.get("/api/health/detail").json()
    for section in ("apt", "rocm", "npu", "torch", "comfyui", "vulkan", "disk",
                    "model_store", "advisories", "deployed", "host"):
        assert section in j, section
    assert j["apt"]["gpu_pending"] == ["amdrocm-base9.9"]
    assert any(a["level"] == "warn" and "amdrocm" in a["text"]
               for a in j["advisories"])
    html = client.get("/health").text
    assert "amdrocm-base9.9" in html


def test_refresh_query_bypasses_the_cache(client, tmp_path):
    calls = tmp_path / "n"
    # `shims` from the client fixture already wrote apt-get; append a counter
    script = next(Path(tmp_path).glob("bin/apt-get"))
    script.write_text('#!/bin/sh\necho run >> "%s"\nprintf "Inst x [1] (2 U [amd64])\\n"' % calls)
    health.reset_cache()
    client.get("/health")
    client.get("/health")
    assert calls.read_text().count("run") == 1, "cached by default"
    client.get("/health?refresh=1")
    assert calls.read_text().count("run") == 2, "?refresh=1 must re-probe"


# ---------------------------------------------------------------- NPU (XDNA)

XRT_SMI_EXAMINE = """
amdxdna Version      : 2.27.0_20261017, 0140477
  NPU Firmware Version : 1.1.2.65

Device(s) Present
|BDF             |Name            |Architecture  |Topology  |
|----------------|----------------|--------------|----------|
|[0000:c4:00.1]  |NPU Strix Halo  |aie2p         |6x8       |
"""


def _fake_smi(tmp_path, body=XRT_SMI_EXAMINE, mark=None):
    smi = tmp_path / "xrt-smi"
    script = "#!/bin/sh\n"
    if mark:
        script += f'touch "{mark}"\n'
    script += f'cat <<\'EOX\'\n{body}\nEOX\n'
    smi.write_text(script)
    smi.chmod(0o755)
    return smi


def test_npu_probe_reads_node_driver_firmware_and_devices(tmp_path, monkeypatch):
    node = tmp_path / "accel0"
    node.write_text("")  # a plain file is enough: the probe asks exists(), not ioctl
    (tmp_path / "modver").write_text("2.27.0_20261017\n")
    monkeypatch.setenv("AI_PORTAL_ACCEL_NODE", str(node))
    monkeypatch.setenv("AI_PORTAL_MODVER", str(tmp_path / "modver"))
    monkeypatch.setenv("AI_PORTAL_XRT_SMI", str(_fake_smi(tmp_path)))

    n = health.npu(refresh=True)
    assert n["node_exists"]
    assert n["devices"] == ["NPU Strix Halo [0000:c4:00.1]"]
    assert n["firmware"] == "1.1.2.65"
    assert n["module"] == "2.27.0_20261017"
    assert n["xrt_smi"] and n["xrt_smi_present"]


def test_xrt_smi_is_not_called_when_the_node_is_absent(tmp_path, monkeypatch):
    """A box with no kernel driver must not spend 2s of every page load on a
    subprocess that can only print '0 devices'."""
    ran = tmp_path / "ran"
    monkeypatch.setenv("AI_PORTAL_ACCEL_NODE", str(tmp_path / "nope"))
    monkeypatch.setenv("AI_PORTAL_MODVER", str(tmp_path / "modver"))
    monkeypatch.setenv("AI_PORTAL_XRT_SMI", str(_fake_smi(tmp_path, mark=ran)))

    n = health.npu(refresh=True)
    assert not n["node_exists"]
    assert n["devices"] == []
    assert not ran.exists(), "xrt-smi ran while the kernel door was closed"


def test_npu_without_xrt_userspace_is_reported_as_userspace_absent(tmp_path, monkeypatch):
    node = tmp_path / "accel0"
    node.write_text("")
    monkeypatch.setenv("AI_PORTAL_ACCEL_NODE", str(node))
    monkeypatch.setenv("AI_PORTAL_MODVER", str(tmp_path / "modver"))
    monkeypatch.setenv("AI_PORTAL_XRT_SMI", str(tmp_path / "no-xrt-smi"))

    n = health.npu(refresh=True)
    assert n["node_exists"] and not n["xrt_smi_present"] and not n["xrt_smi"]


def test_health_page_renders_the_npu_card(client, tmp_path, monkeypatch):
    node = tmp_path / "accel0"
    node.write_text("")
    (tmp_path / "modver").write_text("2.27.0_20261017\n")
    monkeypatch.setenv("AI_PORTAL_ACCEL_NODE", str(node))
    monkeypatch.setenv("AI_PORTAL_MODVER", str(tmp_path / "modver"))
    monkeypatch.setenv("AI_PORTAL_XRT_SMI", str(_fake_smi(tmp_path)))

    r = client.get("/health?refresh=1")
    assert r.status_code == 200
    page = r.text
    assert 'id="npu"' in page
    assert "NPU Strix Halo [0000:c4:00.1]" in page
    assert "1.1.2.65" in page


def test_flat_status_reports_the_npu_line_and_embed_endpoints(client):
    """The compact bundle is what scripts read; a fixture box has no NPU."""
    j = client.get("/api/health").json()
    assert j["npu"] == "—"
    assert j["embed_endpoints"] == "none"


# ------------------------------------------------------- HIP compile probe

def _fake_clang(tmp_path):
    """A clang++ that reproduces the 31.60 gap: dies on the bf16 ocml calls,
    compiles when -include (the shim) is on the command line."""
    c = tmp_path / "clang++"
    c.write_text("""#!/bin/sh
case "$*" in
  *-include*) exit 0 ;;
  *) echo "probe.cu:2:27: error: use of undeclared identifier '__ocml_log2_f32'" >&2
     exit 1 ;;
esac
""")
    c.chmod(0o755)
    return c


def test_hip_probe_spots_the_header_gap_and_the_shim_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PORTAL_CLANG_HIP", str(_fake_clang(tmp_path)))
    shim = tmp_path / "hip_bf16_log2.h"
    monkeypatch.setattr(paths, "HIP_OCML_SHIM", shim)

    h = health.hip_probe(refresh=True)
    assert h["raw_ok"] is False
    assert "__ocml_log2_f32" in (h["raw_tail"] or "")
    assert h["shim_ok"] is None, "no shim file = shim arm must not run"

    shim.write_text("// declarations\n")
    h = health.hip_probe(refresh=True)
    assert h["shim_ok"] is True
    notes = [a for a in health.advisories() if "shim" in a["text"]]
    assert notes and notes[0]["level"] == "info", "gap covered by shim = info, not alarm"


def test_hip_probe_failing_even_with_the_shim_is_bad(tmp_path, monkeypatch):
    c = tmp_path / "clang++"
    c.write_text("#!/bin/sh\necho 'error: total breakage' >&2\nexit 1\n")
    c.chmod(0o755)
    monkeypatch.setenv("AI_PORTAL_CLANG_HIP", str(c))
    shim = tmp_path / "s.h"
    shim.write_text("//")
    monkeypatch.setattr(paths, "HIP_OCML_SHIM", shim)

    h = health.hip_probe(refresh=True)
    assert h["shim_ok"] is False
    notes = [a for a in health.advisories() if "llama.cpp updates" in a["text"]]
    assert notes and notes[0]["level"] == "bad"


def test_no_clang_means_the_probe_is_inert(client, tmp_path):
    j = client.get("/api/health/detail").json()
    assert j["hip_probe"]["clang_present"] is False
    assert not [a for a in j["advisories"] if "HIP" in a["text"]
                and "llama.cpp updates" in a["text"]]
