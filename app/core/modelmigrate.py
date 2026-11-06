"""Cache → library migration for llama router models (PORTAL_PLAN §6.14.1zk).

The HF download cache is perfect as a download cache and opaque as a model
store; this module moves models into ``~/ai/models/llama`` — the router's
``--models-dir`` — where the portal owns the layout, the inventory and the
references. The rules below are not taste: every one of them is a router
behaviour, an incident, or an owner ruling.

**The depth law** (`common/preset.cpp:387`, source-verified b12110 era): the
router scans ``--models-dir`` one level deep; each subdirectory is exactly one
model, named by the directory, and inside it the LAST plain ``.gguf`` iterated
becomes the model file. A family directory holding four quants therefore serves
one of them by filesystem luck — witnessed live: the router exposed only
Xing4.0's Q6_K. So: **one directory per quant**, named ``<Base>-<QUANT>``.
The owner's ``architecture/quantizer/base`` intent survives as lexical
grouping (quant dirs of one base sort adjacent) and as index metadata
(``arch``); the router forbids encoding them as path depth.

**Sidecars.** llama.cpp recognises a vision projector as any filename
containing ``mmproj`` and a draft as the prefixes ``mtp-``/``dspark-``/
``dflash-`` (``common/preset.cpp:370``). A file named ``draft-*.gguf`` is NOT
a sidecar to the router — it is a main-model candidate and can hijack the
directory's slot — so this tool renames ``draft-`` to ``mtp-`` and records it.
Sidecars must sit in the same directory as the model file to be attached.

**Shared files.** The cache and the library live on one filesystem, so a
shared mmproj/draft becomes a *hardlink* in every quant directory: zero extra
bytes, a real regular file to the router's scan, and the inode's link count is
the "who still uses this" ledger — unlinking one name never breaks the others.
That is the owner's "copy while others use it, move when the last user is
gone" expressed by the filesystem.

**Verification law** (owner): nothing is deleted, replaced, or unlinked until
its destination name is staged under a temporary name, size-checked, GGUF-
header-checked, and (for real copies) sha256-verified. When a destination
already exists: validate size + GGUF, then *newer wins* — never assume skip.
One model (its full file set) per operation; preflight validates the whole
plan — free space, paths, integrity, collisions — before a single byte moves.

**Refusals.** A file a live service uses (router-loaded model, any unit's
ExecStart ``--model``/``--mmproj`` — the RAG sidecar is covered here) never
moves. A source that fails integrity (dangling snapshot symlink, missing
zero-byte blob, unparseable header, incomplete shard set — the partial
downloads from metadata testing) is reported and skipped, never migrated.
Nothing is ever deleted recursively, and nothing outside the plan's tracked
names is touched (the §4.10.5 incident where the suite deleted the library
is the reason this file is written the way it is).

References follow the move: personality ``models`` entries and
``model_configs.base_model`` are rewritten from the cache id (``org/repo:TAG``)
to the new library id (the directory name); ``models``/``model_files`` gain
the placement ledger rows; ``modelindex.rescan()`` flips the source badge.
presets.ini is re-rendered by the existing personalities apply pipeline —
this module deliberately never restarts the router.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from . import db, llamakey, model_config, modelindex, modelmeta, paths

# llama.cpp's own sidecar law (common/preset.cpp:370-384, read at b12110 era):
# mmproj is a substring match, draft is a prefix match on exactly these three.
_MMproj_SUB = "mmproj"
_DRAFT_PREFIXES = ("mtp-", "dspark-", "dflash-")
_SHARD_MARK = "-00001-of-"
# A portal-convention draft prefix the router does NOT know; migrated files
# get renamed onto the router's prefix so the draft is seen as a sidecar.
_DRAFT_RENAME_PREFIX = "draft-"

MARGIN_BYTES = 2 * 1024 ** 3          # free-space preflight margin for copies
_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class MigrationError(Exception):
    """A migration that cannot be honoured: refused, never clamped."""


# ------------------------------------------------------------------ naming

def sanitize(name: str) -> str:
    """Filesystem-safe, router-key-safe, human-readable name fragment."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip())
    s = re.sub(r"-{2,}", "-", s).strip("-._")
    return s[:64]


def derive_base(row: dict, filename: str, repo_id: str) -> str:
    """The GGUF metadata fail-over chain, ending in the literal ``Unknown``.

    general.name (sniffed by the index as ``name``) → the filename stem minus
    quant/shard tags → the repo id → ``Unknown``. The owner's law: metadata
    first, filename inference second, an honest Unknown third — never a guess.
    """
    for cand in (row.get("name"),
                 _stem_of(filename, row.get("quant")),
                 repo_id.rsplit("/", 1)[-1] if "/" in repo_id else repo_id):
        s = sanitize(str(cand or ""))
        # A stem that IS the quant tag ("Q4_K_M", "IQ3_S": llama.cpp's tag law
        # upper-cases them, so all-caps-with-a-digit names no model) is the
        # chain speaking with an empty mouth — the next link answers. A pure
        # hex name (40-char SHA fingerprints live in some GGUFs' general.name
        # — witnessed on Jackrong/Qwopus3.5-4B-Coder) is metadata noise for
        # the same reason: a human cannot read it back as a model.
        if s and not re.fullmatch(r"[A-Z0-9_]*\d[A-Z0-9_]*", s) \
                and not re.fullmatch(r"[0-9a-fA-F]{16,}", s):
            return s
    return "Unknown"


def _stem_of(filename: str, quant: str | None) -> str:
    stem = re.sub(r"\.gguf$", "", filename, flags=re.I)
    stem = re.sub(r"[-.]\d{5}-of-\d{5}$", "", stem)   # shard marker first
    if quant:
        stem = re.sub(r"[-.]?" + re.escape(str(quant)) + r"$", "", stem,
                      flags=re.I)
    # and the generic llama.cpp tag law: a trailing dash/dot token containing
    # a digit is a quantization (Q4_K_M, IQ3_S, 2BPW), a quant-free tail is
    # part of the name.
    stem = re.sub(r"[-.](?=[A-Za-z0-9_]*\d)[A-Za-z0-9_]+$", "", stem)
    return stem


def quant_label(row: dict, filename: str) -> str:
    """Quantizer label with the same fail-over chain: index quant (filename
    tag and header, reconciled at scan) → filename TAG → literal Unknown."""
    q = sanitize(str(row.get("quant") or ""))
    if q:
        return q
    _base, _p, _t, tag = modelindex._hf_split(filename)
    return sanitize(tag or "") or "Unknown"


def dir_name_for(repo_id: str, row: dict, filename: str,
                 taken: set[str]) -> str:
    """Collision chain over the taken-id census (router_key-normalised, since
    two spellings that collapse under ``router_key`` are one model to the
    dashboard's live indicator): ``Base-QUANT`` → ``org-Base-QUANT`` →
    ``Base-QUANT-<hash6>``. Deterministic: the same input always names the
    same directory, so a re-plan after a crash reuses the right name."""
    org = repo_id.rsplit("/", 1)[0] if "/" in repo_id else ""
    base = derive_base(row, filename, repo_id)
    quant = quant_label(row, filename)
    seed = hashlib.sha1(f"{repo_id}|{row.get('id')}|{filename}".encode()
                        ).hexdigest()[:6]
    for cand in (sanitize(f"{base}-{quant}"),
                 sanitize(f"{org}-{base}-{quant}") if org else "",
                 sanitize(f"{base}-{quant}-{seed}")):
        if cand and modelindex.router_key(cand) not in taken:
            return cand
    raise MigrationError(f"no collision-free directory name for {row.get('id')}")


# ------------------------------------------------------------------- roles

def file_role(name: str) -> str:
    """The router's own role classification (see module docstring)."""
    low = name.lower()
    if _MMproj_SUB in low:
        return "mmproj"
    if low.startswith(_DRAFT_PREFIXES):
        return "draft"
    if low.startswith(_DRAFT_RENAME_PREFIX):
        return "draft-rename"
    return "model"


def migrate_name(name: str, role: str) -> str:
    """The name the file gets in the library: source names are kept (they
    carry quant and shard markers the router parses) except the ``draft-``
    prefix, which the router cannot see as a sidecar."""
    if role == "draft-rename":
        return "mtp-" + name[len(_DRAFT_RENAME_PREFIX):]
    return name


def _resolve_src(p: Path) -> Path:
    """Snapshot entries are symlinks into blobs/; verification runs on the
    real file, and so do the inode rules for shared names."""
    try:
        return p.resolve(strict=True)
    except OSError:
        return p


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------- plans

def _cache_rows() -> list[dict]:
    return [m for m in modelindex.rows() if m.get("source") == "cache"]


def usage_guard() -> dict[str, str]:
    """Path → user, for every file a live service is using.

    Two sources, both measured at apply time: the router's own model list
    (a loaded worker carries ``--model``/``--mmproj`` in its spawn args) and
    every installed user unit's ExecStart (the RAG embed sidecar names its
    model there; so does the router wrapper). Migration refuses these files —
    "don't move the ground a running process stands on" is the whole rule.
    """
    used: dict[str, str] = {}
    import httpx
    from . import serviceconf
    try:
        url = f"{serviceconf.base_url('llamacpp', 8080)}/v1/models"
        r = httpx.get(url, timeout=5, headers=llamakey.request_headers())
        for m in (r.json().get("data") or []):
            if (m.get("status") or {}).get("value") not in ("loaded", "starting"):
                continue
            # A cache model is spawned with --hf-repo, not --model: the file
            # path never appears in the args (measured on the live router),
            # so the id is the binding — and it is exactly the key a plan
            # is written under. Library-dir workers carry --model paths.
            used[f"id:{m.get('id')}"] = "router model (loaded)"
            args = (m.get("status") or {}).get("args") or []
            for i, a in enumerate(args):
                if a in ("--model", "--mmproj", "-m") and i + 1 < len(args):
                    used[str(_resolve_src(Path(args[i + 1])))] = \
                        f"router model {m.get('id')}"
    except Exception:
        pass    # router unreachable: the ExecStart pass below still guards
    units = Path.home() / ".config/systemd/user"
    if units.is_dir():
        for u in sorted(units.glob("*.service")):
            try:
                text = u.read_text(errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if not line.strip().startswith("ExecStart"):
                    continue
                for tok in line.replace("\\", " ").split():
                    if tok.endswith(".gguf") and Path(tok).is_absolute():
                        used[str(_resolve_src(Path(tok)))] = f"{u.name} ExecStart"
    return used


def _problem_paths(problems: list[dict]) -> set[str]:
    out = set()
    for p in problems:
        if p.get("path"):
            out.add(str(p["path"]))
    return out


def plan_for(repo_id: str, tag: str | None = None) -> dict:
    """One plan for one cache model (one quant = one directory).

    ``files`` carries every op: role, source path (resolved), final name,
    staged name, mode (``link`` on one filesystem, ``copy`` across devices),
    and the exists-rule flag. The plan is inert until preflight and apply.
    """
    rows = _cache_rows()
    row = None
    for m in rows:
        mid = str(m["id"])
        _repo, _, t = mid.rpartition(":")
        if _repo == repo_id and (tag is None or t.upper() == tag.upper()):
            row = m
            break
    if row is None:
        raise MigrationError(f"no cache model {repo_id}"
                             + (f":{tag}" if tag else "") + " in the index")
    src = Path(row["path"] or "")
    if not src.is_file() and not src.is_symlink():
        raise MigrationError(f"{row['id']}: snapshot file missing: {src}")
    snap_dir = src.parent
    # The repo dir is the models--* ancestor — NOT parent.parent: nested
    # (per-quant subdir) layouts sit one level deeper, and a snapshots/
    # "repo_dir" silently skips both blob-release loops (witnessed in tests).
    repo_dir = next((a for a in snap_dir.parents
                     if a.name.startswith("models--")),
                    snap_dir.parent.parent)
    siblings = [p for p in sorted(snap_dir.glob("*.gguf"))]
    # Multi-quant GGUF repos nest each quant in its own snapshot SUBDIR and
    # park the sidecars at the snapshot root (witnessed: ISTA Coder IQ1_M —
    # the projector sat one level above the shards, and a plan blind to it
    # would ship a vision model with no eyes). Sidecar candidates therefore
    # include the snapshot root — ONLY for nested layouts: a flat repo's
    # parent is snapshots/, and scanning it would pull in OTHER revisions
    # (refs-named revisions are the scanned set; leftovers of older ones are
    # not models, test_modelindex pins that law).
    if snap_dir.parent.name == "snapshots":
        pass  # flat: snap_dir IS the snapshot root; its own glob covered it
    else:
        seen_ino = {p.stat().st_ino for p in siblings if p.exists()}
        for p in sorted(snap_dir.parent.glob("*.gguf")):
            try:
                if p.stat().st_ino not in seen_ino:
                    siblings.append(p)
                    seen_ino.add(p.stat().st_ino)
            except OSError:
                continue
    mains = [p for p in siblings
             if file_role(p.name) == "model"
             and _shard_base(p.name) == _shard_base(src.name)]
    if src not in mains:
        mains = [src]
    used = usage_guard()
    if f"id:{row['id']}" in used:
        raise MigrationError(
            f"{row['id']} is loaded on the router — its worker holds the file; "
            "unload it (or restart after a llama.cpp update) before migrating")
    paths.LLAMA_MODELS.mkdir(parents=True, exist_ok=True)
    taken = {modelindex.router_key(d.name)
             for d in paths.LLAMA_MODELS.glob("*") if d.is_dir()}
    filename = src.name
    planned = {migrate_name(p.name, file_role(p.name))
               for p in mains + [p for p in siblings
                                 if file_role(p.name) != "model"]}
    primary = sanitize(f"{derive_base(row, filename, repo_id)}"
                       f"-{quant_label(row, filename)}")
    residue = paths.LLAMA_MODELS / primary
    if (residue.is_dir() and not residue.is_symlink()
            and planned & {q.name for q in residue.iterdir()}):
        # A crashed apply's own directory: names from THIS model's file set
        # live there. The exists-rule operates on those files — the plan
        # reuses the directory. A different model wearing the name shares no
        # file names, and takes the collision chain instead.
        dest_name, dest = primary, residue
    else:
        dest_name = dir_name_for(repo_id, row, filename, taken)
        dest = paths.LLAMA_MODELS / dest_name

    dev_lib = os.stat(paths.LLAMA_MODELS).st_dev
    files: list[dict] = []
    problems: list[str] = []
    for p in mains:
        role = file_role(p.name)
        files.append(_file_op(p, role, dest, dev_lib, used, problems))
    # Sidecars are the repo's: every quant directory of the repo receives a
    # link to the same blob inode — zero bytes, router-visible, nlink-led.
    for p in siblings:
        role = file_role(p.name)
        if role != "model":
            files.append(_file_op(p, role, dest, dev_lib, used, problems))

    for f in files:
        if f["problem"]:
            problems.append(f["problem"])
    return {"repo": repo_id, "id": str(row["id"]), "quant": row.get("quant"),
            "dest": dest, "dest_name": dest_name, "files": files,
            "snapshot_dir": snap_dir, "repo_dir": repo_dir,
            "problems": problems, "bytes": int(row.get("bytes") or 0)}


def _shard_base(name: str) -> str:
    return re.sub(r"\d{5}-of-\d{5}", "", name)


def _file_op(p: Path, role: str, dest: Path, dev_lib: int,
             used: dict[str, str], problems: list[str]) -> dict:
    src = _resolve_src(p)
    final = migrate_name(p.name, role)
    op = {"src": str(src), "link_src": str(p), "name": final,
          "role": role, "size": src.stat().st_size if src.exists() else -1,
          "mode": "copy", "problem": "", "staged": "", "exists": None,
          "bytes_needed": 0}
    if not src.is_file():
        op["problem"] = f"{p.name}: source unresolvable (dangling snapshot link)"
        return op
    if op["size"] <= 0:
        op["problem"] = f"{p.name}: zero-byte blob — partial download"
        return op
    if not modelmeta.sniff_gguf(src):
        op["problem"] = f"{p.name}: header does not parse as GGUF"
        return op
    if str(src) in used:
        op["problem"] = f"{p.name}: in use by {used[str(src)]}"
        return op
    if not _DIR_RE.match(final) or "/" in final:
        op["problem"] = f"{p.name}: unsafe destination file name"
        return op
    op["mode"] = "link" if os.stat(src).st_dev == dev_lib else "copy"
    op["bytes_needed"] = 0 if op["mode"] == "link" else op["size"]
    if role in ("mmproj", "draft", "draft-rename"):
        op["dedupe"] = _dedupe_target(src, op["size"], dest)
    dst = dest / final
    if dst.exists():
        op["exists"] = _exists_check(src, dst)
    return op


def _dedupe_target(src: Path, size: int, dest: Path) -> str:
    """An identical sidecar already lives in the library: link to THAT inode
    so one projector exists on disk for every quant directory (and repo)
    that shares it — the Coder's mmproj IS the Flash-Next projector (owner,
    6.14.1zk: the coder is Flash-Next with experts removed). Content is
    sha256-verified before a match is trusted; sizes are compared first."""
    try:
        src_h = None
        for cand in sorted(paths.LLAMA_MODELS.glob("*/*.gguf")):
            if cand.parent == dest:
                continue
            try:
                if cand.stat().st_size != size:
                    continue
            except OSError:
                continue
            if src_h is None:
                src_h = _sha256(src)
            if _sha256(cand) == src_h:
                return str(cand)
    except OSError:
        pass
    return ""


def _exists_check(src: Path, dst: Path) -> str:
    """The owner's rule for a file that already exists: size, GGUF, newer
    wins. Never assume skip."""
    try:
        same_size = dst.stat().st_size == src.stat().st_size
        valid = bool(modelmeta.sniff_gguf(dst))
        if same_size and valid:
            return "keep" if dst.stat().st_mtime >= src.stat().st_mtime \
                else "replace"
        return "replace"
    except OSError:
        return "replace"


def preflight(plan: dict) -> list[str]:
    """Everything that must be true before a byte moves."""
    problems = list(plan["problems"])
    if not _DIR_RE.match(plan["dest_name"]):
        problems.append(f"unsafe directory name: {plan['dest_name']}")
    root = paths.LLAMA_MODELS.resolve()
    if root not in plan["dest"].resolve().parents:
        problems.append(f"destination escapes the library: {plan['dest']}")
    if plan["dest"].exists() and plan["dest"].is_symlink():
        problems.append(f"destination is a symlink: {plan['dest']}")
    if plan["dest"].exists() and not plan["dest"].is_dir():
        problems.append(f"destination path is not a directory: {plan['dest']}")
    need = sum(f["bytes_needed"] for f in plan["files"])
    free = shutil.disk_usage(paths.LLAMA_MODELS).free
    if need and free < need + MARGIN_BYTES:
        problems.append(f"free space {free // (2**20)} MiB < need "
                        f"{need // (2**20)} MiB + {MARGIN_BYTES // (2**20)} "
                        "MiB margin")
    shard = [f for f in plan["files"] if _SHARD_MARK in f["name"]]
    if shard and not any(f["name"].find(_SHARD_MARK) and
                         re.search(r"00001-of-\d{5}", f["name"])
                         for f in shard):
        problems.append("split model without its part-1 file")
    return problems


# ------------------------------------------------------------------- apply

def apply(plan: dict, log=print) -> dict:
    """Execute one model's plan: stage → verify → land → rewrite → rescan →
    only then unlink cache names. Any unexpected error rolls back: staged
    names are deleted, replaced files are restored, the cache is untouched
    (it is never modified before every verification passed)."""
    problems = preflight(plan)
    if problems:
        raise MigrationError("preflight refused: " + "; ".join(problems))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest: Path = plan["dest"]
    dest.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or not dest.is_dir():
        raise MigrationError(f"destination is not a real directory: {dest}")
    staged: list[Path] = []          # tool's own temp names — the rollback set
    landed: list[Path] = []          # final names this apply created
    replaced: list[tuple[Path, Path]] = []   # (old .mig-old name, final name)
    try:
        for f in plan["files"]:
            tmp = dest / f".mig-{stamp}-{f['name']}"
            _stage(f, tmp, log)
            staged.append(tmp)
        for f in plan["files"]:
            tmp = dest / f".mig-{stamp}-{f['name']}"
            final = dest / f["name"]
            _land(f, tmp, final, stamp, replaced, landed, log)
        plan["ledger"] = _write_ledger(plan, dest)
        plan["refs"] = _rewrite_refs(plan["id"], plan["dest_name"], log)
        modelindex.rescan()
        _drop_cache(plan, log)
        # Rescan AFTER the drop (witnessed live): the first rescan ran while
        # the cache links existed, so it re-listed the cache row; with the
        # links now gone, only a second scan prunes it. Skip this and the
        # models page keeps a ghost "hub cache" row whose Migrate button
        # confirms, fails (snapshot file missing), and stays — measured.
        modelindex.rescan()
        plan["ok"] = True
        db.event("portal", "model-migrate",
                 f"migrated {plan['id']} -> {plan['dest_name']}/ "
                 f"({len(landed)} file(s), "
                 f"{sum(1 for f in plan['files'] if f['mode'] == 'link')} hardlinked)")
        return plan
    except Exception:
        for p in staged:
            try:
                p.unlink()
            except OSError:
                pass
        for old, fin in replaced:
            try:
                if not fin.exists() and old.exists():
                    os.replace(old, fin)
            except OSError:
                log(f"rollback WARNING: could not restore {fin}")
        raise


def _stage(f: dict, tmp: Path, log) -> None:
    src = Path(f["src"])
    if f["mode"] == "link":
        try:
            os.link(src, tmp)
        except OSError as e:
            # st_dev said same-filesystem; a real EXDEV (btrfs subvol, quota)
            # degrades to copy + full hash verify, never to a half measure.
            if e.errno != 18:  # EXDEV
                raise
            f["mode"] = "copy"
            shutil.copy2(src, tmp)
    else:
        shutil.copy2(src, tmp)
    st = tmp.stat()
    if st.st_size != f["size"]:
        raise MigrationError(f"verify: {f['name']} staged size "
                             f"{st.st_size} != {f['size']}")
    if not modelmeta.sniff_gguf(tmp):
        raise MigrationError(f"verify: {f['name']} staged GGUF header rejected")
    if f["mode"] == "copy":
        if _sha256(tmp) != _sha256(src):
            raise MigrationError(f"verify: {f['name']} sha256 mismatch")
        log(f"staged {f['name']} (copy, sha256 verified)")
    else:
        anchor = Path(f.get("dedupe") or f["src"])
        a = anchor.stat()
        if (st.st_ino, st.st_dev) != (a.st_ino, a.st_dev):
            raise MigrationError(f"verify: {f['name']} link inode identity lost")
        if f.get("dedupe"):
            log(f"staged {f['name']} (hardlink to the library's identical "
                "copy — sha256-verified)")
        else:
            log(f"staged {f['name']} (hardlink, inode verified)")


def _land(f: dict, tmp: Path, final: Path, stamp: str,
          replaced: list, landed: list, log) -> None:
    if final.exists():
        if f.get("exists") == "keep":
            tmp.unlink()
            log(f"{f['name']} already present, valid, newer — kept, "
                f"staged name dropped")
            return
        old = final.parent / f".mig-old-{stamp}-{f['name']}"
        os.replace(final, old)
        replaced.append((old, final))
    os.replace(tmp, final)
    landed.append(final)
    log(f"landed {final.name}")


def _write_ledger(plan: dict, dest: Path) -> list:
    files = []
    for f in plan["files"]:
        p = dest / f["name"]
        role = "gguf" if f["role"] in ("model", "draft", "draft-rename") \
            and not f["name"].startswith(("mtp-", "mmproj")) else \
            ("mmproj" if f["role"] == "mmproj" else "draft")
        files.append({"name": f["name"], "size": f["size"],
                      "sha256": _sha256(p) if f["size"] < 4 * 2**30 else None,
                      "role": role, "meta": None})
    db.index_model(plan["dest_name"], dest, "llama", files)
    return files


def _rewrite_refs(cache_id: str, new_id: str, log) -> dict:
    """Personalities and model configs that named the cache id now name the
    library directory. The rewrite is surgical: every other entry is left
    byte-identical. presets.ini is NOT touched here — the personalities page
    re-applies it (validate/snapshot/verify/restart), and the health probe
    reports the stale section until it does."""
    rewrote: list[str] = []
    conn = db._conn()
    try:
        for row in conn.execute(
                "SELECT name, models FROM personalities WHERE models IS NOT NULL"):
            ids = json.loads(row["models"])
            if cache_id not in ids:
                continue
            ids = [new_id if x == cache_id else x for x in ids]
            conn.execute("UPDATE personalities SET models = ?, updated_at = ? "
                         "WHERE name = ?",
                         (json.dumps(ids), time.time(), row["name"]))
            rewrote.append(f"personality {row['name']}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # A config follows its model across the move. model_config.save() REFUSES
    # retargets by design (a config must not wander between models on a user
    # click) — a completed migration is the one event that legitimately moves
    # the base, and the config keeps its name, settings, and assignments.
    moved = [(c["name"], new_id) for c in model_config.rows()
             if c.get("base_model") == cache_id]
    for name, base in moved:
        conn.execute("UPDATE model_configs SET base_model = ?, updated_at = ? "
                     "WHERE name = ?", (base, time.time(), name))
        rewrote.append(f"model config {name}")
    if rewrote:
        log("references rewritten: " + ", ".join(rewrote)
            + " — re-apply the personalities page so presets.ini follows")
    return {"rewrote": rewrote}


def _drop_cache(plan: dict, log) -> None:
    """Unlink the plan's cache names — the only destructive step, and it
    runs after every destination verified. Scope is the plan, never the
    directory: a sidecar snapshot link still serving a quant that has NOT
    migrated stays (the owner's shared-file law: others use it → it does not
    move), and a blob survives while any OTHER snapshot link resolves to its
    inode — the last-user rule, read from the filesystem."""
    hub = modelindex.hub_dir().resolve()
    snap_dir = Path(plan["snapshot_dir"])
    # "who still uses this" means OTHER quants: the plan's own id is gone by
    # definition, and counting it kept IQ1_M's root projector linked to an
    # empty repo (the owner's coder note: the Coder IS the repo's only
    # quant, and its mmproj is the base Flash-Next projector — the library
    # copy is the survivor).
    remaining = [m for m in modelindex.rows()
                 if m.get("source") == "cache"
                 and snap_dir in Path(m["path"] or "").parents
                 and str(m["id"]) != str(plan["id"])]
    removed: list[tuple[int, int]] = []
    for p in sorted({Path(f["link_src"]) for f in plan["files"]}):
        if not p.is_symlink():
            continue
        if file_role(p.name) != "model" and remaining:
            log(f"cache kept: {p.name} still serves {len(remaining)} "
                "cached quant(s) of this repo")
            continue
        try:
            st = _resolve_src(p).stat()
            p.unlink()
            removed.append((st.st_dev, st.st_ino))
            log(f"cache: snapshot link removed for {p.name}")
        except OSError as e:
            log(f"cache left intact for {p.name}: {e}")
    survivors = _snapshot_inos(hub)
    blobs = Path(plan["repo_dir"]) / "blobs"
    for dev, ino in removed:
        if (dev, ino) in survivors:
            log("cache blob kept: other snapshots/revs use the same blob")
            continue
        if not blobs.is_dir():
            continue
        for b in blobs.glob("*"):
            try:
                bs = b.stat()
            except OSError:
                continue
            if b.is_file() and (bs.st_dev, bs.st_ino) == (dev, ino):
                b.unlink()
                log(f"cache: blob {b.name} released")
    # Stranded release: a landed library name sharing a blob whose snapshot
    # links are ALL gone. The blob name is cache litter — unlinking it frees
    # nothing (the bytes live in our name) and completes the move. Witnessed
    # on IQ1_M: shards migrated, blob names stranded. THE TRAP (measured):
    # snapshot links are SYMLINKS — they do not appear in st_nlink, so
    # nlink==2 cannot tell "blob + our name" from "blob + our name + a live
    # symlink pointing here by path". The survivor scan is the gate.
    survivors = _snapshot_inos(hub)
    dest = Path(plan["dest"])
    if blobs.is_dir():
        for f in plan["files"]:
            lp = dest / f["name"]
            try:
                ls = lp.stat()
            except OSError:
                continue
            if ls.st_nlink != 2 or (ls.st_dev, ls.st_ino) in survivors:
                continue        # a name (or a symlink's target) remains
            for b in blobs.glob("*"):
                try:
                    bs = b.stat()
                except OSError:
                    continue
                if b.is_file() and (bs.st_dev, bs.st_ino) == (ls.st_dev, ls.st_ino):
                    b.unlink()
                    log(f"cache: stranded blob released for {lp.name}")


def _snapshot_inos(hub: Path) -> set[tuple[int, int]]:
    """(dev, ino) of every blob every snapshot link resolves to, cache-wide.
    Two depths: flat repos name files at the snapshot root, multi-quant repos
    nest them in per-quant subdirs (both layouts live in this hub)."""
    out: set[tuple[int, int]] = set()
    if not hub.is_dir():
        return out
    for repo in hub.glob("models--*"):
        snaps = repo / "snapshots"
        links = list(snaps.glob("*/*.gguf")) + list(snaps.glob("*/*/*.gguf"))
        for link in links:
            if link.is_symlink():
                try:
                    st = _resolve_src(link).stat()
                    out.add((st.st_dev, st.st_ino))
                except OSError:
                    continue
    return out


# ----------------------------------------------------------- portal bridge

def migratable() -> dict[str, dict]:
    """Per cache row: can the Migrate button be shown, and where would it
    land? Cheap by design (index rows + one router query + unit files) so the
    models page can ask it on every build; the plan's deep integrity checks
    run again, untouched, at apply time."""
    out: dict[str, dict] = {}
    problems_by_path = _problem_paths(
        modelindex.scan_cache()[1])
    paths.LLAMA_MODELS.mkdir(parents=True, exist_ok=True)
    taken = {modelindex.router_key(d.name)
             for d in paths.LLAMA_MODELS.glob("*") if d.is_dir()}
    used = usage_guard()
    for row in _cache_rows():
        mid = str(row["id"])
        repo, _, tag = mid.rpartition(":")
        entry = {"ok": False, "dir": "", "reason": "", "bytes": row.get("bytes")}
        try:
            src = Path(row["path"] or "")
            snap_dir = src.parent
            mine = {str(x) for x in (str(src),) } | {
                str(_resolve_src(p))
                for p in snap_dir.glob("*.gguf")
                if _shard_base(p.name) == _shard_base(src.name)}
            bad = mine & problems_by_path
            if f"id:{mid}" in used:
                entry["reason"] = "loaded on the router — its worker holds the file"
            elif not src.is_file():
                # A cache row whose file is gone is a ghost (a completed
                # migration's stale index row): no button, honest reason.
                # The page self-heals on the next rescan; a click here can
                # only ever fail.
                entry["reason"] = "snapshot file missing (stale index row?)"
            elif bad:
                entry["reason"] = "source integrity problem (partial download?)"
            elif str(_resolve_src(src)) in used:
                entry["reason"] = f"in use by {used[str(_resolve_src(src))]}"
            else:
                d = dir_name_for(repo, row, src.name, taken)
                taken.add(modelindex.router_key(d))
                entry.update(ok=True, dir=d)
        except Exception as e:            # a refusal to show is never a crash
            entry["reason"] = str(e)
        out[mid] = entry
    return out
