"""The embedding sidecar client — text in, unit vector out, no model in the router.

Embeddings run on a standalone llama-server (the sidecar: `tools/embed_sidecar.sh`,
measured 2026-10-16: EmbeddingGemma 2 Q8_0, ready in 2s, 768-dim normalized vectors,
~0.3 GB, CPU) — deliberately NOT the router: `--models-max 1` means a model in the
router is an agent's eviction, and an embedding call must not cost an agent its model.
The sidecar interface is `/v1/embeddings`, so the day the NPU ONNX path lands, the
endpoint's occupant changes and this module does not.

The prefixes are not decoration. EmbeddingGemma 2 is trained asymmetric: a query
prefixed `task: search result | query: …` and a document prefixed `title: … | text: …`
embed measurably closer together than bare text. The module applies them so callers
cannot forget; `raw()` exists for callers that must embed pre-formatted input.

Every rule this module enforces is from the model card, measured on this box:
vectors are 768 finite floats, L2-normalized, FP16 is banned (NaN), and MRL
truncation must re-normalize after slicing — so truncation is not offered.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request

SIDECAR_DEFAULT = "http://127.0.0.1:8390/v1"
QUERY_PREFIX = "task: search result | query: "
DIM = 768
_PROBE_TTL = 15.0
_probe = {"at": 0.0, "ok": False}


def endpoint() -> str:
    return os.environ.get("PORTAL_EMBED_URL", SIDECAR_DEFAULT)


def live(timeout: float = 3.0) -> bool:
    """Is the sidecar answering? Probed at most once per 15s: memory_add must not
    block for three seconds per row because nobody started the sidecar."""
    now = time.time()
    if now - _probe["at"] < _PROBE_TTL:
        return bool(_probe["ok"])
    base = endpoint().rstrip("/")
    probe_url = base[: -len("/v1")] + "/health" if base.endswith("/v1") else base + "/health"
    ok = False
    try:
        with urllib.request.urlopen(probe_url, timeout=timeout) as r:
            ok = r.status == 200
    except Exception:  # noqa: BLE001 — a dead sidecar is a state, not an event
        ok = False
    _probe.update(at=now, ok=ok)
    return ok


def _post_embed(inputs: list[str], timeout: float = 30.0) -> list[list[float]]:
    req = urllib.request.Request(
        endpoint().rstrip("/") + "/embeddings",
        data=json.dumps({"model": "embeddinggemma2", "input": inputs,
                         "encoding_format": "float"}).encode(),
        headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    rows = sorted(data.get("data") or [], key=lambda e: e.get("index", 0))
    out: list[list[float]] = []
    for e in rows:
        vec = e.get("embedding") or []
        if len(vec) != DIM or not all(math.isfinite(v) for v in vec):
            raise ValueError(f"the sidecar returned {len(vec)} values, not {DIM} finite ones")
        out.append(vec)
    if len(out) != len(inputs):
        raise ValueError(f"asked for {len(inputs)} embeddings, got {len(out)}")
    return out


def embed_documents(texts: list[str], title: str = "none") -> list[list[float]]:
    """Corpus side of the asymmetric pair: `title: … | text: …` (the model card's
    document format; `title: none` when there is no real title)."""
    return _post_embed([f"title: {title} | text: {t}" for t in texts])


def embed_query(query: str) -> list[float]:
    """Query side: `task: search result | query: …`."""
    return _post_embed([QUERY_PREFIX + query])[0]


def raw(texts: list[str]) -> list[list[float]]:
    """Pre-formatted input, no prefixes applied (the caller owns the format)."""
    return _post_embed(texts)


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine of the two vectors; the sidecar's vectors arrive normalized, this
    normalizes anyway so a third-party occupant of the endpoint cannot skew ranks."""
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)
