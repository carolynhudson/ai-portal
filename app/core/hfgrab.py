"""The -hf law: one spec, every file the model needs, placed by type.

llama.cpp's ``-hf org/model:QUANT`` resolves to *a set of files*: the quant's
weights (all parts when a model is split), the mmproj sidecar, and nothing
else — a repo's LoRAs, imatrix dumps, and README images are not "the model".
This module is that law on the portal side, plus what llama.cpp -hf does not
do: it downloads into the HF cache and leaves the files there; here a
finished grab hands off to the migration ladder for GGUF (the full law:
preflight, verified copy, refs follow, cache names release last) and to the
tensor-key classifier for Comfy weights (the same signals ComfyUI's own
loaders use, with the repo's folder names as the prior).

Controls are file-granular and honest: ``hf_hub_download`` cannot be
interrupted mid-file, so pause/cancel stop *new files from starting* and the
in-flight file finishes. The HF cache is the resume: a re-run re-fetches
only what is missing. That is also why downloads use the hub cache at all.

One grab runs at a time (job kind ``model-grab`` is non-disruptive: one of
its kind, and never alongside a disruptive job) — hence the single-slot
control slot below; a second concurrent grab is refused at the route.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, modelindex, modelmeta, modelmigrate, paths

GRAB_KIND = "model-grab"
WORKERS = 2
RETRIES = 3
HEADROOM = 20 * 2**30          # refuse a plan that leaves < 20 GiB free

_sidecars = ("mmproj", "mtp-", "dspark-", "dflash-")
_junk = ("imatrix", "images/", "lora", "assets", "media")
_weights_ext = (".safetensors", ".ckpt", ".bin", ".pt", ".pth")

# single-slot control: {command: running|pause|cancel}
_ctl = {"command": "running", "done_bytes": 0, "total_bytes": 0}
_ctl_lock = threading.Lock()


class GrabCancelled(Exception):
    pass


def set_command(cmd: str) -> str:
    """pause | resume | cancel for the running grab (file-granular).
    ``resume`` normalizes to ``running`` — the slot carries the STATE,
    not the verb that set it."""
    if cmd not in ("pause", "resume", "cancel", "running"):
        raise ValueError("command must be pause, resume, cancel, or running")
    norm = "running" if cmd == "resume" else cmd
    with _ctl_lock:
        _ctl["command"] = norm
    return norm


def _command() -> str:
    with _ctl_lock:
        return _ctl["command"]


def _bump(n: int) -> None:
    with _ctl_lock:
        _ctl["done_bytes"] += n


# ------------------------------------------------------------------ spec

def parse_spec(text: str) -> dict:
    """``org/model:QUANT`` (llama.cpp -hf shape), optionally as a pasted
    ``hf download`` / ``huggingface-cli download`` command. Returns
    {repo, quant, label}. Comfy repos carry no quant."""
    s = (text or "").strip()
    for pre in ("huggingface-cli download", "hf download", "huggingface-cli",
                "hf-cli download"):
        if s.lower().startswith(pre):
            s = s[len(pre):].strip()
            break
    for flag in ("--include", "--local-dir", "--token", "--repo-type"):
        i = s.find(flag)
        if i > 0:
            s = s[:i].strip()
    tok = s.split(maxsplit=1)
    spec = tok[0] if tok else ""
    # a pasted CLI command may name one FILE second (hf download repo file):
    # that is modeladd's single-file path, not this law — a second token that
    # looks like a file drops to the repo.
    repo = spec.split(":", 1)[0] if (spec.count(":") == 1 and
                                     "/" not in spec.split(":", 1)[1]) else spec
    quant = None
    if spec.count(":") == 1 and "/" not in spec.split(":", 1)[1]:
        repo, quant = spec.split(":", 1)
    if "/" not in repo or not repo or not repo.split("/")[0]:
        raise ValueError("expected org/model or org/model:QUANT")
    label = repo + (":" + quant if quant else "")
    return {"repo": repo, "quant": quant, "label": label}


def _split_base(name: str) -> str:
    """Qwen-...-00001-of-00002.gguf -> Qwen-...gguf (the split stem,
    digits and their dash gone). _shard_base only blanks the digits, so
    the dash that joined them is removed here — witnessed: a split
    BF16's quant read as '' and the model vanished from its own menu."""
    base = name.rsplit("/", 1)[-1]
    stem = modelmigrate._shard_base(base)
    stem = stem[: stem.rfind(".")] if "." in stem else stem
    return stem.rstrip("-_")


def _quant_of(name: str) -> str:
    stem = _split_base(name)
    return stem.rsplit("-", 1)[-1] if "-" in stem else stem


def _is_junk(name: str) -> bool:
    low = name.lower()
    return any(j in low for j in _junk) and not any(s in low for s in _sidecars)


def _is_sidecar(name: str) -> bool:
    low = name.lower().rsplit("/", 1)[-1]
    return any(low.startswith(s) or s in low for s in _sidecars)


def quants_of(siblings: list[tuple[str, int]]) -> list[dict]:
    """[{tag, bytes, files}] for every quant in a (name, size) list —
    split parts summed under the base tag, sidecars and junk excluded."""
    acc: dict[str, dict] = {}
    for name, size in siblings:
        if not name.lower().endswith(".gguf") or _is_sidecar(name) or _is_junk(name):
            continue
        tag = _quant_of(name)
        a = acc.setdefault(tag, {"tag": tag, "bytes": 0, "files": 0})
        a["bytes"] += size
        a["files"] += 1
    return sorted(acc.values(), key=lambda a: a["bytes"])


def _siblings(repo: str) -> list[tuple[str, int]]:
    from huggingface_hub import HfApi
    info = HfApi().model_info(repo, files_metadata=True, timeout=30)
    out = []
    for s in info.siblings:
        name = s.rfilename
        out.append((name, int(s.size or 0)))
    return out


def plan(repo: str, quant: str | None, siblings: list[tuple[str, int]]
         ) -> dict:
    """The file set for one spec. GGUF repos: the quant's files (all split
    parts) + every sidecar; no quant named -> refuse with the menu (llama.cpp
    -hf without a quant is undefined, so this says 'pick one' with numbers).
    Weight repos (Comfy): every weight file, with its repo folder as the
    placement prior."""
    ggufs = [(n, s) for n, s in siblings if n.lower().endswith(".gguf")
             and not _is_junk(n)]
    files: list[dict] = []
    out_pruned: list = []
    if ggufs:
        if not quant:
            menu = ", ".join(f"{q['tag']} ({q['bytes'] / 2**30:.1f} G"
                             + (f", {q['files']} parts" if q['files'] > 1 else "")
                             + ")" for q in quants_of(siblings))
            raise ValueError(
                "this is a GGUF repo — name the quant, e.g. "
                f"{repo}:Q6_K. available: {menu or 'none listed'}")
        want = quant.casefold()
        for n, s in ggufs:
            if _is_sidecar(n):
                files.append({"name": n, "bytes": s, "role": "sidecar"})
            elif _quant_of(n).casefold() == want:
                files.append({"name": n, "bytes": s, "role": "model"})
        if not any(f["role"] == "model" for f in files):
            menu = ", ".join(q["tag"] for q in quants_of(siblings))
            raise ValueError(f"no {quant} files in {repo}. available: {menu}")
        kind = "gguf"
    else:
        files = [{"name": n, "bytes": s, "role": "weight"}
                 for n, s in siblings
                 if n.lower().endswith(_weights_ext) and not _is_junk(n)]
        kind = "comfy"
        if not files:
            raise ValueError(f"no safetensors/ckpt weights in {repo}")
        # CHAIN LAW, per category: the variant filter picks within a
        # category, and where NO name carries the word (the qwen VAE has
        # no fp8 flavor) the category keeps what the chain needs — a
        # filter must never amputate a role. Within a category, one file:
        # the smallest variant wins, every pruning logged; the owner
        # pulls a named variant with a narrower spec (witnessed:
        # Qwen-Image ComfyUI = 260 G whole-repo, five diffusion flavors).
        by_cat: dict[str, list] = {}
        for f in files:
            parts = f["name"].split("/")
            cat = parts[-2] if len(parts) >= 2 else "root"
            by_cat.setdefault(cat, []).append(f)
        kept, pruned = [], []
        matched = False
        for cat, fs in sorted(by_cat.items()):
            pool = fs
            if quant:
                m = [f for f in fs if quant.casefold() in f["name"].casefold()]
                if m:
                    pool = m
                    matched = True
            pool = sorted(pool, key=lambda f: f["bytes"])
            kept.append(pool[0])
            pruned.extend(pool[1:])
            pruned.extend(f for f in fs if f not in pool)
        if quant and not matched:
            menu = ", ".join(sorted(Path(f["name"]).name
                                    for f in files))[:300]
            raise ValueError(f"no weights matching {quant} in {repo}. "
                             f"files: {menu}")
        files = sorted(kept, key=lambda f: f["name"])
        out_pruned = [(f["name"], f["bytes"]) for f in pruned]
    model_bytes = sum(f["bytes"] for f in files if f["role"] == "model")
    return {"repo": repo, "quant": quant, "kind": kind, "files": files,
            "model_bytes": model_bytes, "pruned": out_pruned,
            "total_bytes": sum(f["bytes"] for f in files)}


# ------------------------------------------------------------------ grab

def _download(repo: str, f: dict, log) -> Path:
    """One file into the HF cache, with retries. Pause/cancel gate BEFORE
    each file (file-granular law); mid-file is not interruptible, and the
    cache makes the next attempt resume inside the hub's own machinery."""
    from huggingface_hub import hf_hub_download
    while True:
        cmd = _command()
        if cmd == "cancel":
            raise GrabCancelled()
        if cmd == "pause":
            log(f"paused — {f['name']} not started (resume to continue)")
            while _command() == "pause":
                time.sleep(2)
            continue
        break
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            local = Path(hf_hub_download(repo_id=repo, filename=f["name"]))
            f["local"] = str(local)
            _bump(f["bytes"])
            log(f"fetched {f['name']} ({f['bytes'] / 2**30:.2f} G)")
            return local
        except GrabCancelled:
            raise
        except Exception as e:      # hub, network, disk: all retry-shaped
            last = e
            if attempt == RETRIES:
                break
            log(f"{f['name']}: attempt {attempt} failed "
                f"({type(e).__name__}: {e}) — retrying in {5 * attempt}s")
            slept = 0
            while slept < 5 * attempt:
                if _command() == "cancel":
                    raise GrabCancelled() from e
                time.sleep(2)
                slept += 2
    raise RuntimeError(f"{f['name']}: {last}")


def _space_guard(total: int, log) -> None:
    # the nearest EXISTING ancestor: a repo that has never downloaded leaves
    # ~/.cache/huggingface uncreated, and statvfs of a missing path is a
    # FileNotFoundError wearing an I/O costume
    probe = paths.HF_CACHE
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    log(f"free space: {free / 2**30:.0f} G, plan needs {total / 2**30:.1f} G")
    if free < total + HEADROOM:
        raise RuntimeError(
            f"not enough free space: {free / 2**30:.0f} G free, plan wants "
            f"{total / 2**30:.1f} G + {HEADROOM / 2**30:.0f} G headroom")


def _land_sidecar(local: Path, dest: Path, log) -> None:
    """A sidecar follows its model into the model's library dir, under the
    same copy+verify law: hardlink on one filesystem (inode + size prove it),
    copy + sha256 compare across devices. The cache row survives; the
    ladder's drop pass releases it when its own cache row migrates."""
    final = dest / local.name
    if final.exists():
        log(f"{final.name}: already in {dest.name}/ — left alone")
        return
    dest.mkdir(parents=True, exist_ok=True)
    try:
        os.link(local, final)
        if final.stat().st_ino == local.stat().st_ino:
            log(f"{final.name} hardlinked into {dest.name}/")
            return
        final.unlink()
    except OSError:
        with contextlib.suppress(OSError):
            final.unlink()
    tmp = dest / (".grab-" + local.name)
    shutil.copyfile(local, tmp)
    a = hashlib.sha256(local.read_bytes()).hexdigest()
    b = hashlib.sha256(tmp.read_bytes()).hexdigest()
    if a != b:
        tmp.unlink()
        raise RuntimeError(f"{local.name}: copy verification failed")
    os.replace(tmp, final)
    log(f"{final.name} copied+verified into {dest.name}/")


def _comfy_category(f: dict, log) -> str:
    """Comfy placement: tensor keys first (the same signals ComfyUI's
    loaders use), the repo's folder name as the prior, then refuse —
    the pane asks for nothing, and a refusal names what it saw."""
    local = Path(f["local"])
    # the PRIOR is the deepest repo folder ("split_files/diffusion_models/x"
    # -> diffusion_models; the first segment is the repo's layout word,
    # not a Comfy category)
    parts = f["name"].split("/")
    prior = parts[-2] if len(parts) >= 2 else ""
    known = sorted(x.name for x in paths.COMFY_MODELS.iterdir()
                   if x.is_dir()) if paths.COMFY_MODELS.is_dir() else []
    cat = None
    if local.suffix.lower() == ".safetensors":
        meta = modelmeta.sniff_safetensors(local)
        cat = modelmeta.classify_safetensors((meta or {}).get("keys", []),
                                             local.stat().st_size)
    elif local.suffix.lower() in (".ckpt", ".bin"):
        cat = "checkpoints"
    if not cat and prior.casefold() in [k.lower() for k in known]:
        cat = prior
        log(f"tensor keys inconclusive — using repo folder '{prior}/'")
    if not cat:
        raise RuntimeError(
            f"cannot classify {local.name} for a ComfyUI models/ folder "
            f"(tensor keys + repo folder both inconclusive) — known folders: "
            f"{', '.join(known)}")
    return cat


def _place_comfy(plan: dict, log) -> list[str]:
    placed = []
    for f in plan["files"]:
        local = Path(f["local"])
        cat = _comfy_category(f, log)
        dest = paths.COMFY_MODELS / cat / local.name
        if dest.exists():
            log(f"{local.name}: already in {cat}/ — left alone")
            placed.append(str(dest))
            continue
        (paths.COMFY_MODELS / cat).mkdir(parents=True, exist_ok=True)
        tmp = paths.COMFY_MODELS / cat / (".grab-" + local.name)
        shutil.copyfile(local, tmp)
        h = hashlib.sha256()
        with tmp.open("rb") as fh:
            for blk in iter(lambda: fh.read(2**20), b""):
                h.update(blk)
        if h.hexdigest() != hashlib.sha256(local.read_bytes()).hexdigest():
            tmp.unlink()
            raise RuntimeError(f"{local.name}: copy verification failed")
        os.replace(tmp, dest)
        log(f"placed {local.name} in {cat}/")
        placed.append(str(dest))
    return placed


def _place_gguf(plan: dict, log) -> list[str]:
    """The handoff: index the fresh cache rows, run the ladder for the
    quant, then the sidecars follow into the SAME directory (the router's
    sidecar law: mmproj lives in the model's dir)."""
    modelindex.rescan()
    p = modelmigrate.plan_for(plan["repo"], plan["quant"])
    applied = modelmigrate.apply(p, log)
    dest = Path(applied["dest"])
    placed = [str(dest / f["name"].rsplit("/", 1)[-1])
              for f in p.get("files", [])]
    for f in plan["files"]:
        if f["role"] != "sidecar":
            continue
        _land_sidecar(Path(f["local"]), dest, log)
        placed.append(str(dest / Path(f["name"]).name))
    modelindex.rescan()
    db.event("portal", "model-grab",
             f"{plan['repo']}:{plan['quant']} -> {dest.name}/")
    return placed


def grab(spec: dict, log) -> dict:
    """The job body: plan -> space guard -> download (controls live) ->
    place (ladder for GGUF, classifier for Comfy)."""
    from huggingface_hub import HfApi
    with _ctl_lock:
        _ctl.update(command="running", done_bytes=0, total_bytes=0)
    log(f"planning {spec['label']}")
    siblings = _siblings(spec["repo"])
    pl = plan(spec["repo"], spec["quant"], siblings)
    for f in pl["files"]:
        log(f"  {f['role']:7s} {f['bytes'] / 2**30:8.2f} G  {f['name']}")
    for name, size in pl.get("pruned", []):
        log(f"  pruned  {size / 2**30:8.2f} G  {name} "
            "(chain law: smallest variant per category)")
    _space_guard(pl["total_bytes"], log)
    with _ctl_lock:
        _ctl["total_bytes"] = pl["total_bytes"]
    log(f"downloading {len(pl['files'])} file(s), "
        f"{pl['total_bytes'] / 2**30:.1f} G, {WORKERS} workers")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_download, spec["repo"], f, log)
                   for f in pl["files"]]
        for fu in futures:
            fu.result()            # first failure fails the job; the cache resumes
    if _command() == "cancel":
        raise GrabCancelled()
    log("all parts present — placing")
    placed = (_place_gguf(pl, log) if pl["kind"] == "gguf"
              else _place_comfy(pl, log))
    return {"repo": pl["repo"], "quant": pl["quant"], "kind": pl["kind"],
            "bytes": pl["total_bytes"], "placed": placed,
            "dir": (pl["quant"] if pl["kind"] == "gguf" else "comfyui")}
