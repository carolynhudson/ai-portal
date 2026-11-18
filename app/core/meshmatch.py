"""meshmatch: the model-equivalence rubric (owner design, 2026-11-17).

A request names a model (id, or a coarse need). Nodes answer with service
cards. meshmatch ranks the candidate cards by a DETERMINISTIC rubric the
owner set, in this exact order:

  0. EXACT model match (id, alias, normalized stem) wins outright.
  1. FILTER: candidates must reach the requested context window
     (min(ctx_card, ctx_request) >= ctx_request).
  2. SIZE PROXY: parameter count estimated from GGUF geometry
     (params_est below). Closer to the requested params wins, with a
     bias toward BIGGER; an MoE is scored by ACTIVE params
     (expert_used_count), because active parameters are what the
     request actually pays for in latency. A min_params_ratio DROPS
     candidates far below the source (owner: 9B under a 27B is a
     capability cliff, not a near miss).
  3. FILE SIZE: weights bytes as the second size proxy (this is where
     quantization enters -- NOT quant names, per owner law: quant labels
     are maker marketing; bytes are physics). Closer wins, bias bigger.
  4. FIT: a candidate must fit the node it is offered on
     (weights + KV at the request ctx <= node free RAM when known).
     "Bigger" never outranks "fits": oversized bigger is discarded.
     A max_active_b task cap drops the too-slow-for-the-job class (the
     120B answers brilliantly and is useless for autocomplete, and it
     eats the RAM the IDE and its builds need).
  5. TAGS: only when the field is still tied -- feature tags
     (moe, vision, thinking/roleplay/coder tuning, family) break the
     tie by overlap with the request tags.
  6. STICKINESS (prefer_loaded): an already-loaded CLOSE fit beats a
     "better" fit that pays the load; the bonus SCALES with load time
     (a 27B's minutes of swap justify more stickiness than a 2B's
     seconds). Exact match still wins outright.

Every ranking carries `why` strings: the policy is VISIBLE data, the
same law as mesh.place's pref column.

params_est is a coarse scale measure from the header geometry (owner:
"how smart, coarsely: context size + parameter count"): attention +
SwiGLU FFN per layer + MoE experts + embeddings. It is a proxy for
COMPARING scale across architectures, not an audit of any model's true
parameter count, and the output says so via params_src.
"""
from __future__ import annotations

import math
import re

# geometry -> parameters. Shapes from the GGUF header keys the portal's
# modelmeta / meshd cards carry. Formula per layer:
#   attention: Q,K,V,O projections; gemma-style declares key_length/value_length
#   FFN: SwiGLU (gate,up,down) = 3*E*F; MoE swaps the FFN for
#        expert_count * 3*E*F_expert + a router (E*expert_count)
#   embeddings + output: 2 * V * E (tied embeddings make this an upper bound)
def params_est(meta: dict | None) -> dict | None:
    """{'params_b': float, 'active_b': float, 'params_src': str} or None
    when the geometry is too incomplete to price honestly (None, not 0 --
    the pricer law: a scheduler that receives 0 will happily load it)."""
    if not meta:
        return None
    g = lambda k: meta.get(k)
    L, E = g("block_count"), g("embedding_length")
    if not L or not E:
        return None
    hd = g("rope_dimension_count") or g("attention_key_length")
    heads = g("attention_count")
    if isinstance(heads, list):
        heads = max(heads, default=None)
    kvh = g("attention_kv_count")
    if isinstance(kvh, list):
        kvh = max(kvh, default=None)
    if not hd and heads:
        hd = E // heads
    if not hd:
        return None
    kvh = kvh if kvh is not None else heads
    # attention: Q (E x Qdim), K,V (E x KVdim each), O (Qdim x E)
    qdim = heads * hd
    kvdim = (kvh or 0) * (g("attention_value_length") or hd)
    attn = E * (qdim + 2 * kvdim) + qdim * E
    F = g("feed_forward_length")
    if not F:
        return None
    ffn = 3 * E * F
    n_exp = g("expert_count") or 0
    Fe = g("feed_forward_length_expert")
    if n_exp > 1 and Fe:
        ffn = n_exp * 3 * E * Fe + E * n_exp
    V = g("vocab_size")
    emb = 2 * V * E if V else 0
    per_layer = attn + ffn
    total = L * per_layer + emb
    active = total
    used = g("expert_used_count")
    if n_exp > 1 and used and Fe:
        dense_ffn = 3 * E * F
        active = L * (attn + used * 3 * E * Fe + E * n_exp + dense_ffn) + emb
    return {"params_b": round(total / 1e9, 2),
            "active_b": round(active / 1e9, 2),
            "params_src": "header-geometry"}


_TAG_RULES = [
    (r"thinkingcap|swift|gsq|mtp", "efficient_thinking"),
    (r"roleplay|humanlike|human-like|stheno|role[-_]", "roleplay"),
    (r"coder|code|instruct-coder|deepseek-coder", "code"),
    (r"reranker|embedding|embed", "embedding"),
    (r"vision|mmproj|clip|vit", "vision"),
    (r"nl|base", "base"),
]
_FAMILY_RULES = [
    (r"qwen3\.?8|qwen38", "qwen3.8"), (r"qwen3", "qwen3"), (r"qwen2", "qwen2"),
    (r"deepseek", "deepseek"), (r"gemma", "gemma"), (r"llama", "llama"),
    (r"granite", "granite"), (r"mistral|mixtral", "mistral"),
]


def tags_from(name: str, meta: dict | None) -> set[str]:
    """Deterministic feature tags from the model name + header geometry.
    Pure function of the same inputs everywhere: two nodes computing tags
    for the same model compute the same set."""
    t: set[str] = set()
    low = (name or "").lower()
    for rx, tag in _TAG_RULES:
        if re.search(rx, low):
            t.add(tag)
    for rx, fam in _FAMILY_RULES:
        if re.search(rx, low):
            t.add("family:" + fam)
            break
    m = meta or {}
    if (m.get("expert_count") or 0) > 1:
        t.add("moe")
    if m.get("filetype"):
        t.add("quant:" + str(m["filetype"]))
    return t


def _norm(name: str) -> str:
    # stem comparison: basename, lowercase, quant/maker noise segments
    # stripped (-Q5_K_M, -IQ3_S, -MTP, -HF, -BF16...). Stripping runs on
    # the underscore-intact form because the separator collapse below
    # turns q5_k_m into "q5 k m", which the quant pattern can no longer
    # see as one segment.
    n = (name or "").lower().replace("\\", "/").split("/")[-1]
    n = re.sub(r"\.(gguf|safetensors|bin|ckpt)$", "", n)
    n = re.sub(r"[-_.]?(q\d+[_kxm]*|iq\d[_a-z]+|bf16|f16|f32|mtp|hf)[-_.]?", " ", n)
    n = re.sub(r"[-_.]+", " ", n)
    return " ".join(n.split())


def _fits(cand: dict, ctx: int, free_ram_mib: int | None) -> bool:
    if free_ram_mib is None:
        return True  # no node signal: fit is not assessable, do not filter
    w = cand.get("weights_bytes") or 0
    kv = cand.get("kv_bytes_at_ctx")
    if kv is None:
        kv = cand.get("kv_bytes") or 0
    return (w + kv) <= free_ram_mib * 1048576


def candidates_from_cards(cards: list[dict]) -> list[dict]:
    """Adapt meshd service-cards to rubric candidates: geometry, size, and
    tags all derive from the card's own measured.gguf block, so every node
    (and the portal) computes the same facts from the same bytes."""
    out = []
    for c in cards:
        g = (c.get("measured") or {}).get("gguf") or {}
        out.append({
            "node": c.get("node"), "service": c.get("service"),
            "ctx": (c.get("limits") or {}).get("context") or 0,
            "params": params_est(g),
            "weights_bytes": (c.get("measured") or {}).get("weights_bytes") or 0,
            "kv_bytes": (c.get("measured") or {}).get("kv_bytes") or 0,
            "tags": tags_from(c.get("service") or "", g),
            "state": c.get("state"), "pref": c.get("pref"),
            "load_s": (c.get("time") or {}).get("load_s"),
        })
    return out


def rank(request: dict, cands: list[dict]) -> tuple[list, list]:
    """Rank candidate cards for a request. Request keys (all optional):
    model (id/name), ctx, params_b, weights_bytes, tags (list/set),
    free_ram_mib (node signal; when None the fit filter is skipped),
    min_params_ratio (failover law: a model FAR BELOW the source's size
    is a capability cliff, not a close match - 9B under a 27B request
    is DROPPED, not ranked last; the owner sets the ratio per scenario),
    max_active_b (task latency cap: the 120B that answers brilliantly
    is useless for autocomplete and eats the RAM the IDE and its builds
    need), prefer_loaded (stickiness: a loaded close fit beats a
    "better" fit that pays the load; the bonus SCALES with measured
    load time, because a 27B swap costs minutes a 2B swap does not),
    task (profile shorthand: "fim" -> active cap 30B + sticky;
    "chat" -> sticky).
    Candidate keys: service, ctx, params (params_est dict), weights_bytes,
    kv_bytes, tags (set), node. Returns (ranked, filtered): ranked =
    [{node, service, why}, ...] best first; filtered = the candidates the
    rubric dropped, each with the stated reason (silence = a stated
    filter, never a hidden one)."""
    req_model = _norm(request.get("model") or "")
    req_ctx = int(request.get("ctx") or 0)
    req_tags = set(request.get("tags") or ())
    free_ram = request.get("free_ram_mib")
    task = (request.get("task") or "").lower()
    max_active = request.get("max_active_b")
    if task == "fim" and max_active is None:
        max_active = 30.0
    prefer_loaded = request.get("prefer_loaded")
    if prefer_loaded is None and task in ("fim", "chat"):
        prefer_loaded = True
    if prefer_loaded is None and req_model in ("", "auto", "mesh"):
        # "place me somewhere" IS a chat-shaped ask: no size proxies to
        # score, so the stickiness law is the whole ranking
        prefer_loaded = True
    min_ratio = request.get("min_params_ratio")

    scored, filtered = [], []
    for c in cands:
        meta_p = (c.get("params") or {})
        p = meta_p.get("active_b") if "moe" in (c.get("tags") or ()) else meta_p.get("params_b")
        why = []
        if _norm(c.get("service") or "") == req_model and req_model:
            # exact copies on several nodes: the LOADED copy wins outright
            # (prefer_loaded) -- paying minutes of load to reach a cold
            # twin is the swap cost the owner named, with zero upside
            tie = 0.0 if (prefer_loaded and c.get("state") == "loaded") else 0.1
            why = ["EXACT model match"] + (["loaded copy wins"] if tie == 0.0
                                           else [])
            scored.append((-1.0 + tie, math.inf, 0,
                           0 if (prefer_loaded and c.get("state") == "loaded") else 1,
                           c, why))
            continue
        if req_ctx and (c.get("ctx") or 0) < req_ctx:
            # failover law: the ctx floor IS the source's ctx. A model
            # with a smaller window cannot take the session over - the
            # transfer can fail outright mid-context.
            filtered.append({"node": c.get("node"), "service": c.get("service"),
                             "why": f"ctx {c.get('ctx')} < {req_ctx}"})
            continue
        if min_ratio and request.get("params_b") and p is not None:
            floor = float(request["params_b"]) * float(min_ratio)
            if p < floor:
                filtered.append({"node": c.get("node"), "service": c.get("service"),
                                 "why": f"params {p}B far below source (floor {floor:.1f}B)"})
                continue
        if max_active and meta_p.get("active_b") is not None and meta_p["active_b"] > float(max_active):
            filtered.append({"node": c.get("node"), "service": c.get("service"),
                             "why": f"active {meta_p['active_b']}B over task cap {max_active}B"})
            continue
        if not _fits(c, req_ctx, free_ram):
            filtered.append({"node": c.get("node"), "service": c.get("service"),
                             "why": "does not fit node free RAM"})
            continue
        size_pen, w_pen = math.inf, math.inf
        if request.get("params_b") and p:
            req_p = float(request["params_b"])
            # owner bias: bigger over smaller; a 10% overshoot costs less
            # than a 10% undershoot
            rel = (p - req_p) / req_p
            size_pen = abs(rel) * (0.5 if rel > 0 else 1.0)
            why.append(f"params {p}B vs {req_p}B")
            # stickiness: an already-loaded close fit beats a "better"
            # fit that pays the load. The bonus SCALES with load time -
            # minutes of swap for a 27B justify more stickiness than a
            # 2B's seconds (load_s from the measured bench lane when
            # available, computed estimate otherwise).
            if prefer_loaded and c.get("state") == "loaded":
                load_s = c.get("load_s") or 0.0
                stick = 0.2 + 0.3 * min(load_s / 120.0, 1.0)
                size_pen = max(size_pen - stick, -0.5)
                why.append(f"loaded: stickiness -{stick:.2f} (load {load_s:g}s)")
        if request.get("weights_bytes") and c.get("weights_bytes"):
            req_w = float(request["weights_bytes"])
            relw = (c["weights_bytes"] - req_w) / req_w
            w_pen = abs(relw) * (0.5 if relw > 0 else 1.0)
            why.append(f"weights {c['weights_bytes'] / 2**30:.1f} GiB")
        tag_pen = 0.0
        if req_tags:
            ct = set(c.get("tags") or ())
            miss = len(req_tags - ct)
            if miss:
                tag_pen = miss
                why.append(f"tags missing: {','.join(sorted(req_tags - ct))}")
        # the load tier breaks full ties (all rubric fields equal) in favor
        # of the loaded copy -- and for model="auto" (no size proxies, all
        # cards tie) it is the whole law: point at what is ALREADY UP
        load_tier = 0 if (prefer_loaded and c.get("state") == "loaded") else 1
        if load_tier == 0 and size_pen == math.inf:
            why.append("loaded: auto lands on what is up")
        scored.append((size_pen, w_pen, tag_pen, load_tier, c, why))

    scored.sort(key=lambda s: (s[0], s[1], s[2], s[3],
                               str(s[4].get("node")), str(s[4].get("service"))))
    out = [{"node": c.get("node"), "service": c.get("service"),
            # router_url rides through for the /llm gateway's proxy target;
            # the rubric itself never reads it (state feeds stickiness)
            "router_url": c.get("router_url"), "state": c.get("state"),
            "why": w or ["rubric: ctx ok"]} for _, _, _, _, c, w in scored]
    return out, filtered
