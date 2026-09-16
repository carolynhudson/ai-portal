"""GPU memory reporting on an iGPU box.

Why these tests exist: the first version of `_gtt()` globbed
`/sys/devices/virtual/drm/card*/device` and silently returned `{}` on this
machine, because the GPU is `card1` and is reached via `/sys/class/drm`. A
function that returns "no data" on the only hardware it is meant to run on looks
identical to a function that works, unless something asserts the numbers.

The point of the whole module: on a Strix Halo (gfx1151) there is no discrete
VRAM. `rocm-smi` reports ~512 MiB while the memory the models live in is the GTT
aperture (108 GiB after the `amdttm pages_limit` fix). Reading only rocm-smi
shows an iGPU as nearly full and hides the figure the §4.7 memory budget is
planned against.
"""
from __future__ import annotations

from pathlib import Path

from app import metrics


def _mkfs(tmp_path: Path, *, card: str, gtt_total: int, gtt_used: int,
          vram_total: int) -> Path:
    """Build a stand-in for /sys/class/drm/cardN/device."""
    dev = tmp_path / "pci0000:00" / "0000:c3:00.0"
    dev.mkdir(parents=True)
    (dev / "mem_info_gtt_total").write_text(f"{gtt_total}\n")
    (dev / "mem_info_gtt_used").write_text(f"{gtt_used}\n")
    (dev / "mem_info_vram_total").write_text(f"{vram_total}\n")
    # symlink_to does not create intermediate directories, so cardN itself has
    # to exist before "device" can point at the fake PCI node.
    drm = tmp_path / "drm"
    (drm / card).mkdir(parents=True)
    (drm / card / "device").symlink_to(dev, target_is_directory=True)
    # Connector children must be ignored: card1-DP-1 has no mem_info at all.
    (drm / f"{card}-DP-1").mkdir()
    return drm


GiB = 1 << 30


def test_gtt_is_read_from_the_class_drm_symlink(tmp_path, monkeypatch):
    drm = _mkfs(tmp_path, card="card1", gtt_total=108 * GiB,
                gtt_used=71 * GiB, vram_total=512 * 1024 * 1024)
    monkeypatch.setattr(metrics, "_DRM_CARDS", drm)
    got = metrics._gtt()
    assert got["gtt_total"] == 108 * GiB
    assert got["gtt_used"] == 71 * GiB
    assert got["vram_total_driver"] == 512 * 1024 * 1024


def test_connector_dirs_are_not_mistaken_for_a_gpu(tmp_path, monkeypatch):
    """A card1-DP-1 entry exists on real hardware and must not be treated as a
    second device — it has no mem_info files, so matching it would hide card1."""
    drm = _mkfs(tmp_path, card="card1", gtt_total=108 * GiB,
                gtt_used=1 * GiB, vram_total=512 * 1024 * 1024)
    monkeypatch.setattr(metrics, "_DRM_CARDS", drm)
    assert metrics._gpu_dev() is not None
    assert metrics._gtt()["gtt_total"] == 108 * GiB


def test_no_gpu_reads_as_absent_not_as_zero(tmp_path, monkeypatch):
    """No data must not look like a GPU with an empty aperture: the caller uses
    the difference to decide whether to show a pool at all."""
    monkeypatch.setattr(metrics, "_DRM_CARDS", tmp_path / "absent")
    assert metrics._gtt() == {}
    assert metrics._gpu_dev() is None


def test_unreadable_sysfs_degrades_to_empty(tmp_path, monkeypatch):
    """A truncated or garbage value must not raise out of a metrics read; that
    would take the status page down over one odd driver file."""
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "mem_info_gtt_total").write_text("not-a-number\n")
    drm = tmp_path / "drm"
    (drm / "card0").mkdir(parents=True)
    (drm / "card0" / "device").symlink_to(dev, target_is_directory=True)
    monkeypatch.setattr(metrics, "_DRM_CARDS", drm)
    assert metrics._gtt() == {}


def test_igpu_reports_gtt_as_the_pool_not_the_vram_stub(tmp_path, monkeypatch):
    """The bug this whole change is about: rocm-smi's 512 MiB VRAM stub must not
    become the reported pool when GTT is 108 GiB."""
    drm = _mkfs(tmp_path, card="card1", gtt_total=108 * GiB,
                gtt_used=71 * GiB, vram_total=512 * 1024 * 1024)
    monkeypatch.setattr(metrics, "_DRM_CARDS", drm)
    monkeypatch.setattr(metrics, "_GPU_CACHE", (0.0, None))
    monkeypatch.setattr(metrics.subprocess, "run",
                        lambda *a, **k: _FakeRun(_ROCM_SMI_IGPU))

    g = _require_gpu(metrics.gpu())
    assert g["is_igpu"] is True
    assert g["pool_total"] == 108 * GiB
    assert g["pool_used"] == 71 * GiB
    # the stub is still reported, but clearly labelled as not the pool
    assert g["vram_total"] == 512 * 1024 * 1024
    assert g["pool_total"] > g["vram_total"] * 100


def test_discrete_card_keeps_vram_as_the_pool(tmp_path, monkeypatch):
    """The same code must stay honest on a real dGPU, where VRAM is the pool and
    GTT is only the spill area."""
    drm = _mkfs(tmp_path, card="card0", gtt_total=16 * GiB,
                gtt_used=1 * GiB, vram_total=24 * GiB)
    monkeypatch.setattr(metrics, "_DRM_CARDS", drm)
    monkeypatch.setattr(metrics, "_GPU_CACHE", (0.0, None))
    monkeypatch.setattr(metrics.subprocess, "run",
                        lambda *a, **k: _FakeRun(_ROCM_SMI_DGPU))

    g = _require_gpu(metrics.gpu())
    assert g["is_igpu"] is False
    assert g["pool_total"] == 24 * GiB


def test_gpu_survives_rocmsmi_missing(tmp_path, monkeypatch):
    """No rocm-smi (or a crash) must not lose the driver numbers."""
    drm = _mkfs(tmp_path, card="card1", gtt_total=108 * GiB,
                gtt_used=71 * GiB, vram_total=512 * 1024 * 1024)
    monkeypatch.setattr(metrics, "_DRM_CARDS", drm)
    monkeypatch.setattr(metrics, "_GPU_CACHE", (0.0, None))

    def boom(*a, **k):
        raise FileNotFoundError("rocm-smi")

    monkeypatch.setattr(metrics.subprocess, "run", boom)
    g = _require_gpu(metrics.gpu())
    assert g["pool_total"] == 108 * GiB


def _require_gpu(g: dict | None) -> dict:
    """Assert-and-narrow: gpu() may legitimately be None, and indexing None
    produces an unreadable test failure that hides which assertion broke."""
    assert g is not None, "gpu() returned None — driver data was not merged"
    return g


def test_first_int_tolerates_junk():
    assert metrics._first_int("VRAM Total Memory (B): 536870912") == 536870912
    assert metrics._first_int("no digits here") is None
    assert metrics._first_int("inf") is None       # int(inf) raises OverflowError


class _FakeRun:
    def __init__(self, out: str):
        self.stdout, self.stderr, self.returncode = out, "", 0


_ROCM_SMI_IGPU = """
GPU[0]\t\t: GPU use (%):  91.00
GPU[0]\t\t: VRAM Total Memory (B): 536870912
GPU[0]\t\t: VRAM Total Used Memory (B): 516562944
"""

_ROCM_SMI_DGPU = """
GPU[0]\t\t: GPU use (%):  12.00
GPU[0]\t\t: VRAM Total Memory (B): 25769803776
GPU[0]\t\t: VRAM Total Used Memory (B): 1073741824
"""


# ------------------------------------------------------------------ advisory
# The 64 GiB ceiling fails quietly: a model above the aperture loads and then
# reports a 4096-token context, so it looks like a bad model rather than lost
# configuration. These assert the portal notices the regression instead of
# leaving the owner to rediscover it from a mysteriously small context window.

def test_healthy_aperture_is_silent():
    from app.core import health
    assert health.aperture_advisory({"gtt_total": 108 * GiB}) == []


def test_regressed_to_the_amdgpu_default_is_bad():
    from app.core import health
    a = health.aperture_advisory({"gtt_total": 64 * GiB})
    assert len(a) == 1
    assert a[0]["level"] == "bad"
    # the text must name the symptom, not just the number, or it is not actionable
    assert "4096" in a[0]["text"]
    assert "amdttm.conf" in a[0]["text"]


def test_partially_regressed_aperture_warns():
    from app.core import health
    a = health.aperture_advisory({"gtt_total": 90 * GiB})
    assert len(a) == 1 and a[0]["level"] == "warn"


def test_probed_but_unreadable_says_so():
    """{} means the probe ran and found nothing — different from not probing."""
    from app.core import health
    a = health.aperture_advisory({})
    assert len(a) == 1
    assert a[0]["level"] == "info"
    assert "512 MiB" in a[0]["text"]      # explains why rocm-smi VRAM misleads


def test_not_probed_stays_quiet():
    """None means "not read", which is normal without amdgpu and must not warn.
    Collapsing this with {} would cry wolf on every non-AMD machine."""
    from app.core import health
    assert health.aperture_advisory(None) == []


def test_zero_gtt_counts_as_unreadable_not_as_full():
    from app.core import health
    a = health.aperture_advisory({"gtt_total": 0, "gtt_used": 0})
    assert a and a[0]["level"] == "info"


def test_aperture_advisory_leads_the_list():
    """A shrunken aperture invalidates every other capacity judgement on the
    page, so it is emitted first — before reboot-pending and the rest."""
    from app.core import health
    out = health.advisories(aperture={"gtt_total": 64 * GiB})
    assert out, "expected at least the aperture advisory"
    assert "4096" in out[0]["text"], "aperture advisory must come first"


def test_advisories_accepts_aperture_keyword():
    from app.core import health
    out = health.advisories(aperture={"gtt_total": 64 * GiB})
    assert any("4096" in a["text"] for a in out)


def test_gpu_aperture_degrades_to_none(monkeypatch):
    """A crash in the metrics layer must not take the health page down."""
    from app.core import health

    def boom(*a, **k):
        raise RuntimeError("driver exploded")

    import app.metrics as metrics
    monkeypatch.setattr(metrics, "gpu", boom)
    monkeypatch.setattr(metrics, "_GPU_CACHE", (0.0, None))
    assert health.gpu_aperture() is None
