"""The harness configs the portal generates (§6.14.1m, queue 18).

These two files — `~/.pi/agent/models.json` for pi, `~/.config/opencode/opencode.json` for opencode —
are the whole interface between the portal's llama.cpp router and a coding agent. The failures they
carry are the operator's, and they are all measured: an empty credential makes every pi model
selection answer `No API key for llama.cpp/…`; a config that lists models the router has never heard of
produces a picker with nothing in it; two hand-written files describing one server drift apart.

What the tests care about, in order: the generated content is what the harnesses actually accept
(shapes measured against the running router and opencode 1.19.0), a hand-written config survives us, a
file we cannot parse is never rewritten, running twice changes nothing, and nothing is written when the
router has nothing to say.
"""
from __future__ import annotations

import json

import pytest

from app.core import agentconfig, routerctl

IDS = [
    "ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF:IQ3_XXS",
    "ggml-org/embeddinggemma-300M-GGUF:Q8_0",
    "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF:IQ3_S",
]


@pytest.fixture(autouse=True)
def _router_answers(monkeypatch):
    """Every test here is about the files, so the router answers the same way in all of them."""
    def fake(port, timeout=3.0):
        return {"ok": True, "count": len(IDS), "loaded": 1, "loaded_ids": IDS[:1],
                "aliases": ["swift-iq3"]}

    def open_url(url):
        return json.dumps({"data": [{"id": i} for i in IDS]})

    monkeypatch.setattr(routerctl, "served_models", fake)
    monkeypatch.setattr(agentconfig, "_open", open_url)


def _dirs(tmp_path):
    pi, oc = tmp_path / "pi", tmp_path / "oc"
    pi.mkdir()
    oc.mkdir()
    return pi, oc


# --- the content -------------------------------------------------------------------------------

def test_the_pi_config_is_the_shape_pi_accepted_on_this_box():
    """`{providers: {"llama.cpp": {baseUrl, api, apiKey, models}}}` — measured to bind and run.

    The provider id is pi's own (`llama.cpp`), the base URL carries `/v1`, and the key is non-empty:
    with a blank key every selection fails `No API key for llama.cpp/…`, which is the exact state the
    operator's manual `/login llama.cpp=<url>` with an empty answer leaves behind.
    """
    cfg = agentconfig.pi_models_config(8080, IDS)
    entry = cfg["providers"][agentconfig.PI_PROVIDER]
    assert entry["baseUrl"] == "http://127.0.0.1:8080/v1"
    assert entry["api"] == "openai-completions"
    assert entry["apiKey"], "an empty key is the failure this file exists to prevent"
    assert [m["id"] for m in entry["models"]] == IDS


def test_the_opencode_config_is_the_shape_opencode_connected_with():
    cfg = agentconfig.opencode_config(8080, IDS, "project")
    entry = cfg["provider"][agentconfig.OC_PROVIDER]
    assert entry["npm"] == "@ai-sdk/openai-compatible"
    assert entry["options"]["baseURL"] == "http://127.0.0.1:8080/v1"
    assert entry["options"]["apiKey"]
    assert set(entry["models"]) == set(IDS)
    assert cfg["model"] == f"{agentconfig.OC_PROVIDER}/{IDS[0]}"
    assert cfg["permission"] == agentconfig.POSTURES["project"]
    assert cfg["compaction"]["prune"] is True, "old tool outputs are what overflow a small local context"


def test_the_postures_differ_exactly_where_the_operator_said_they_must():
    """Project-scoped by default; full-auto inside the project; everything, with the secrets kept."""
    scoped = agentconfig.POSTURES["project"]
    auto = agentconfig.POSTURES["project-auto"]
    every = agentconfig.POSTURES["all"]
    assert scoped["bash"]["*"] == "ask"
    assert scoped["external_directory"]["*"] == "ask", "leaving the project is a decision, not a default"
    assert auto["bash"]["*"] == "allow"
    assert auto["external_directory"]["*"] == "deny", "full-auto is inside the project, not everywhere"
    assert every["*"] == "allow"
    assert every["external_directory"]["~/ai/state/portal/secrets/**"] == "deny", "the widest posture keeps the portal's secrets out of reach"
    for name in ("project", "project-auto", "all"):
        assert agentconfig.POSTURES[name]["doom_loop"] == "ask", "a repeated call is worth a prompt in every posture"


def test_an_unknown_posture_is_refused_before_anything_is_written(tmp_path):
    """Posture validation happens before a path is touched, so the refusal cannot half-write a pair."""
    pi, oc = _dirs(tmp_path)
    out = agentconfig.sync(pi_dir=pi, oc_dir=oc, port=8080, posture="yolo")
    assert not out["ok"]
    assert "posture" in out["error"]
    assert list(pi.iterdir()) == [] and list(oc.iterdir()) == []


# --- the writing -------------------------------------------------------------------------------

def test_sync_writes_both_files_and_names_the_models(tmp_path):
    pi, oc = _dirs(tmp_path)
    out = agentconfig.sync(pi_dir=pi, oc_dir=oc, port=8080)
    assert out["ok"], out
    assert out["models"] == len(IDS)
    assert out["loaded"] == IDS[:1], "the page should be able to say which model the router has loaded"
    pi_data = json.loads((pi / "models.json").read_text())
    oc_data = json.loads((oc / "opencode.json").read_text())
    assert [m["id"] for m in pi_data["providers"][agentconfig.PI_PROVIDER]["models"]] == IDS
    assert set(oc_data["provider"][agentconfig.OC_PROVIDER]["models"]) == set(IDS)


def test_a_provider_the_operator_wrote_survives_our_generation(tmp_path):
    pi, oc = _dirs(tmp_path)
    (pi / "models.json").write_text(json.dumps(
        {"providers": {"openai": {"baseUrl": "https://api.openai.com/v1", "models": [{"id": "gpt-6"}]}}},
        indent=2))
    (oc / "opencode.json").write_text(json.dumps(
        {"provider": {"ollama": {"npm": "@ai-sdk/openai-compatible",
                                 "options": {"baseURL": "http://localhost:11434/v1"},
                                 "models": {"qwen2.5-coder:7b": {}}}},
         "model": "ollama/qwen2.5-coder:7b"}, indent=2))

    out = agentconfig.sync(pi_dir=pi, oc_dir=oc, port=8080)
    assert out["ok"], out
    pi_data = json.loads((pi / "models.json").read_text())
    oc_data = json.loads((oc / "opencode.json").read_text())
    assert "openai" in pi_data["providers"], "we add our provider, we do not replace the file"
    assert "ollama" in oc_data["provider"]
    assert oc_data["model"] == "ollama/qwen2.5-coder:7b", "the operator's default model is theirs to keep"
    assert agentconfig.PI_PROVIDER in pi_data["providers"]
    assert agentconfig.OC_PROVIDER in oc_data["provider"]


def test_writing_twice_changes_nothing(tmp_path):
    pi, oc = _dirs(tmp_path)
    first = agentconfig.sync(pi_dir=pi, oc_dir=oc, port=8080)
    assert first["ok"] and first["pi"]["changed"] and first["opencode"]["changed"]
    before = (pi / "models.json").read_bytes(), (oc / "opencode.json").read_bytes()
    second = agentconfig.sync(pi_dir=pi, oc_dir=oc, port=8080)
    assert second["ok"]
    assert not second["pi"]["changed"] and not second["opencode"]["changed"]
    assert (pi / "models.json").read_bytes(), (oc / "opencode.json").read_bytes() == before
    assert not (pi / "models.json.portal-prev").exists(), "an unchanged file gets no backup copy"


def test_a_config_we_cannot_parse_is_left_alone(tmp_path):
    """pi allows comments in models.json. Reproducing a file we cannot read is how a generator
    destroys a hand-written config, so the file is a hard stop."""
    pi, oc = _dirs(tmp_path)
    hand = "// my notes, my file\n{\"providers\": {}}\n"
    (pi / "models.json").write_text(hand)
    out = agentconfig.sync(pi_dir=pi, oc_dir=oc, port=8080)
    assert not out["ok"]
    assert "leaving it alone" in out["error"]
    assert (pi / "models.json").read_text() == hand
    assert not (oc / "opencode.json").exists(), "refusing means refusing both, not half a pair"


def test_a_changed_file_keeps_one_prior_copy(tmp_path):
    pi, oc = _dirs(tmp_path)
    agentconfig.sync(pi_dir=pi, oc_dir=oc, port=8080)
    agentconfig.sync(pi_dir=pi, oc_dir=oc, port=8080, posture="all")
    prev = json.loads((oc / "opencode.json.portal-prev").read_text())
    now = json.loads((oc / "opencode.json").read_text())
    assert prev["permission"] == agentconfig.POSTURES["project"]
    assert now["permission"] == agentconfig.POSTURES["all"]


def test_nothing_is_written_when_the_router_has_nothing_to_say(tmp_path, monkeypatch):
    pi, oc = _dirs(tmp_path)

    def dead(port, timeout=3.0):
        return {"ok": False, "error": "connection refused"}

    monkeypatch.setattr(routerctl, "served_models", dead)
    out = agentconfig.sync(pi_dir=pi, oc_dir=oc, port=8080)
    assert not out["ok"]
    assert "connection refused" in out["error"]
    assert list(pi.iterdir()) == [] and list(oc.iterdir()) == []


def test_an_empty_model_list_is_refused_too(tmp_path, monkeypatch):
    """A harness pointed at a router with no registered models shows an empty picker, which is
    worse than no harness at all — that is the state the operator described fighting."""
    pi, oc = _dirs(tmp_path)

    def empty(port, timeout=3.0):
        return {"ok": True, "count": 0, "loaded": 0, "loaded_ids": [], "aliases": []}

    monkeypatch.setattr(routerctl, "served_models", empty)
    monkeypatch.setattr(agentconfig, "_open", lambda url: json.dumps({"data": []}))
    out = agentconfig.sync(pi_dir=pi, oc_dir=oc, port=8080)
    assert not out["ok"] and "no models" in out["error"]
    assert not (oc / "opencode.json").exists()


# --- reading -----------------------------------------------------------------------------------

def test_status_reads_the_two_files_without_writing(tmp_path):
    pi, oc = _dirs(tmp_path)
    before = sorted(p.name for p in pi.iterdir()), sorted(p.name for p in oc.iterdir())
    st = agentconfig.status(pi_dir=pi, oc_dir=oc, port=8080)
    assert st["pi"]["present"] is False and st["opencode"]["present"] is False
    assert st["port"] == 8080
    assert (sorted(p.name for p in pi.iterdir()), sorted(p.name for p in oc.iterdir())) == before

    agentconfig.sync(pi_dir=pi, oc_dir=oc, port=8080, posture="project-auto")
    st = agentconfig.status(pi_dir=pi, oc_dir=oc, port=8080)
    assert st["pi"]["present"] and st["opencode"]["present"]
    assert st["pi"]["models"] == len(IDS)
    assert st["opencode"]["models"] == len(IDS)
    assert st["opencode"]["posture"] == "project-auto"
    assert st["pi"]["baseUrl"] == "http://127.0.0.1:8080/v1"


def test_the_credential_check_reports_presence_and_never_the_value(tmp_path):
    pi, oc = _dirs(tmp_path)
    (pi / "auth.json").write_text(json.dumps(
        {"llama.cpp": {"type": "api_key", "env": {"LLAMA_BASE_URL": "http://127.0.0.1:8080"}}}, indent=2))
    st = agentconfig.auth_status(pi)
    assert st["entry"] is True and st["key_set"] is False, "the blank-key state, in the operator's own words"

    (pi / "auth.json").write_text(json.dumps(
        {"llama.cpp": {"type": "api_key", "key": agentconfig.STANDIN_KEY}}, indent=2))
    st = agentconfig.auth_status(pi)
    assert st["key_set"] is True
    assert agentconfig.STANDIN_KEY not in json.dumps(st), "a credential check must not echo credentials"
    assert "127.0.0.1" not in json.dumps(st)


def test_the_provider_ids_are_the_ones_the_harnesses_use():
    """pi's built-in id is `llama.cpp` (its docs and its ACP config options both use it); opencode
    gets its own id so a hand-written `llamacpp` provider can coexist with its built-in `llama`."""
    assert agentconfig.PI_PROVIDER == "llama.cpp"
    assert agentconfig.OC_PROVIDER == "llamacpp"
    assert agentconfig.OC_PROVIDER != "llama"


# --- the endpoints (D1: every read is also an endpoint) ----------------------------------------

def _client():
    from fastapi.testclient import TestClient

    from app.core import config as cfg
    from app.main import app
    c = TestClient(app)
    c.cookies.set(cfg.COOKIE_NAME, cfg.make_token(cfg.load()))
    return c


def test_the_status_endpoint_answers_the_page():
    c = _client()
    r = c.get("/api/agents/status")
    assert r.status_code == 200, r.text
    d = r.json()
    for key in ("port", "pi", "opencode", "auth"):
        assert key in d, key
    for key in ("path", "present", "provider"):
        assert key in d["pi"] and key in d["opencode"], key
    for key in ("path", "present", "entry", "key_set"):
        assert key in d["auth"], key


def test_the_write_endpoint_requires_auth():
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)
    assert c.get("/api/agents/status", follow_redirects=False).status_code == 401
    assert c.post("/api/agents/sync", json={"posture": "project"}, follow_redirects=False).status_code == 401


def test_the_write_endpoint_refuses_an_unknown_posture():
    c = _client()
    r = c.post("/api/agents/sync", json={"posture": "yolo"})
    assert r.status_code == 409, r.text
    assert "posture" in r.json()["detail"]
