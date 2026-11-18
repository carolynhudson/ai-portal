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
     request actually pays for in latency.
  3. FILE SIZE: weights bytes as the second size proxy (this is where
     quantization enters -- NOT quant names, per owner law: quant labels
     are maker marketing; bytes are physics). Closer wins, bias bigger.
  4. FIT: a candidate must fit the node it is offered on
     (weights + KV at the request ctx <= node free RAM when known).
     "Bigger" never outranks "fits": oversized bigger is discarded.
  5. TAGS: only when the field is still tied -- feature tags
     (moe, vision, thinking/roleplay/coder tuning, family) break the
     tie by overlap with the request tags.

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
        })
    return out


def rank(request: dict, cands: list[dict]) -> tuple[list, list]:
    """Rank candidate cards for a request. Request keys (all optional):
    model (id/name), ctx, params_b, weights_bytes, tags (list/set),
    free_ram_mib (node signal; when None the fit filter is skipped).
    Candidate keys: service, ctx, params (params_est dict), weights_bytes,
    kv_bytes, tags (set), node. Returns (ranked, filtered): ranked =
    [{node, service, why}, ...] best first; filtered = the candidates the
    rubric dropped, each with the stated reason (silence = a stated
    filter, never a hidden one)."""
    req_model = _norm(request.get("model") or "")
    req_ctx = int(request.get("ctx") or 0)
    req_tags = set(request.get("tags") or ())
    free_ram = request.get("free_ram_mib")

    scored, filtered = [], []
    for c in cands:
        meta_p = (c.get("params") or {})
        p = meta_p.get("active_b") if "moe" in (c.get("tags") or ()) else meta_p.get("params_b")
        why = []
        if _norm(c.get("service") or "") == req_model and req_model:
            scored.append((-1.0, math.inf, 0, 0, c, ["EXACT model match"]))
            continue
        if req_ctx and (c.get("ctx") or 0) < req_ctx:
            filtered.append({"node": c.get("node"), "service": c.get("service"),
                             "why": f"ctx {c.get('ctx')} < {req_ctx}"})
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
        scored.append((size_pen, w_pen, tag_pen, 0.0, c, why))

    scored.sort(key=lambda s: (s[0], s[1], s[2], str(s[4].get("node")), str(s[4].get("service"))))
    out = [{"node": c.get("node"), "service": c.get("service"),
            "why": w or ["rubric: ctx ok"]} for _, _, _, _, c, w in scored]
    return out, filtered
