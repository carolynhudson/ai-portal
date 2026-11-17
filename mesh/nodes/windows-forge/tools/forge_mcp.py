import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter

# forge_mcp.py : image-forge MCP server (RainbowBlock, 2080 Ti).
# Thin over ComfyUI HTTP (127.0.0.1:8188) + kit tool logic, per the approved
# workroom proposal. MCP streamable-HTTP: POST /mcp JSON-RPC 2.0 (initialize,
# tools/list, tools/call); GET /health; GET /files/<relpath under kit out> for
# portal pickup of composite/ladder artifacts.
# SECURITY: NO authentication, like ComfyUI. The firewall gates this port to
# the portal IP ONLY (firewall_forge_mcp.ps1, rule 'ai-forge forge-mcp (portal
# only)'). Bind 0.0.0.0; local callers (tests) use 127.0.0.1.
# LAW (from the field report): validate every input before submit; surface
# engine exception payloads; on timeout interrupt + purge the queue; verifiers
# return MEASURED values, not bare booleans; renders are serialized (VRAM
# contract: --disable-smart-memory, one model resident).

API = "http://127.0.0.1:8188"
PROSE = ("127.0.0.1", 8081)   # RETIRED 2026-11-12: router is the only llama surface
AGENT = ("127.0.0.1", 24096)
ROUTER = "http://127.0.0.1:8082"
KIT = r"C:\forge\kit"
WORKFLOWS = os.path.join(KIT, "workflows")
OUT = os.path.join(KIT, "out", "mcp")
PLATE = (255, 0, 255)
LADDER = [512, 180, 48, 32, 16]
PORT = 8096
PROTOCOL = "2025-03-26"

# SUPERVISOR (personal-PC law 2026-11-12): the GPU is the user's, not ours.
# After COMFY_IDLE_S of no renders with an empty queue, POST /free unloads
# the diffusion models (keeps the engine process; model RAM/VRAM returns to
# the user). The llama router self-manages via --sleep-idle-seconds and
# --models-memory-margin (native llama.cpp idle law). Config: supervisor.json
# in the kit (comfy_idle_s, check_s) - test windows without restarts.
COMFY_IDLE_S = 1200
CHECK_S = 60
ACT = {"last_render": 0.0, "last_free": 0.0, "frees": 0,
       "last_free_result": None}


def load_supervisor_config():
    global COMFY_IDLE_S, CHECK_S
    try:
        cfg = json.load(open(os.path.join(KIT, "supervisor.json"), encoding="ascii"))
        COMFY_IDLE_S = float(cfg.get("comfy_idle_s", COMFY_IDLE_S))
        CHECK_S = float(cfg.get("check_s", CHECK_S))
    except FileNotFoundError:
        pass


# MESH LEASES (meshd phase 2b, 2026-11-15): a granted mesh_lease is a VRAM
# reservation ON THIS NODE. The forge yields: with leases committed and an
# empty queue, POST /free NOW - do not wait for COMFY_IDLE_S. Ledger is
# meshd's (C:\forge\etc\mesh_leases.json); we read it, prune TTL on read,
# never write it (single writer law).
LEASES_PATH = r"C:\forge\etc\mesh_leases.json"


def leased_vram_mib():
    try:
        d = json.load(open(LEASES_PATH, encoding="ascii"))
        now = int(time.time())
        return int(sum(int(l.get("vram_mib", 0)) for l in d.get("leases", [])
                       if int(l.get("expires_unix", 0)) > now))
    except Exception:
        return 0


def supervisor_loop():
    while True:
        time.sleep(CHECK_S)
        try:
            committed = leased_vram_mib()
            idle = time.time() - ACT["last_render"] if ACT["last_render"] else None
            busy = ACT["last_render"] > ACT["last_free"]
            q = requests.get(API + "/queue", timeout=10).json()
            queued = q.get("queue_running") or q.get("queue_pending")
            if committed > 0 and busy and not queued:
                reason = "lease %d MiB committed, queue empty - yield VRAM" % committed
            else:
                if idle is None or not busy or idle < COMFY_IDLE_S:
                    continue
                if queued:
                    continue
                reason = "%d s idle" % int(idle)
            r = requests.post(API + "/free",
                              json={"unload_models": True, "free_memory": True},
                              timeout=60)
            ACT["last_free"] = time.time()
            ACT["frees"] += 1
            ACT["last_free_result"] = "HTTP %d (%s)" % (r.status_code, reason)
            sys.stdout.write("supervisor: /free " + ACT["last_free_result"] + "\n")
            sys.stdout.flush()
        except Exception as e:
            ACT["last_free_result"] = "error " + repr(e)[:200]

RENDER_LOCK = threading.Lock()
NAME_RE = re.compile(r"^[A-Za-z0-9_\-/.]+$")
HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------- helpers ----------------------------------------------------------

def out_json(handler, payload, status=200):
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def tcp_open(addr, timeout=1.5):
    try:
        with socket.create_connection(addr, timeout=timeout):
            return True
    except OSError:
        return False


def pinkish(a):
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
    return (r > 150) & (b > 90) & (g < r - 70) & (sat > 0.45)


def magenta_adj(a):
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return (r > 150) & (b > 150) & (g < r - 60) & (g < b - 60)


def near(c, t, d=60):
    return all(abs(int(x) - int(y)) <= d for x, y in zip(c, t))


def sharpness(im):
    g = np.asarray(im.convert("L"), dtype=float)
    h, w = g.shape
    g = g[h // 4:h - h // 4, w // 4:w - w // 4]
    lap = (-g[1:-1, 1:-1] * 4 + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
    return float(lap.var())


def fetch_image(filename, subfolder="", type_="output"):
    r = requests.get(API + "/view", params={"filename": filename, "subfolder": subfolder,
                                            "type": type_}, timeout=60)
    r.raise_for_status()
    import io
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def save_out(im, name):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    im.save(p)
    return p, os.path.getsize(p)


def resolve_source(name, subfolder):
    """Accept a ComfyUI output file (via /view) or a kit-out relative path."""
    if name.startswith("kitout/"):
        rel = name[len("kitout/"):]
        p = os.path.realpath(os.path.join(OUT, rel))
        if not p.startswith(os.path.realpath(OUT) + os.sep):
            raise ValueError("kitout path escapes the out dir: " + rel)
        return Image.open(p).convert("RGB"), p
    if not NAME_RE.match(name) or ".." in name:
        raise ValueError("bad filename: " + name)
    return fetch_image(name, subfolder or ""), None


# ---------- tools ------------------------------------------------------------

# ADVERTISEMENT (mesh services law 2026-11-12): a node's forge reports its
# services the way the llama router reports models - per-contract id, tier,
# fills, models, and the hardware the box actually has. The meshd controller
# and portal read THIS to decide placement; a node with no GPU simply
# advertises fewer/zero image contracts. Cached by workflows-dir mtime.
_CONTRACT_CACHE = {"key": None, "list": None}


def contract_index():
    key = os.path.getmtime(WORKFLOWS)
    if _CONTRACT_CACHE["key"] == key and _CONTRACT_CACHE["list"] is not None:
        return _CONTRACT_CACHE["list"]
    out = []
    for f in sorted(os.listdir(WORKFLOWS)):
        if not f.endswith(".json"):
            continue
        try:
            g = json.load(open(os.path.join(WORKFLOWS, f), encoding="utf-8"))
            meta = g.get("_meta", {})
            out.append({"name": f[:-5],
                        "tier": meta.get("tier"),
                        "models": meta.get("models", []),
                        "fills": sorted(fills_map(meta)),
                        "validated": meta.get("validated")})
        except Exception as e:
            out.append({"name": f[:-5], "error": repr(e)[:120]})
    _CONTRACT_CACHE["key"] = key
    _CONTRACT_CACHE["list"] = out
    return out


def advertise():
    return {"kind": "image-forge", "service": "forge-mcp",
            "contracts": contract_index(),
            "limits": {"width_height": "multiples of 32, 256..1536",
                       "concurrency": 1, "timeout_s": "30..1800"},
            "idle_policy": {"comfy_free_after_s": COMFY_IDLE_S,
                             "lease_yield": "meshd leases cut the idle timer: committed VRAM + empty queue -> /free now"}}


def tool_health():
    h = {"engine": None, "queue": None, "prose_8081_tcp": tcp_open(PROSE),
         "router_8082_tcp": tcp_open(("127.0.0.1", 8082)),
         "agent_24096_tcp": tcp_open(AGENT),
         "supervisor": {"comfy_idle_s": COMFY_IDLE_S,
                         "leased_vram_mib": leased_vram_mib(),
                        "idle_since_render_s": (int(time.time() - ACT["last_render"])
                                                if ACT["last_render"] else None),
                        "frees": ACT["frees"], "last_free_result": ACT["last_free_result"]},
         "capabilities": advertise()}
    try:
        s = requests.get(API + "/system_stats", timeout=10).json()
        sysd = s[0] if isinstance(s, list) else s
        devs = [{"name": d.get("name"), "vram_total": d.get("vram_total"),
                 "vram_free": d.get("vram_free")} for d in sysd.get("devices", [])]
        h["engine"] = {"ok": True, "system": sysd.get("system", {}), "devices": devs}
    except Exception as e:
        h["engine"] = {"ok": False, "error": repr(e)[:300]}
    try:
        q = requests.get(API + "/queue", timeout=10).json()
        h["queue"] = {"running": len(q.get("queue_running", [])),
                      "pending": len(q.get("queue_pending", []))}
    except Exception as e:
        h["queue"] = {"ok": False, "error": repr(e)[:300]}
    return h


def load_contract(contract):
    if not NAME_RE.match(contract) or "/" in contract:
        raise ValueError("bad contract name: " + contract)
    path = os.path.join(WORKFLOWS, contract + ".json")
    if not os.path.isfile(path):
        avail = sorted(f[:-5] for f in os.listdir(WORKFLOWS) if f.endswith(".json"))
        raise ValueError("contract not found: %s (available: %s)" % (contract, ", ".join(avail)))
    return json.load(open(path, encoding="utf-8"))


def fills_map(contract_meta):
    """caller_fills entries are 'node.field' plus optional annotation text.
    The REFERENCE is the first whitespace token: '4.prompt (use <image1>
    markers)' declares field 'prompt' on node 4; the parenthetical is docs."""
    fm = {}
    for spec in contract_meta.get("caller_fills", []):
        ref = spec.split()[0]
        if "." not in ref:
            raise ValueError("bad caller_fills spec (want node.field): %r" % (spec,))
        node, field = ref.split(".", 1)
        fm[field] = node
    return fm


def apply_fills(graph, contract_meta, fills):
    fm = fills_map(contract_meta)
    for k, v in fills.items():
        if k not in fm:
            raise ValueError("fill '%s' not declared in contract caller_fills (allowed: %s)"
                             % (k, ", ".join(sorted(fm))))
        node = fm[k]
        if node not in graph:
            raise ValueError("contract declares fill '%s' at node %s, but the graph has "
                             "no node %s (contract bug, not caller bug)" % (k, node, node))
        graph[node]["inputs"][k] = v


def tool_render(a):
    ACT["last_render"] = time.time()
    contract = a.get("contract")
    if not isinstance(contract, str):
        raise ValueError("contract: string required")
    graph0 = load_contract(contract)
    meta = graph0.get("_meta", {})
    timeout_s = a.get("timeout_s", 300)
    if not isinstance(timeout_s, int) or not 30 <= timeout_s <= 1800:
        raise ValueError("timeout_s must be int in 30..1800")
    seeds = a.get("seeds")
    seed_top = a.get("seed")
    fills_in = a.get("fills", {})
    if not isinstance(fills_in, dict):
        raise ValueError("fills: object of contract-declared fields required")
    for k in fills_in:
        if not isinstance(k, str) or not FIELD_RE.match(k):
            raise ValueError("bad fill key: %r" % (k,))
    fmap = fills_map(meta)
    # seed fill name is contract-declared, not hardcoded (interop bug, portal
    # 2026-11-11): t2i contracts use noise_seed, the edit graph seeds its
    # KSampler via seed. Resolve from caller_fills BEFORE validation.
    seed_key = "noise_seed" if "noise_seed" in fmap else ("seed" if "seed" in fmap else None)
    if seeds is None and seed_top is None and seed_key is not None:
        fs = fills_in.get(seed_key)
        if isinstance(fs, int) and not isinstance(fs, bool):
            seeds = [fs]  # seeds passed as the declared fill: honored, not dropped
    if seeds is None:
        seeds = [seed_top]
    if not isinstance(seeds, list) or not seeds or \
            (len(seeds) == 1 and seeds[0] is None):
        raise ValueError("seeds: list of ints required (or seed: int, "
                         "or the contract's seed fill inside fills)")
    for s in seeds:
        if not isinstance(s, int) or isinstance(s, bool) or not 0 <= s <= 2**53:
            raise ValueError("seed must be int 0..2**53, got %r" % (s,))
    if seed_key is None:
        raise ValueError("contract declares no seed fill (caller_fills: %s) - "
                         "cannot render with seeds" % ", ".join(sorted(fmap)))
    w, hgt = a.get("width"), a.get("height")
    for v, nm in ((w, "width"), (hgt, "height")):
        if v is not None and (not isinstance(v, int) or v % 32 or not 256 <= v <= 1536):
            raise ValueError("%s must be int, multiple of 32, 256..1536" % nm)
    prompt = a.get("prompt")
    if prompt is not None and (not isinstance(prompt, str) or len(prompt) > 4000):
        raise ValueError("prompt: string, max 4000 chars")
    prefix = a.get("prefix", "mcp/render")
    if not NAME_RE.match(prefix) or ".." in prefix:
        raise ValueError("bad prefix: " + prefix)

    fills = dict(fills_in)
    if prompt is not None:
        fills["prompt"] = prompt
    if w is not None:
        fills["width"] = w
    if hgt is not None:
        fills["height"] = hgt

    results = []
    with RENDER_LOCK:
        for seed in seeds:
            g = json.loads(json.dumps(graph0))
            g.pop("_meta", None)
            f = dict(fills)
            f[seed_key] = seed
            apply_fills(g, meta, f)
            for nid, node in g.items():
                if node.get("class_type") == "SaveImage":
                    node["inputs"]["filename_prefix"] = "%s_s%d" % (prefix, seed)
            t0 = time.time()
            try:
                r = requests.post(API + "/prompt", json={"prompt": g}, timeout=30)
            except Exception as e:
                results.append({"seed": seed, "ok": False, "stage": "submit",
                                "error": repr(e)[:300]})
                continue
            if r.status_code != 200:
                results.append({"seed": seed, "ok": False, "stage": "submit",
                                "error": r.text[:600]})
                continue
            pid = r.json()["prompt_id"]
            meta_img, err = None, None
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                time.sleep(2)
                try:
                    hh = requests.get(API + "/history/" + pid, timeout=15).json()
                except Exception:
                    continue
                if pid in hh:
                    out = hh[pid]
                    st = out.get("status", {})
                    if st.get("status_str") == "error":
                        err = json.dumps(st)[:1200]
                    for o in out.get("outputs", {}).values():
                        if "images" in o:
                            meta_img = o["images"][0]
                    break
            if meta_img:
                results.append({"seed": seed, "ok": True, "prompt_id": pid,
                                "filename": meta_img["filename"],
                                "subfolder": meta_img.get("subfolder", ""),
                                "type": meta_img.get("type", "output"),
                                "url": "%s/view?filename=%s&subfolder=%s&type=%s"
                                       % (API, meta_img["filename"],
                                          meta_img.get("subfolder", ""),
                                          meta_img.get("type", "output")),
                                "ms": int((time.time() - t0) * 1000)})
            elif err:
                results.append({"seed": seed, "ok": False, "prompt_id": pid,
                                "stage": "generate", "engine_error": err})
            else:
                # recovery: interrupt + purge so the engine is clean for the next job
                try:
                    requests.post(API + "/interrupt", timeout=5)
                    q = requests.get(API + "/queue", timeout=5).json()
                    ids = [str(item[1]) for item in q.get("queue_pending", [])]
                    if ids:
                        requests.post(API + "/queue", json={"delete": ids}, timeout=5)
                except Exception:
                    pass
                results.append({"seed": seed, "ok": False, "prompt_id": pid,
                                "stage": "timeout",
                                "recovery": "interrupted + queue purged",
                                "hint": "fetch later from /history/" + pid})
    ok = all(r["ok"] for r in results)
    return {"ok": ok, "contract": contract, "results": results}


def tool_verify(a):
    name = a.get("filename")
    if not isinstance(name, str):
        raise ValueError("filename required")
    im, _ = resolve_source(name, a.get("subfolder", ""))
    arr = np.asarray(im, dtype=int)
    h, w = arr.shape[:2]
    spec = a.get("spec", {})
    out = {"ok": True, "measured": {}}
    m = out["measured"]
    m["size"] = [w, h]
    corners = [tuple(int(x) for x in arr[2, 2]), tuple(int(x) for x in arr[2, w - 3]),
               tuple(int(x) for x in arr[h - 3, 2]), tuple(int(x) for x in arr[h - 3, w - 3])]
    m["corner_pixels"] = corners
    if spec.get("plate"):
        want = tuple(spec.get("plate_rgb", list(PLATE)))
        m["plate"] = {"expect": list(want),
                      "corners_exact": all(c == want for c in corners),
                      "corner_max_dev": [max(abs(x - y) for x, y in zip(c, want)) for c in corners]}
    pal = spec.get("palette")
    if pal:
        hits = {}
        step = max(1, (arr.shape[0] * arr.shape[1]) // 200000)
        flat = arr.reshape(-1, 3)[::step]
        for hexc in pal:
            if not HEX_RE.match(hexc):
                raise ValueError("palette entry must be 6 hex digits: " + hexc)
            t = tuple(int(hexc[i:i + 2], 16) for i in (0, 2, 4))
            hits[hexc] = any(near(tuple(px), t) for px in flat)
        m["palette_hits"] = hits
    forbid = spec.get("forbid_hue", "magenta")
    if forbid == "magenta":
        if spec.get("plate"):
            # plate files ARE magenta outside the disc: measure the disc INTERIOR
            yy, xx = np.mgrid[0:h, 0:w]
            inside = ((xx - w // 2) ** 2 + (yy - h // 2) ** 2) < (0.93 * min(w, h) // 2) ** 2
            m["interior_pink_pct"] = round(100 * magenta_adj(arr)[inside].mean(), 3)
            m["magenta_adjacent_px"] = int(magenta_adj(arr)[inside].sum())
        else:
            m["magenta_adjacent_px"] = int(magenta_adj(arr).sum())
    if "min_sharpness" in spec:
        s = sharpness(im)
        m["sharpness"] = round(s, 1)
        m["sharpness_ok"] = s >= spec["min_sharpness"]
    hard = []
    if spec.get("plate") and not m["plate"]["corners_exact"]:
        hard.append("plate corners not exact")
    if spec.get("plate") and "interior_pink_pct" in m and m["interior_pink_pct"] > 1.0:
        hard.append("interior pink %.2f pct (disc contaminated)" % m["interior_pink_pct"])
    if pal and not all(m["palette_hits"].values()):
        hard.append("palette misses: " + ",".join(k for k, v in m["palette_hits"].items() if not v))
    if "min_sharpness" in spec and not m["sharpness_ok"]:
        hard.append("sharpness below min")
    out["ok"] = not hard
    if hard:
        out["violations"] = hard
    return out


def composite_scene(im, frac=1.0):
    w, h = im.size
    S = min(w, h)
    im = im.crop(((w - S) // 2, (h - S) // 2, (w - S) // 2 + S, (h - S) // 2 + S))
    AA = 4
    d = int(S * frac) * AA
    mask = Image.new("L", (S * AA, S * AA), 0)
    ImageDraw.Draw(mask).ellipse(((S * AA - d) // 2, (S * AA - d) // 2,
                                  (S * AA + d) // 2, (S * AA + d) // 2), fill=255)
    mask = mask.resize((S, S), Image.LANCZOS)
    plate = Image.new("RGB", (S, S), PLATE)
    plate.paste(im, (0, 0), mask)
    return plate


def tool_plate(a):
    name = a.get("source")
    if not isinstance(name, str):
        raise ValueError("source required (ComfyUI filename or kitout/<rel>)")
    frac = a.get("frac", 1.0)
    if not isinstance(frac, (int, float)) or not 0.3 <= frac <= 1.0:
        raise ValueError("frac must be number 0.3..1.0")
    out_name = a.get("out_name")
    if not isinstance(out_name, str) or not NAME_RE.match(out_name) or ".." in out_name \
            or not out_name.endswith(".png"):
        raise ValueError("out_name: [A-Za-z0-9_.-] ending .png")
    im, _ = resolve_source(name, a.get("subfolder", ""))
    plate = composite_scene(im, frac)
    path, size = save_out(plate, out_name)
    arr = np.asarray(plate, dtype=int)
    S = arr.shape[0]
    corners = [tuple(int(x) for x in arr[2, 2]), tuple(int(x) for x in arr[2, S - 3]),
               tuple(int(x) for x in arr[S - 3, 2]), tuple(int(x) for x in arr[S - 3, S - 3])]
    ok = all(c == PLATE for c in corners)
    res = {"ok": ok, "out_path": path, "bytes": size, "corner_pixels": corners,
           "files_url": "http://RainbowBlock.local:%d/files/%s" % (PORT, out_name)}
    if not ok:
        res["violations"] = ["plate corners not pure by construction"]
    return res


def tool_scale(a):
    name = a.get("source")
    if not isinstance(name, str):
        raise ValueError("source required (ComfyUI filename or kitout/<rel>)")
    ladder = a.get("ladder", LADDER)
    if not isinstance(ladder, list) or not all(isinstance(s, int) and 8 <= s <= 1024 for s in ladder):
        raise ValueError("ladder: list of ints 8..1024")
    im, _ = resolve_source(name, a.get("subfolder", ""))
    base = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.basename(name).rsplit(".", 1)[0])
    files = []
    for s in ladder:
        small = im.resize((s, s), Image.LANCZOS)
        fn = "%s_%d.png" % (base, s)
        p, sz = save_out(small, fn)
        files.append({"px": s, "path": p, "bytes": sz,
                      "files_url": "http://RainbowBlock.local:%d/files/%s" % (PORT, fn)})
    cell, pad = 540, 20
    canvas = Image.new("RGB", (cell * len(ladder) + pad * (len(ladder) + 1), cell + 20), (24, 28, 36))
    x = pad
    for s in ladder:
        small = im.resize((s, s), Image.LANCZOS)
        if s < 180:
            small = small.resize((cell, cell), Image.NEAREST)
        canvas.paste(small, (x, pad))
        x += cell + pad
    mname = "%s_montage.png" % base
    mpath, msz = save_out(canvas, mname)
    return {"ok": True, "files": files,
            "montage": {"path": mpath, "bytes": msz,
                        "files_url": "http://RainbowBlock.local:%d/files/%s" % (PORT, mname)}}


def tool_getfile(a):
    name = a.get("filename")
    if not isinstance(name, str) or not NAME_RE.match(name) or ".." in name:
        raise ValueError("bad filename: %r" % (name,))
    r = requests.get(API + "/view", params={"filename": name,
                                            "subfolder": a.get("subfolder", ""),
                                            "type": a.get("type", "output")}, timeout=60)
    r.raise_for_status()
    path, size = save_out_bytes(name.replace("/", "_"), r.content)
    return {"ok": True, "local_path": path, "bytes": size,
            "lan_url": "%s/view?filename=%s&subfolder=%s&type=%s"
                       % (API, name, a.get("subfolder", ""), a.get("type", "output")),
            "note": "portal can also fetch lan_url directly (8188 rule is portal-IP-only)"}


def save_out_bytes(name, content):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    open(p, "wb").write(content)
    return p, len(content)


# ---------- router (llama.cpp multi-model LLM endpoint, port 8082) ----------
# Build 12211 has NO admin load/unload URLs (measured: /models/{name}/load,
# DELETE .../unload, /load_model all 404). The load API IS a chat request
# with model=<id>: the router loads on demand and evicts LRU beyond
# models-max 1 (measured swaps: ->flint 39.8 s, ->nemotron 8.6-18 s).
# Key from subagent_key.txt, never returned in tool output.

ROUTER = "http://127.0.0.1:8082"
ROUTER_KEYFILE = r"C:\forge\llama\subagent_key.txt"
ROUTER_LOCK = threading.Lock()
ROUTER_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _router_key():
    return open(ROUTER_KEYFILE, encoding="ascii").read().strip()


def _router_models(timeout=30):
    r = requests.get(ROUTER + "/models", timeout=timeout,
                     headers={"Authorization": "Bearer " + _router_key()})
    r.raise_for_status()
    rows = []
    for m in r.json().get("data", []):
        st = m.get("status", {})
        rows.append({"id": m.get("id"),
                     "state": (st.get("value") if isinstance(st, dict) else str(st))})
    return rows


def _free_ram_gb():
    import ctypes

    class MSX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                    ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64)]
    ms = MSX()
    ms.dwLength = ctypes.sizeof(MSX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
    return round(ms.ullAvailPhys / 2**30, 1)


def tool_router_status(a):
    out = {"ok": True, "endpoint": ROUTER,
           "listening": tcp_open(("127.0.0.1", 8082)), "free_ram_gb": _free_ram_gb()}
    try:
        out["models"] = _router_models()
    except Exception as e:
        out["models_error"] = repr(e)[:200]
    return out


def tool_router_switch(a):
    model = a.get("model")
    if not isinstance(model, str) or not ROUTER_NAME_RE.match(model):
        raise ValueError("model: id required ([A-Za-z0-9_-])")
    ids = [m["id"] for m in _router_models()]
    if model not in ids:
        raise ValueError("unknown model id: %s (known: %s)" % (model, ids))
    with ROUTER_LOCK:
        t0 = time.time()
        r = requests.post(ROUTER + "/v1/chat/completions",
                          headers={"Authorization": "Bearer " + _router_key()},
                          json={"model": model,
                                "messages": [{"role": "user",
                                              "content": "Reply with exactly: READY"}],
                                "max_tokens": 64}, timeout=600)
        if r.status_code != 200:
            raise ValueError("switch ping failed: %s %s" % (r.status_code, r.text[:300]))
        ms = int((time.time() - t0) * 1000)
    return {"ok": True, "model": model, "ping_ms": ms, "models": _router_models(),
            "free_ram_gb": _free_ram_gb()}


def tool_router_start(a):
    if tcp_open(("127.0.0.1", 8082)):
        return {"ok": True, "note": "already listening", "models": _router_models()}
    with ROUTER_LOCK:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", os.path.join(KIT, "start_forge_router.ps1")],
                       timeout=60, check=False)
        for _ in range(60):
            if tcp_open(("127.0.0.1", 8082), timeout=3):
                return {"ok": True, "listening": True,
                        "note": "router started; COLD by law (no preload) - chat model=<id> loads on demand, sleeps after 15 min idle",
                        "models": _router_models()}
            time.sleep(2)
    raise ValueError("router not listening 120 s after start; check C:\\forge\\logs\\router-*.log")


def tool_router_stop(a):
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-NetTCPConnection -State Listen -LocalPort 8082 -ErrorAction SilentlyContinue | "
                    "ForEach-Object { Stop-Process -Id ($_.OwningProcess) -Force }"],
                   timeout=60, check=False)
    time.sleep(2)
    return {"ok": not tcp_open(("127.0.0.1", 8082)),
            "note": "router process stopped (RAM freed for renders); router_start tool brings it back; the logon task restarts it at next logon"}


# VRAM OFFLOAD (mesh_lease actor lane, owner 2026-11-17): mesh presets stay
# -ngl 0 by law; -ngl 99 happens ONLY as a lease-backed placement, and the
# admission ticket is checked by meshd (live lease) BEFORE this runs. We edit
# the node's OWN presets.ini (timestamped backup; section-scoped -ngl), warm
# the model on demand (router --models-max 1: the switch IS the unload path),
# and revert to -ngl 0 on release. The token is never printed.
ROUTER_PRESETS = os.environ.get("FORGE_ROUTER_PRESETS") or r"C:\forge\llama\router\presets.ini"


def _presets_set_ngl(model, ngl):
    """Section-scoped -ngl patch of presets.ini; timestamped .bak. Returns
    (changed, backup_path). INI sections are [name]; args are one per line."""
    if not ROUTER_NAME_RE.match(model or ""):
        raise ValueError("model: id required ([A-Za-z0-9_-])")
    if not os.path.isfile(ROUTER_PRESETS):
        raise ValueError("presets file missing: " + ROUTER_PRESETS)
    lines = open(ROUTER_PRESETS, encoding="utf-8").read().splitlines()
    sec = None
    for i, ln in enumerate(lines):
        t = ln.strip()
        if t.startswith("[") and t.endswith("]"):
            if sec is not None:
                break
            if t[1:-1].strip() == model:
                sec = i
    if sec is None:
        raise ValueError("no preset section [%s] in %s" % (model, ROUTER_PRESETS))
    end = len(lines)
    for j in range(sec + 1, len(lines)):
        if lines[j].strip().startswith("["):
            end = j
            break
    changed = False
    for j in range(sec + 1, end):
        t = lines[j].strip()
        if t.startswith("-ngl") or t.startswith("--n-gpu-layers"):
            want = str(ngl)
            cur = t.split()[-1]
            if cur != want:
                indent = lines[j][:len(lines[j]) - len(lines[j].lstrip())]
                lines[j] = indent + t.split()[0] + " " + want
                changed = True
            break
    else:
        lines.insert(sec + 1, "-ngl %d" % ngl)
        changed = True
    bak = ROUTER_PRESETS + ".bak." + str(int(time.time()))
    if changed:
        shutil.copy2(ROUTER_PRESETS, bak)
        open(ROUTER_PRESETS, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return changed, (bak if changed else "")


def tool_router_offload(a):
    model = a.get("model")
    ngl = int(a.get("ngl", 99))
    ngl = max(1, min(99, ngl))
    if not ROUTER_NAME_RE.match(model or ""):
        raise ValueError("model: id required ([A-Za-z0-9_-])")
    ids = [m["id"] for m in _router_models()]
    if model not in ids:
        raise ValueError("unknown model id: %s (known: %s)" % (model, ids))
    changed, bak = _presets_set_ngl(model, ngl)
    t0 = time.time()
    with ROUTER_LOCK:
        r = requests.post(ROUTER + "/v1/chat/completions",
                          headers={"Authorization": "Bearer " + _router_key()},
                          json={"model": model,
                                "messages": [{"role": "user",
                                              "content": "Reply with exactly: READY"}],
                                "max_tokens": 64}, timeout=600)
        if r.status_code != 200:
            raise ValueError("offload warm failed: %s %s" % (r.status_code, r.text[:300]))
        ms = int((time.time() - t0) * 1000)
    return {"ok": True, "model": model, "ngl": ngl, "preset_changed": changed,
            "preset_backup": bak, "warm_ms": ms,
            "models": _router_models(), "free_ram_gb": _free_ram_gb()}


def tool_router_offload_revert(a):
    model = a.get("model")
    changed, bak = _presets_set_ngl(model, 0)
    return {"ok": True, "model": model, "ngl": 0, "preset_changed": changed,
            "preset_backup": bak,
            "note": "preset back to -ngl 0 (mesh law restored); GPU weights "
                    "release at the router idle-sleep (900 s) or next model "
                    "switch; release the meshd lease separately (lease_release)"}


TOOLS = {
    "forge_health": {
        "description": "Engine/queue/VRAM/prose/agent liveness, supervisor state, and "
                       "capabilities (measured values).",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: tool_health()},
    "forge_caps": {
        "description": "Advertise this node's forge services the way the llama router "
                       "reports models: per-contract name/tier/fills/models/validated + "
                       "limits + idle policy. The mesh controller/portal read this for "
                       "placement (capability = what the box can run, measured).",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: advertise()},
    "forge_render": {
        "description": "Render a kit workflow contract (workflows/<name>.json) with validated "
                       "fills: typed prompt/width/height (x32, 256..1536) + seeds[]/seed, and a "
                       "generic 'fills' object for any contract-declared caller_fills field "
                       "(edit: image [upload first via POST /upload/image], negative_prompt, "
                       "resolution). The seed fill is resolved PER CONTRACT (t2i: noise_seed, "
                       "edit: seed) - never pass noise_seed to the edit graph. Serialized on a "
                       "global lock (VRAM contract); polls /history per seed; surfaces engine "
                       "exception payloads; on timeout interrupts + purges queue. Returns "
                       "per-seed filename/subfolder/url/ms.",
        "schema": {"type": "object",
                   "properties": {
                       "contract": {"type": "string"},
                       "prompt": {"type": "string"},
                       "width": {"type": "integer"}, "height": {"type": "integer"},
                       "fills": {"type": "object"},
                       "seeds": {"type": "array", "items": {"type": "integer"}},
                       "seed": {"type": "integer"},
                       "prefix": {"type": "string"},
                       "timeout_s": {"type": "integer"}},
                   "required": ["contract"]},
        "fn": tool_render},
    "forge_verify": {
        "description": "Measure a rendered file (ComfyUI filename/subfolder): corner pixels, "
                       "plate purity (spec.plate + spec.plate_rgb), palette hits (spec.palette "
                       "hex list), magenta-adjacent px count, sharpness (spec.min_sharpness). "
                       "Returns measured values + violations, ok=false on any violation.",
        "schema": {"type": "object",
                   "properties": {
                       "filename": {"type": "string"},
                       "subfolder": {"type": "string"},
                       "spec": {"type": "object"}},
                   "required": ["filename"]},
        "fn": tool_verify},
    "forge_plate": {
        "description": "Deterministic chroma plate: crop source (ComfyUI filename or "
                       "kitout/<rel>) to center square, mask into disc (frac 0.3..1.0), composite "
                       "onto flat solid RGB(255,0,255), 4x antialias. NEVER ask the LLM to paint "
                       "the plate. Saves to kit out/mcp; corner purity measured in the result.",
        "schema": {"type": "object",
                   "properties": {
                       "source": {"type": "string"},
                       "subfolder": {"type": "string"},
                       "frac": {"type": "number"},
                       "out_name": {"type": "string"}},
                   "required": ["source", "out_name"]},
        "fn": tool_plate},
    "forge_scale": {
        "description": "Favicon-ladder gate: downscale a plate (ComfyUI filename or kitout/<rel>) "
                       "to ladder px (default 512/180/48/32/16), emit real PNGs + upscaled "
                       "montage under kit out/mcp; files served at GET /files/<name>.",
        "schema": {"type": "object",
                   "properties": {
                       "source": {"type": "string"},
                       "subfolder": {"type": "string"},
                       "ladder": {"type": "array", "items": {"type": "integer"}}},
                   "required": ["source"]},
        "fn": tool_scale},
    "forge_getfile": {
        "description": "Fetch a ComfyUI output file to kit out/mcp and return local path + "
                       "LAN /view URL (portal can fetch the URL directly over LAN).",
        "schema": {"type": "object",
                   "properties": {
                       "filename": {"type": "string"},
                       "subfolder": {"type": "string"},
                       "type": {"type": "string"}},
                   "required": ["filename"]},
        "fn": tool_getfile},
    "forge_router_status": {
        "description": "LLM router (port 8082, flint + nemotron) as measured values: listening, "
                       "model ids + states (loaded/unloaded), free RAM. No arguments.",
        "schema": {"type": "object", "properties": {}},
        "fn": tool_router_status},
    "forge_router_switch": {
        "description": "Load/switch a router model NOW (flint = general agent, nemotron = "
                       "ThinkingCap agentic/coder, MTP). Sends a READY ping with model=<id>: "
                       "this build has no admin load API - the ping IS the load (measured: "
                       "->flint 39.8 s, ->nemotron 8.6-18 s). Returns ping_ms + final states. "
                       "Use before a sub-agent phase so the first real request is warm.",
        "schema": {"type": "object",
                   "properties": {"model": {"type": "string"}},
                   "required": ["model"]},
        "fn": tool_router_switch},
    "forge_router_start": {
        "description": "Start the LLM router endpoint (8082) if not listening; nemotron "
                       "preloads via presets (load-on-startup). No arguments.",
        "schema": {"type": "object", "properties": {}},
        "fn": tool_router_start},
    "forge_router_stop": {
        "description": "Stop the LLM router process to free ~25-30 GB RAM for heavy renders "
                       "(VRAM untouched). The logon task revives it at next logon; "
                       "forge_router_start revives it now. No arguments.",
        "schema": {"type": "object", "properties": {}},
        "fn": tool_router_stop},
    "forge_router_offload": {
        "description": "Lease-backed VRAM offload: set the preset's -ngl (mesh law: 0 by "
                       "default; meshd passes 99 ONLY with a live lease) and warm-load the "
                       "model on demand (measured warm_ms). meshd mesh_router_offload is "
                       "the authorized caller (admission checked at the ledger).",
        "schema": {"type": "object", "properties": {
            "model": {"type": "string"}, "ngl": {"type": "integer"},
            "ctx": {"type": "integer"}, "lease_id": {"type": "string"}},
            "required": ["model"]},
        "fn": tool_router_offload},
    "forge_router_offload_revert": {
        "description": "Set the preset back to -ngl 0 (mesh law restored). GPU weights "
                       "release at the router idle-sleep (900 s) or next model switch; "
                       "the meshd lease is released separately. mesh_router_offload "
                       "action=revert is the authorized caller.",
        "schema": {"type": "object", "properties": {"model": {"type": "string"}},
                   "required": ["model"]},
        "fn": tool_router_offload_revert},
}


# ---------- JSON-RPC --------------------------------------------------------

def handle_rpc(req):
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params", {})
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": PROTOCOL,
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "forge-mcp", "version": "1.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"tools": [{"name": n, "description": t["description"],
                                      "inputSchema": t["schema"]} for n, t in TOOLS.items()]}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": "unknown tool: %s" % name}}
        try:
            res = TOOLS[name]["fn"](args)
            text = json.dumps(res, default=str)
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": text}],
                               "isError": not res.get("ok", True)}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text",
                                            "text": json.dumps({"ok": False, "error": repr(e)[:500]})}],
                               "isError": True}}
    if method and method.startswith("notifications/"):
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": "method not found: %s" % method}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stdout.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def do_POST(self):
        if self.path.split("?")[0] != "/mcp":
            out_json(self, {"error": "not found"}, 404)
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            out_json(self, {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "bad JSON: %s" % e}}, 400)
            return
        resp = handle_rpc(req)
        if resp is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        out_json(self, resp)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            out_json(self, {"ok": True, "server": "forge-mcp", "tools": sorted(TOOLS),
                            "health": tool_health()})
            return
        if path.startswith("/files/"):
            rel = urllib.parse.unquote(path[len("/files/"):])
            p = os.path.realpath(os.path.join(OUT, rel))
            if ".." in rel or not p.startswith(os.path.realpath(OUT) + os.sep) \
                    or not os.path.isfile(p):
                out_json(self, {"error": "not found"}, 404)
                return
            data = open(p, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if p.endswith(".png")
                             else "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        out_json(self, {"ok": True, "server": "forge-mcp",
                        "endpoints": ["POST /mcp", "GET /health", "GET /files/<name>"]})


def main():
    load_supervisor_config()
    os.makedirs(OUT, exist_ok=True)
    threading.Thread(target=supervisor_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stdout.write("forge-mcp on 0.0.0.0:%d (firewall-gated to portal IP; "
                     "supervisor comfy_idle_s=%d)\n" % (PORT, COMFY_IDLE_S))
    sys.stdout.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
