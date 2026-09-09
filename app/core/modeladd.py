"""Smart model acquisition: turn whatever the user pasted into a correctly
filed model.

Accepted sources (exactly one):
- ``hf_cli``  — a pasted ``huggingface-cli download org/repo [file]`` (or
  ``hf download …``) command; the repo and optional file are extracted,
  everything else (flags, --local-dir, …) is ignored.
- ``hf_repo`` — a bare ``org/name`` Hugging Face repo id.
- ``url``     — a direct http(s) file URL.
- ``path``    — a file already on this machine (copied, never moved).

Placement (canonical tree, see ``paths``):
- GGUF → ``~/ai/models/llama/<dir>/<file>`` where ``<dir>`` is the filename
  stem (the tree's existing convention). ``mmproj-*`` / ``mtp-*`` /
  ``draft-*`` auxiliaries nest under their parent model's directory when it
  can be found unambiguously; an explicit ``model_dir`` always wins.
- safetensors/.ckpt/.bin → ``~/ai/comfyui/models/<category>/<file>``; the
  category is auto-classified from the tensor keys (``modelmeta``) unless the
  caller passes one.

Every placement is indexed in portal.db (``models`` + ``model_files``) so the
models page and the gguf-indexer pick it up without a rescan.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import urllib.parse
from pathlib import Path

from . import db, modelmeta, paths

# "huggingface-cli download org/repo [sub/path/file.gguf] <flags…>"
_HF_CLI_RE = re.compile(
    r"(?:huggingface-?cli|hf)\s+download\s+"
    r"(?P<repo>[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)"
    r"(?:\s+(?P<file>[^\s\"']+))?",
    re.IGNORECASE,
)
_HF_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-/]+$")
_AUX_PREFIX_RE = re.compile(r"^(mmproj|mtp|draft)-")
_WEIGHT_EXTS = (".safetensors", ".ckpt", ".bin")
_GGUF_EXTS = (".gguf",)


def parse_source(url: str | None = None, path: str | None = None,
                 hf_cli: str | None = None,
                 hf_repo: str | None = None) -> dict:
    """Normalize the (single) user-supplied source into a dict.

    Returns {"kind": "hf"|"url"|"local", "label": str, …}. Raises ValueError
    when nothing or several sources are given, or the value is unusable.
    """
    given = [n for n, v in (("url", url), ("path", path),
                            ("hf_cli", hf_cli), ("hf_repo", hf_repo))
             if v and str(v).strip()]
    if len(given) > 1:
        raise ValueError("give exactly one of url / path / hf_cli / hf_repo, "
                         f"got: {', '.join(given)}")
    if not given:
        raise ValueError("give one of url / path / hf_cli / hf_repo")
    kind = given[0]
    val = (url or path or hf_cli or hf_repo or "").strip()

    if kind == "hf_cli":
        m = _HF_CLI_RE.search(val)
        if not m:
            raise ValueError("not a huggingface-cli/hf download command: "
                             f"{val!r}")
        return {"kind": "hf", "repo_id": m.group("repo"),
                "filename": m.group("file"),
                "label": f"hf {m.group('repo')}"
                         + (f" {m.group('file')}" if m.group("file") else "")}

    if kind == "hf_repo":
        if not _HF_REPO_RE.match(val):
            raise ValueError("HF repo id should look like org/name, got "
                             f"{val!r}")
        return {"kind": "hf", "repo_id": val, "filename": None,
                "label": f"hf {val}"}

    if kind == "url":
        if not re.match(r"^https?://", val):
            raise ValueError("url must start with http:// or https://")
        name = urllib.parse.urlparse(val).path.rsplit("/", 1)[-1]
        if not name or name.startswith("."):
            raise ValueError(f"cannot derive a filename from url: {val!r}")
        return {"kind": "url", "url": val, "filename": name,
                "label": val}

    # local path
    p = Path(val).expanduser()
    if not p.is_file():
        raise ValueError(f"local path is not a file: {p}")
    return {"kind": "local", "path": str(p), "filename": p.name,
            "label": str(p)}


# ------------------------------------------------------------------ fetching

def _sha256(p: Path, log=print) -> str:
    h = hashlib.sha256()
    n = 0
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
            n += len(chunk)
            if n // (256 * 1024 * 1024) > (n - len(chunk)) // (256 * 1024 * 1024):
                log(f"  hashing… {n / 1e6:.0f} MB")
    return h.hexdigest()


def _fetch_local(src: dict, staging: Path, log) -> Path:
    p = Path(src["path"])
    log(f"copying {p} ({p.stat().st_size / 1e6:.1f} MB)")
    dest = staging / p.name
    shutil.copy2(p, dest)
    return dest


def _fetch_url(src: dict, staging: Path, log) -> Path:
    import httpx

    name = src["filename"]
    tmp = staging / (name + ".part")
    log(f"downloading {src['url']}")
    done = 0
    with httpx.stream("GET", src["url"], follow_redirects=True,
                      timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(256 * 1024):
                f.write(chunk)
                done += len(chunk)
                if total and done // (64 * 1024 * 1024) > \
                        (done - len(chunk)) // (64 * 1024 * 1024):
                    log(f"  {done / 1e6:.0f} MB of {total / 1e6:.0f} MB")
    tmp.rename(staging / name)
    log(f"downloaded {name} ({done / 1e6:.1f} MB)")
    return staging / name


def _hf_weight_files(repo_id: str) -> list[str]:
    import huggingface_hub

    files = huggingface_hub.list_repo_files(repo_id, repo_type="model")
    return sorted(f for f in files
                  if f.lower().endswith(_WEIGHT_EXTS + _GGUF_EXTS)
                  and not f.startswith(".") and "/.cache" not in f)


def _fetch_hf(src: dict, staging: Path, log) -> Path:
    import huggingface_hub

    repo, filename = src["repo_id"], src.get("filename")
    if not filename:
        candidates = _hf_weight_files(repo)
        if len(candidates) == 1:
            filename = candidates[0]
            log(f"repo has one weight file — using {filename}")
        elif not candidates:
            raise ValueError(f"no model weight files found in {repo}")
        else:
            raise ValueError(
                f"{repo} has several weight files — pass filename: "
                + ", ".join(candidates[:12]))
    log(f"downloading {repo}/{filename} via huggingface_hub")
    local = huggingface_hub.hf_hub_download(
        repo_id=repo, filename=filename, local_dir=str(staging))
    p = Path(local)
    log(f"downloaded {p.name} ({p.stat().st_size / 1e6:.1f} MB)")
    return p


# ----------------------------------------------------------------- placement

def _match_parent_dir(base: str) -> Path | None:
    """For an auxiliary file (mmproj/mtp/draft), find the parent model's
    directory by longest common prefix (>= 8 chars) of its name. Returns
    None when no — or more than one plausible — directory matches."""
    if not paths.LLAMA_MODELS.is_dir():
        return None
    m = _AUX_PREFIX_RE.match(base)
    if m:
        base = base[m.end():]
    best, best_len = None, 0
    for d in paths.LLAMA_MODELS.iterdir():
        if not d.is_dir():
            continue
        n = 0
        for a, b in zip(d.name, base):
            if a != b:
                break
            n += 1
        if n >= 8 and n > best_len:
            best, best_len = d, n
    return best


def _gguf_file_role(name: str) -> str:
    if name.startswith("mmproj-"):
        return "mmproj"
    if name.startswith(("mtp-", "draft-")):
        return "draft"
    return "gguf"


def _place_gguf(local: Path, model_dir: str | None, log) -> tuple[Path, Path]:
    """Move a staged .gguf into ~/ai/models/llama/<dir>/; returns (dir, file)."""
    name = local.name
    stem = name[: -len(".gguf")]
    if model_dir:
        d = paths.LLAMA_MODELS / model_dir
    elif _AUX_PREFIX_RE.match(name):
        parent = _match_parent_dir(stem)
        if parent is None:
            log(f"note: no unambiguous parent model dir for {name} — "
                f"creating {stem}/ (pass model_dir to nest it)")
        d = parent or (paths.LLAMA_MODELS / stem)
    else:
        d = paths.LLAMA_MODELS / stem
    d.mkdir(parents=True, exist_ok=True)
    dest = d / name
    if dest.exists():
        raise ValueError(f"{dest} already exists")
    shutil.move(str(local), str(dest))
    log(f"placed {name} in {d.relative_to(paths.HOME)}/")
    return d, dest


def _place_weight(local: Path, meta: dict, category: str | None, log
                  ) -> tuple[Path, Path, str]:
    """Move a staged safetensors/.ckpt/.bin into
    ~/ai/comfyui/models/<category>/; returns (dir, file, category)."""
    size = local.stat().st_size
    if local.suffix.lower() == ".safetensors" and not category:
        category = modelmeta.classify_safetensors(meta.get("keys", []), size)
        if category:
            log(f"auto-classified as {category}/")
    if not category:
        have = sorted(x.name for x in paths.COMFY_MODELS.iterdir()
                      if x.is_dir()) if paths.COMFY_MODELS.is_dir() else []
        raise ValueError(
            f"could not decide which ComfyUI models/ folder {local.name} "
            f"belongs to — pass category (existing: {', '.join(have) or '—'})")
    d = paths.COMFY_MODELS / category
    d.mkdir(parents=True, exist_ok=True)
    dest = d / local.name
    if dest.exists():
        raise ValueError(f"{dest} already exists")
    shutil.move(str(local), str(dest))
    log(f"placed {local.name} in {d.relative_to(paths.HOME)}/")
    return d, dest, category


# ------------------------------------------------------------------- indexing

def _meta_for_db(meta: dict) -> dict:
    """Shrink a sniffed header for storage (safetensors keys can be long)."""
    m = {k: v for k, v in meta.items() if k != "keys"}
    keys = meta.get("keys") or []
    m["tensor_count"] = meta.get("num_tensors") or len(keys)
    m["sample_keys"] = keys[:25]
    return m


def _index_llama_dir(d: Path, touched: Path, touched_sha: str,
                    touched_meta: dict | None = None) -> None:
    """(Re)index a whole llama model dir; pre-existing files keep whatever
    hash/meta they already had (a 270 GB gguf is not re-hashed on a re-index)."""
    files = []
    for f in sorted(d.rglob("*")):
        if not f.is_file() or f.suffix.lower() != ".gguf":
            continue
        if f == touched:
            sha, meta = touched_sha, touched_meta
        else:
            prev = db.query("SELECT sha256, meta FROM model_files WHERE path=?",
                            (str(f),))
            sha = prev[0][0] if prev and prev[0][0] else None
            meta = json.loads(prev[0][1]) if prev and prev[0][1] else None
        files.append({"name": str(f.relative_to(d)),
                      "size": f.stat().st_size, "sha256": sha,
                      "role": _gguf_file_role(f.name), "meta": meta})
    db.index_model(d.name, d, "llama", files)


def add_model(src: dict, filename: str | None = None,
              category: str | None = None, model_dir: str | None = None,
              log=print) -> dict:
    """Full pipeline: fetch → sniff → place → index. Raises ValueError on
    anything unusable; the caller (job runner) records it as a failure."""
    if filename:
        src = {**src, "filename": filename}
    staging = paths.STATE / "downloads" / f"add-{os.getpid()}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        local = {"local": _fetch_local, "url": _fetch_url,
                 "hf": _fetch_hf}[src["kind"]](src, staging, log)

        meta = modelmeta.sniff_file(local)
        if meta is None:
            raise ValueError(f"unrecognized model format: {local.name} "
                             "(need .gguf or .safetensors/.ckpt/.bin)")
        log(f"format: {meta['format']}"
            + (f" · {meta.get('name') or meta.get('arch')}"
               if meta["format"] == "gguf" else ""))

        if meta["format"] == "gguf":
            d, dest = _place_gguf(local, model_dir, log)
            log("hashing new file…")
            sha = _sha256(dest, log)
            _index_llama_dir(d, dest, sha, _meta_for_db(meta))
            model_id = d.name
        else:
            d, dest, cat = _place_weight(local, meta, category, log)
            sha = _sha256(dest, log)
            model_id = f"comfyui/{cat}/{dest.name}"
            db.index_model(model_id, d, "comfyui",
                           [{"name": dest.name, "size": dest.stat().st_size,
                             "sha256": sha,
                             "role": dest.suffix.lstrip(".").lower(),
                             "meta": _meta_for_db(meta)}])
        log(f"indexed {model_id} in portal.db")
        return {"path": str(dest), "model": model_id,
                "bytes": dest.stat().st_size, "sha256": sha}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
