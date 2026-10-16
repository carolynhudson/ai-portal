"""Harness configs, generated from the router's own account (§6.14.1m, queue 18).

Two harnesses read two files, and both files describe the same fact: which models exist on this box and
where the router answers. The portal is the component that knows that — `routerctl` reads the unit's
command line for the port and asks the router's own `/v1/models` for what it serves — so the portal
writes the files, and the operator never edits a harness config by hand.

Why generated, and why the router is the source: the measured failure this retires is the operator's own
ritual. pi's llama.cpp provider will answer with a model *named* only when the model is loaded, and an
empty credential makes every selection fail with "No API key for llama.cpp/…"; the manual path to that
is `/login llama.cpp=http://127.0.0.1:8080` answered with a blank key, then `/model`, then hoping the
model you wanted is in the list at all. opencode is kinder (a 12-line config gave it the router's 16
models and `connected` in `GET /provider`), but it is the same fact restated in a second syntax, and a
second file a person has to keep correct. Two hand-written files describing one server drift; a
generated pair cannot.

What goes in them:

- **The model list**, from the router, so the harnesses offer exactly what this box can serve.
- **A non-empty api key** (`portal`). The router ignores it; pi's credential check does not — measured,
  in both directions, on this box.
- **Permission posture**, in opencode's own vocabulary (`permission`, `external_directory`): project
  scoped by default, full-auto inside the project, or everything with the portal's own big confirmation
  in front of it. Generated here so the page's three choices are the file's three shapes, and a deploy
  job that needs `~/ai/` is a posture the operator picks, not a hole punched in a config.
- **Compaction**, with `prune: true`. The compaction bug the operator keeps hitting — a session that
  loses the thread and has to be restarted with a written handoff — is a small-context local-model
  problem, and dropping old tool outputs is the knob that costs nothing and helps most.

Writes are conservative, because these files belong to the operator's harnesses and not to the portal:
a file that is not there is created, a file that parses is merged (only our provider key is replaced,
foreign providers survive), a file that does not parse is left completely alone and reported. Changing
an existing file keeps a `.portal-prev` copy, so a bad generation is a `mv` away from being undone.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.core import routerctl

# Provider ids. `llama.cpp` is pi's own built-in id, so model ids come out as
# `llama.cpp/<repo>:<quant>` — the shape pi's own docs and the ACP config options use.
PI_PROVIDER = "llama.cpp"
OC_PROVIDER = "llamacpp"

# The router does not authenticate; pi's provider machinery insists on a non-empty
# credential before it will name a model. This is the whole content of that key.
STANDIN_KEY = "portal"

# The three postures the Projects page offers. `external_directory` is opencode's
# guard for paths outside the project working directory; `~/ai/**` is the tree a
# portal deploy has to reach, so the widest posture is the one that opens it.
POSTURES: dict[str, dict[str, Any]] = {
    "project": {
        "read": {"*": "allow"},
        "edit": {"*": "allow"},
        "bash": {"*": "ask", "git status*": "allow", "git diff*": "allow", "pytest*": "allow"},
        "task": "allow",
        "skill": "allow",
        "question": "allow",
        "webfetch": "ask",
        "external_directory": {"*": "ask"},
        "doom_loop": "ask",
    },
    "project-auto": {
        "read": {"*": "allow"},
        "edit": {"*": "allow"},
        "bash": {"*": "allow"},
        "task": "allow",
        "skill": "allow",
        "question": "allow",
        "webfetch": "allow",
        "external_directory": {"*": "deny"},
        "doom_loop": "ask",
    },
    "all": {
        "*": "allow",
        "external_directory": {"*": "allow", "~/ai/state/portal/secrets/**": "deny"},
        "doom_loop": "ask",
    },
}

POSTURE_CHOICES = tuple(POSTURES)

COMPACT = {"auto": True, "prune": True, "reserved": 8000}


def _default_dirs() -> tuple[Path, Path]:
    home = Path(os.environ.get("AI_PORTAL_HOME", str(Path.home())))
    return home / ".pi" / "agent", home / ".config" / "opencode"


def router_models(port: int, fetch: Callable[[str], str] | None = None) -> dict[str, Any]:
    """The router's registered models, in its own words. Degrades alone."""
    report = routerctl.served_models(port, timeout=3.0)
    if not report.get("ok"):
        return {"ok": False, "error": report.get("error") or "the router did not answer", "ids": [], "loaded": []}
    try:
        url = _router_url(port, "/v1/models")
        get = fetch if fetch is not None else _open
        rows = json.loads(get(url)).get("data") or []
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"the router's model list is unreadable: {e}", "ids": [], "loaded": []}
    ids = [m.get("id") for m in rows if m.get("id")]
    # The caps ride the same answer: `context_length` and per-model `architecture` are
    # the router's own account of every catalog entry (b11669), which is what
    # opencode's model entry wants. Measured: b11669 omits supported_parameters, so
    # the per-model entry cannot ask "does this model honor reasoning_effort" of the
    # router — see _model_entry.
    caps: dict[str, dict[str, Any]] = {}
    for m in rows:
        mid = m.get("id")
        if not mid:
            continue
        caps[mid] = {"context_length": m.get("context_length"),
                     "architecture": m.get("architecture"),
                     "loaded": (m.get("status") or {}).get("value") == "loaded"}
    return {"ok": True, "ids": ids, "loaded": list(report.get("loaded_ids") or []), "caps": caps}


def _router_url(port: int, path: str) -> str:
    """Only the loopback router, over http. A test seam cannot aim this somewhere else."""
    if not isinstance(port, int) or port <= 0 or port > 65535:
        raise ValueError(f"not a port: {port!r}")
    return f"http://127.0.0.1:{port}{path}"


def _open(url: str) -> str:
    """Plain HTTP to the loopback router, by host and port -- no URL scheme to interpret."""
    import http.client
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port:
        raise ValueError(f"only the loopback router is fetched here: {url!r}")
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        conn.request("GET", parts.path or "/")
        return conn.getresponse().read().decode("utf-8", "replace")
    finally:
        conn.close()


def _load(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        # pi accepts comments in models.json. We do not write them, and we refuse to
        # write over them: reproducing a file we cannot parse is how a generator
        # destroys a hand-written config.
        return None, f"{path.name} is not plain JSON ({type(e).__name__}): leaving it alone"
    if not isinstance(data, dict):
        return None, f"{path.name} is not a JSON object: leaving it alone"
    return data, None


def _store(path: Path, data: dict[str, Any]) -> bool:
    """Write only when the bytes change; keep one prior copy when they do."""
    new = json.dumps(data, indent=2) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == new:
        return False
    if path.is_file():
        path.with_suffix(path.suffix + ".portal-prev").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(path)
    return True


def pi_models_config(port: int, ids: Sequence[str]) -> dict[str, Any]:
    return {
        "providers": {
            PI_PROVIDER: {
                "baseUrl": f"http://127.0.0.1:{port}/v1",
                "api": "openai-completions",
                "apiKey": STANDIN_KEY,
                "models": [{"id": i} for i in ids],
            }
        }
    }


def _model_entry(mid: str, cap: dict[str, Any] | None) -> dict[str, Any]:
    """One model's opencode entry, stated from the router's own account.

    The router answers `/v1/models` with `context_length` and per-model
    `architecture.input_modalities`/`output_modalities` for every catalog entry — measured
    on b11669 — and those are the numbers opencode's model entry wants
    (`limit.context`, `modalities`). b11669 omits `supported_parameters`, the field a
    tool would read to learn what a chat model honors, so the two facts that are NOT
    in the router's answer are stated as what llama.cpp's OpenAI-compatible surface
    is, measured on this box's own models:

      * interleaved thinking arrives on `reasoning_content` (IQ3_XXS and IQ3_S both
        answer with it), which is opencode's `interleaved` field — the declaration that
        makes opencode show the reasoning instead of losing it;
      * reasoning-effort levels (`minimal`/`low`/`medium`/`high`) send `reasoning_effort`,
        which llama.cpp does not read. Declaring them disabled is the honest answer to
        "which levels are allowed": on this server thinking is the template's, not the
        caller's dial.

    A model the router lists but has never loaded is still stated with the same surface:
    it is the server's behavior, not the model's, and the template decides per reply
    whether there is any thinking in it.
    """
    entry: dict[str, Any] = {
        "name": mid.split("/")[-1],
        "reasoning": True,
        "interleaved": "reasoning_content",
        "tool_call": True,
        "temperature": True,
        "variants": {v: {"disabled": True}
                     for v in ("none", "minimal", "low", "medium", "high", "xhigh")},
    }
    cap = cap or {}
    ctx = cap.get("context_length")
    if isinstance(ctx, (int, float)) and ctx > 0:
        # `limit` demands both halves; llama.cpp's server default for n_predict is -1
        # (generate to the end of the prompt's budget), and 16k is a headroom that
        # every model here has demonstrated. It is a request cap, not a promise.
        entry["limit"] = {"context": int(ctx), "output": 16384}
    arch = cap.get("architecture")
    if isinstance(arch, dict):
        modalities: dict[str, Any] = {}
        if isinstance(arch.get("input_modalities"), list):
            modalities["input"] = [m for m in arch["input_modalities"] if isinstance(m, str)]
        if isinstance(arch.get("output_modalities"), list):
            modalities["output"] = [m for m in arch["output_modalities"] if isinstance(m, str)]
        if modalities:
            entry["modalities"] = modalities
    return entry


def opencode_config(port: int, ids: Sequence[str], posture: str = "project",
                    mcp: dict[str, Any] | None = None,
                    caps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            OC_PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "llama.cpp router",
                "options": {"baseURL": f"http://127.0.0.1:{port}/v1", "apiKey": STANDIN_KEY},
                "models": {i: _model_entry(i, (caps or {}).get(i)) for i in ids},
            }
        },
        "permission": POSTURES[posture],
        "compaction": COMPACT,
    }
    if ids:
        cfg["model"] = f"{OC_PROVIDER}/{ids[0]}"
    if mcp:
        cfg["mcp"] = mcp
    return cfg


def merge(existing: dict[str, Any], generated: dict[str, Any], provider: str) -> dict[str, Any]:
    """Replace only our provider; everything the operator put there survives."""
    out = dict(existing)
    providers = dict(out.get("providers") or {})
    providers[provider] = generated["providers"][provider]
    out["providers"] = providers
    return out


def _merge_top(existing: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    """Our keys, the operator's choices.

    The provider entry is ours (it is derived from the router), and so are `permission` and
    `compaction` (they are what the page's posture control writes). `model` is the operator's default
    model, and is written only when the file has none: silently moving someone's default onto the
    router is exactly the sort of surprise a generator must not commit.
    """
    out = dict(existing)
    for key, value in generated.items():
        if key == "provider":
            providers = dict(out.get("provider") or {})
            providers[OC_PROVIDER] = value[OC_PROVIDER]
            out["provider"] = providers
        elif key == "model" and out.get("model"):
            continue
        else:
            out[key] = value
    return out


def status(pi_dir: Path | None = None, oc_dir: Path | None = None,
           port: int | None = None) -> dict[str, Any]:
    """What the two harness configs are, without writing anything."""
    pi_dir, oc_dir = pi_dir or _default_dirs()[0], oc_dir or _default_dirs()[1]
    port = port if port is not None else _router_port()
    pi_path, oc_path = pi_dir / "models.json", oc_dir / "opencode.json"
    pi_data, pi_err = _load(pi_path)
    oc_data, oc_err = _load(oc_path)
    out: dict[str, Any] = {
        "port": port,
        "pi": {"path": str(pi_path), "present": pi_path.is_file(), "provider": PI_PROVIDER,
               "error": pi_err},
        "opencode": {"path": str(oc_path), "present": oc_path.is_file(), "provider": OC_PROVIDER,
                     "error": oc_err},
        "auth": auth_status(pi_dir),
    }
    if pi_data is not None:
        entry = (pi_data.get("providers") or {}).get(PI_PROVIDER) or {}
        out["pi"]["models"] = len(entry.get("models") or [])
        out["pi"]["baseUrl"] = entry.get("baseUrl")
    if oc_data is not None:
        entry = (oc_data.get("provider") or {}).get(OC_PROVIDER) or {}
        out["opencode"]["models"] = len(entry.get("models") or {})
        out["opencode"]["baseUrl"] = ((entry.get("options") or {}).get("baseURL"))
        out["opencode"]["posture"] = _posture_of(oc_data.get("permission"))
    return out


def _posture_of(perm: Any) -> str | None:
    for name, rules in POSTURES.items():
        if perm == rules:
            return name
    return "custom" if perm else None


def _router_port() -> int | None:
    try:
        report = routerctl.self_report()
        router = (report.get("processes") or {}).get("router") or {}
        return (router.get("launch") or {}).get("port")
    except Exception:  # noqa: BLE001
        return None


def auth_status(pi_dir: Path | None = None) -> dict[str, Any]:
    """Does pi have a usable credential for the router provider? Values are never reported."""
    pi_dir = pi_dir or _default_dirs()[0]
    path = pi_dir / "auth.json"
    out: dict[str, Any] = {"path": str(path), "present": path.is_file(), "entry": False, "key_set": False}
    if not path.is_file():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        out["error"] = f"unreadable ({type(e).__name__})"
        return out
    entry = data.get(PI_PROVIDER)
    if isinstance(entry, dict):
        out["entry"] = True
        out["key_set"] = bool(entry.get("key"))
    return out


def sync(pi_dir: Path | None = None, oc_dir: Path | None = None,
         port: int | None = None, posture: str = "project",
         mcp: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write both harness configs from the router's account of itself. Refuses rather than guesses."""
    if posture not in POSTURES:
        return {"ok": False, "error": f"unknown posture {posture!r}; the choices are {', '.join(POSTURE_CHOICES)}"}
    pi_dir, oc_dir = pi_dir or _default_dirs()[0], oc_dir or _default_dirs()[1]
    port = port if port is not None else _router_port()
    if not port:
        return {"ok": False, "error": "the router's port is not known, so there is nothing to point a harness at"}
    found = router_models(port)
    if not found.get("ok"):
        return {"ok": False, "error": found.get("error") or "the router did not answer"}
    if not found["ids"]:
        return {"ok": False, "error": "the router reported no models; a harness with an empty picker is worse than no harness"}

    pi_path, oc_path = pi_dir / "models.json", oc_dir / "opencode.json"
    pi_existing, pi_err = _load(pi_path)
    oc_existing, oc_err = _load(oc_path)
    if pi_err:
        return {"ok": False, "error": pi_err}
    if oc_err:
        return {"ok": False, "error": oc_err}

    pi_changed = _store(pi_path, merge(pi_existing or {}, pi_models_config(port, found["ids"]), PI_PROVIDER))
    oc_changed = _store(oc_path, _merge_top(oc_existing or {},
                                            opencode_config(port, found["ids"], posture, mcp,
                                                            caps=found.get("caps"))))
    return {
        "ok": True,
        "port": port,
        "models": len(found["ids"]),
        "loaded": found["loaded"],
        "pi": {"path": str(pi_path), "changed": pi_changed, "models": len(found["ids"])},
        "opencode": {"path": str(oc_path), "changed": oc_changed, "models": len(found["ids"]),
                     "posture": posture},
        "auth": auth_status(pi_dir),
    }
