#!/usr/bin/env python3
"""Regenerate the Windows agent's opencode.json from the router's truth.

    python3 tools/sync_agent_models.py [--print]

The router runs with --models-max 1: exactly ONE model is resident, and
requesting any other catalog id forces a swap (unload the resident, load
the request) - which evicts the portal harness. So the agent's config is
never a hand-list of favorites and never a blind copy of /v1/models (that
lists the whole catalog, 20 ids, only one of them safe). It is the
LOADED model, generated: id, context, output cap, capability flags,
aliases - all read from the router process the moment you run this.

Run on the portal box. If the router is unreachable, the script refuses
and leaves the file alone: a stale generated file is honest (it carries
its generated_at); a guessed one is not.
"""
from __future__ import annotations

import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path

ROUTER = "http://127.0.0.1:8080"
PRESETS = Path.home() / "ai/config/llama/presets.ini"
CONFIG = Path(__file__).resolve().parents[1] / "deploy/windows-agent/opencode.json"

DEFAULT_CONTEXT = 32768        # llama.cpp's n_ctx default when nothing says otherwise
DEFAULT_OUTPUT = 8192
CTX_FROM_ALIAS = (262144, 16384)  # native window for Qwen3-Next class ids (as the harness runs them)
REPO_RE = re.compile(r"--hf-repo\s+(\S+)")
CTX_RE = re.compile(r"--ctx-size\s+(\d+)")
ALIAS_RE = re.compile(r"--alias\s+(\S+)")


def preset_ctx(model_id: str) -> int | None:
    """The [section] ctx-size for a model id, if the presets pin one."""
    if not PRESETS.is_file():
        return None
    section = None
    for line in PRESETS.read_text().splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
        m = re.match(r"ctx-size\s*=\s*(\d+)", s)
        if m and section == model_id:
            return int(m.group(1))
    return None


def build_config(models: dict[str, dict]) -> dict:
    """The opencode.json payload from built model entries {catalog_id: entry}."""
    first = sorted(models)[0]
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "llamarouter": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Halo llama.cpp router (192.168.1.228)",
                "options": {"baseURL": "http://192.168.1.228:8080/v1",
                            "apiKey": "local"},
                "models": models,
            }
        },
        "model": "llamarouter/" + first,
    }


def main(argv: list[str]) -> int:
    show = "--print" in argv
    try:
        with urllib.request.urlopen(ROUTER + "/props", timeout=5) as r:
            props = json.load(r)
        with urllib.request.urlopen(ROUTER + "/v1/models", timeout=5) as r:
            catalog = {m["id"]: m for m in json.load(r)["data"]}
        procs = subprocess_ps()
    except Exception as exc:
        print(f"router unreachable ({exc}); leaving {CONFIG} alone", file=sys.stderr)
        return 2

    # The router front carries no --hf-repo; workers do. --models-max 1
    # means at most one worker, so "resident" is that worker's model id -
    # the exact string callers pass as `model` and opencode lists as id.
    resident = None
    worker_ctx = None
    worker_alias = None
    for cmd in procs:
        m = REPO_RE.search(cmd)
        if m and "--alias" in cmd:
            resident = m.group(1)
            a = ALIAS_RE.search(cmd)
            worker_alias = a.group(1) if a else None
            c = CTX_RE.search(cmd)
            worker_ctx = int(c.group(1)) if c else None
            break
    if resident is None:
        print("no resident worker (--hf-repo) - nothing safe to configure", file=sys.stderr)
        return 2

    ctx = preset_ctx(resident) or worker_ctx
    native = ":IQ3" in resident  # the 262k-window Swift family, as the harness runs it
    if ctx is None:
        ctx = CTX_FROM_ALIAS[0] if native else DEFAULT_CONTEXT
    entry = {
        "name": (worker_alias or resident) + " (resident: zero load)",
        "tool_call": True,
        "temperature": True,
        "limit": {"context": ctx,
                  "output": CTX_FROM_ALIAS[1] if native else DEFAULT_OUTPUT},
    }
    if native or "Swift" in resident:
        entry["reasoning"] = True
        entry["interleaved"] = "reasoning_content"

    cfg = build_config({resident: entry})
    if show:
        print(json.dumps(cfg, indent=2))
        return 0
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"wrote {CONFIG}: resident={resident} ctx={ctx} "
          f"at {datetime.datetime.now().isoformat(timespec='seconds')}")
    return 0


def subprocess_ps() -> list[str]:
    import subprocess
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=10)
    return out.stdout.splitlines()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
