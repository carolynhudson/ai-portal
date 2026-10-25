"""Network exposure: bind modes, live probes, firewall hints, the bind API,
login throttling, and the router API-key + generated-launch-config plumbing.

Everything runs against a throwaway ``AI_PORTAL_HOME``; the only host reads
are ``ip``/``ss``/socket probes, and the only writes land in the temp tree (the
systemctl call inside units.install_unit is stubbed).
"""
import os
import socket
import tempfile

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-net-"))
os.environ.setdefault("AI_PORTAL_HOST", "127.0.0.1")
os.environ.setdefault("AI_PORTAL_PORT", "18093")
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import llamakey, network, ratelimit, serviceconf, units  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.llamacpp import LlamaCpp, router_argv  # noqa: E402


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    return c


@pytest.fixture
def free_port():
    s = socket.socket()
    s.bind((network.ALL, 0))
    s.listen(1)
    yield s.getsockname()[1]
    s.close()


# ------------------------------------------------------------------ network
def test_bind_mode_classification():
    assert network.bind_mode("127.0.0.1") == "loopback"
    assert network.bind_mode("::1") == "loopback"
    assert network.bind_mode(network.ALL) == "all"
    assert network.bind_mode("::") == "all"
    assert network.bind_mode(None) == "all"        # unset must not read as loopback
    assert network.bind_mode("192.168.5.5") == "lan"


def test_resolve_bind_roundtrip():
    assert network.resolve_bind("loopback") == "127.0.0.1"
    assert network.resolve_bind("all") == network.ALL
    assert network.resolve_bind("lan", "10.1.2.3") == "10.1.2.3"
    with pytest.raises(ValueError):
        network.resolve_bind("public")
    modes = list(network.MODES)
    if not network.primary_lan():
        modes.remove("lan")        # nothing to bind to; see the next test
    for mode in modes:
        assert network.bind_mode(network.resolve_bind(mode)) == mode


@pytest.mark.skipif(bool(network.interfaces()), reason="a LAN interface exists here")
def test_lan_mode_without_an_interface_is_an_error_not_a_wide_open_fallback():
    """'lan' must never quietly become 0.0.0.0."""
    with pytest.raises(ValueError):
        network.resolve_bind("lan")


def test_interfaces_shape_and_primary():
    ifs = network.interfaces()
    assert isinstance(ifs, list)
    for it in ifs:
        assert {"name", "address", "scope", "up", "virtual", "lan", "default"} <= set(it)
    # loopback is never a LAN candidate; a LAN candidate needs a real address
    lo = [i for i in ifs if i["name"] == "lo"]
    assert lo and lo[0]["lan"] is False
    for i in ifs:
        if i["lan"]:
            assert i["scope"] == "global" and not i["virtual"]
    primary = network.primary_lan()
    if primary:
        assert primary["lan"] is True
        assert network.is_local_address(primary["address"])


def test_endpoint_urls_follow_the_bind():
    lo = network.endpoint_urls("127.0.0.1", 8080)
    assert all(u["reachable_from"] == "local" for u in lo)
    assert len(lo) == 1
    every = network.endpoint_urls(network.ALL, 8080)
    lan = [u for u in every if u["reachable_from"] == "lan"]
    if network.primary_lan():
        assert lan, "an all-interfaces bind must advertise the LAN address"
        lan0 = network.primary_lan()
    assert lan0 is not None
    assert lan[0]["url"].startswith(f"http://{lan0['address']}:8080")


def test_listening_on_honours_wildcard_bind(free_port):
    assert network.listening_on("127.0.0.1", free_port) is True
    assert network.bound_addresses(free_port) is not None
    assert network.listening_on("127.0.0.1", free_port + 1) is False


def test_check_reachable_reports_failure_reason():
    res = network.check_reachable("127.0.0.1", 1)   # nothing bound, privileged port
    assert res["ok"] is False and res["error"]
    assert (res["host"], res["port"]) == ("127.0.0.1", 1)


def test_firewall_status_is_best_effort():
    st = network.firewall_status()
    assert "enabled" in st and "detail" in st
    assert st.get("backend") in (None, "ufw")


def test_allow_command_is_scoped_to_the_lan():
    cmd = network.allow_command(8080, network="192.168.1.0/24", comment="llama.cpp router")
    assert cmd.startswith("sudo ufw allow from 192.168.1.0/24")
    assert "port 8080 proto tcp" in cmd
    assert "comment 'llama.cpp router'" in cmd
    assert network.allow_command(1, network=None).count("from") == 1


# -------------------------------------------------------------- serviceconf
def test_env_defaults_are_seeded():
    """A first-ever env file gets host AND port (a bind-only write used to
    leave the service with port 0)."""
    for svc, conf in serviceconf.SERVICE_CONF.items():
        f = conf["env_file"]
        f.unlink(missing_ok=True)
        env = serviceconf.ensure_defaults(svc)
        assert env[conf["host_key"]] == conf["defaults"]["host"], svc
        assert env[conf["port_key"]] == str(conf["defaults"]["port"]), svc
        assert f.read_text().count("=") >= 2, f"{svc}: file was not written"


def test_ensure_defaults_keeps_existing_values():
    conf = serviceconf.SERVICE_CONF["llamacpp"]
    serviceconf.write_service_env("llamacpp", {conf["host_key"]: "10.0.0.1",
                                               conf["port_key"]: "7777",
                                               "LLAMA_ARGS": "--keep-me 1"})
    env = serviceconf.ensure_defaults("llamacpp")
    assert env[conf["host_key"]] == "10.0.0.1" and env[conf["port_key"]] == "7777"
    assert env["LLAMA_ARGS"] == "--keep-me 1", "must not drop unknown keys"


def test_set_bind_modes_and_persistence():
    for mode, host in (("loopback", "127.0.0.1"), ("all", "0.0.0.0")):
        res = serviceconf.set_bind("llamacpp", mode)
        assert res["ok"] and res["host"] == host, res
        assert serviceconf.service_ports()["llamacpp"]["host"] == host
        assert res["needs_restart"] is True
        # the env file the unit actually reads carries the value
        assert serviceconf.read_service_env("llamacpp")["LLAMA_HOST"] == host
    # unknown modes are rejected before any write
    with pytest.raises(ValueError):
        serviceconf.set_bind("llamacpp", "internet")


@pytest.mark.skipif(not network.primary_lan(), reason="no LAN interface here")
def test_set_bind_lan_uses_primary_interface():
    res = serviceconf.set_bind("portal", "lan")
    lan = network.primary_lan()
    assert lan is not None
    assert res["ok"] and res["host"] == lan["address"]
    assert res["mode"] == "lan"
    serviceconf.set_bind("portal", "all")


def test_set_bind_rejects_foreign_address():
    res = serviceconf.validate_bind("portal", "203.0.113.9")
    assert res["ok"] is False and res["errors"]
    with pytest.raises(ValueError):
        serviceconf.set_bind("portal", "lan", "203.0.113.9")


def test_opening_a_no_auth_service_warns():
    # ComfyUI has no auth at all: opening it must produce a warning, never an
    # error — the operator decides, the portal only states the consequence.
    assert serviceconf.SERVICE_CONF["comfyui"]["auth_kind"] == "none"
    res = serviceconf.set_bind("comfyui", "all")
    assert res["ok"] and res["warnings"]
    v = serviceconf.validate_bind("comfyui", "127.0.0.1")
    assert v["ok"] and not v["warnings"], "loopback needs no exposure warning"


def test_comfyui_json_config_follows_the_bind():
    """comfyui.json is the provider's launch-config source of truth; a bind
    change must be written there too or the next ensure_service() would revert
    the env file (the silent-revert bug this guards)."""
    pytest.importorskip("app.providers.comfyui")
    from app.providers import comfyui as comfyui_mod
    serviceconf.set_bind("comfyui", "all")
    assert comfyui_mod.ComfyUI().config()["listen"] == "0.0.0.0"
    serviceconf.set_bind("comfyui", "loopback")
    assert comfyui_mod.ComfyUI().config()["listen"] == "127.0.0.1"


def test_legacy_external_flag_still_works_and_warns():
    res = serviceconf.set_service_port("llamacpp", port=8123, external=True)
    assert res["ok"] and res["host"] == "0.0.0.0" and res["mode"] == "all"
    res = serviceconf.set_service_port("llamacpp", external=False)
    assert res["ok"] and res["host"] == "127.0.0.1" and res["port"] == 8123
    # port survives a bind-only change (regression: env file rewrite)
    assert serviceconf.port_of("llamacpp") == 8123


def test_exposure_report_shape():
    serviceconf.set_bind("portal", "all")
    exp = serviceconf.exposure()
    assert set(exp) == set(serviceconf.SERVICE_CONF)
    for key, e in exp.items():
        assert {"host", "port", "mode", "urls", "lan_probe", "listening_lan",
                "pending_restart", "firewall", "allow_command", "unit_active"} <= set(e)
    p = exp["portal"]
    assert p["urls"], "all-interfaces bind must list candidate URLs"
    assert (p["allow_command"] is None) or "port 8090" in p["allow_command"]
    one = serviceconf.exposure("comfyui")
    assert set(one) == {"comfyui"}


def test_probe_host_uses_loopback_unless_lan_only():
    serviceconf.set_bind("comfyui", "all")
    assert serviceconf.probe_host("comfyui") == "127.0.0.1"
    serviceconf.set_bind("comfyui", "loopback")
    assert serviceconf.probe_host("comfyui") == "127.0.0.1"
    lan = network.primary_lan()
    if lan:
        serviceconf.set_bind("comfyui", "lan")
        assert serviceconf.probe_host("comfyui") == lan["address"]
        # base_url must be usable by the health probes
        assert serviceconf.base_url("comfyui", 8188).startswith("http://")
    serviceconf.set_bind("comfyui", "loopback")


# ------------------------------------------------------------------ the API
def test_network_endpoint_shape(client):
    r = client.get("/api/network")
    assert r.status_code == 200
    body = r.json()
    assert {"interfaces", "primary_lan", "lan_network", "firewall",
            "services", "router_auth", "modes"} <= set(body)
    assert set(body["services"]) == set(serviceconf.SERVICE_CONF)


def test_exposure_endpoint(client):
    assert client.get("/api/exposure").status_code == 200
    assert client.get("/api/exposure", params={"service": "comfyui"}).json().keys() == {"comfyui"}
    assert client.get("/api/exposure", params={"service": "nope"}).status_code == 404


def test_bind_endpoint_requires_auth():
    c = TestClient(app)
    assert c.post("/api/settings/services/portal/bind",
                  json={"bind": "all"}).status_code == 401


def test_bind_endpoint_validation_paths(client):
    assert client.post("/api/settings/services/nope/bind",
                       json={"bind": "all"}).status_code == 404
    r = client.post("/api/settings/services/portal/bind", json={})
    assert r.status_code == 400
    r = client.post("/api/settings/services/portal/bind", json={"bind": "bogus"})
    assert r.status_code == 400
    r = client.post("/api/settings/services/portal/bind",
                    json={"bind": "lan", "address": "203.0.113.9"})
    assert r.status_code == 422 or r.status_code == 400


def test_bind_endpoint_applies_loopback(client):
    r = client.post("/api/settings/services/portal/bind", json={"bind": "loopback"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["host"] == "127.0.0.1" and body["mode"] == "loopback"
    assert serviceconf.read_service_env("portal")["AI_PORTAL_HOST"] == "127.0.0.1"


def test_router_restart_via_bind_requires_confirm(client):
    # the router is the one unit whose restart drops agent sessions
    r = client.post("/api/settings/services/llamacpp/bind",
                    json={"bind": "loopback", "restart": True})
    assert r.status_code == 400
    assert "confirm" in str(r.json())


def test_harness_bind_offers_loopback_and_all_only():
    """The opencode host's modes are constrained (owner Oct-25 asked for LAN
    access): `lan` binds ONE interface address, which moves the endpoint off
    127.0.0.1 — and the portal's own client to the harness is loopback-pinned
    (test_the_client_never_leaves_loopback), so a lan bind would cut the portal
    off the host the Projects page launches sessions on."""
    ports = serviceconf.service_ports()["harness"]
    assert ports["modes"] == ["loopback", "all"]
    with pytest.raises(ValueError) as e:
        serviceconf.set_bind("harness", "lan")
    assert "loopback" in str(e.value), "the refusal must say what is allowed and why"
    assert serviceconf.read_service_env("harness")["HARNESS_HOST"] == "127.0.0.1", \
        "a refused bind must not have touched the env"


def test_harness_bind_moves_the_host_and_keeps_the_rest_of_the_env(client):
    """harness.env carries the server password and the binary path alongside
    the host; the bind write moves ONLY HARNESS_HOST (the wrapper execs
    `--hostname $HARNESS_HOST`, so exposure is this value)."""
    conf = serviceconf.SERVICE_CONF["harness"]
    serviceconf.write_service_env("harness", {
        conf["host_key"]: "127.0.0.1", conf["port_key"]: "4096",
        "OPENCODE_SERVER_PASSWORD": "s3cret", "HARNESS_BIN": "/x/opencode"})
    r = client.post("/api/settings/services/harness/bind", json={"bind": "all"})
    assert r.status_code == 200, r.text
    assert r.json()["host"] == "0.0.0.0" and r.json()["mode"] == "all"
    env = serviceconf.read_service_env("harness")
    assert env["OPENCODE_SERVER_PASSWORD"] == "s3cret", \
        "the bind write must not strip the secret the wrapper needs"
    assert env["HARNESS_BIN"] == "/x/opencode"
    # and back home
    assert client.post("/api/settings/services/harness/bind",
                       json={"bind": "loopback"}).json()["host"] == "127.0.0.1"


def test_harness_restart_via_bind_requires_confirm(client):
    # the harness restart ends a running turn (sessions persist) — same gate
    # shape as the router's, its own reason
    r = client.post("/api/settings/services/harness/bind",
                    json={"bind": "loopback", "restart": True})
    assert r.status_code == 400
    assert "confirm" in str(r.json())


def test_settings_services_payload_carries_harness_modes(client):
    """The page renders exactly the payload's modes; an endpoint that offered
    what it would refuse is the bug this pins."""
    body = client.get("/api/settings/services").json()
    h = body["services"]["harness"]
    assert h["modes"] == ["loopback", "all"]
    assert h["label"] == "opencode harness host"
    assert "HTTP Basic" in h["auth"] and h["code_exec"] is True


def test_dashboard_shows_who_can_reach_each_service(client):
    """The heroes must state the bind: 'can other systems reach this?' is the
    question an operator looks at first, and it must not be guessed."""
    from app import router as router_mod
    serviceconf.set_bind("llamacpp", "all")
    serviceconf.set_bind("comfyui", "loopback")
    router_mod._cache = (0.0, {})      # the hero caches for 2 s
    r = client.get("/")
    assert r.status_code == 200
    assert "open to LAN" in r.text, "router hero must show it is listening on the LAN"
    assert "loopback only" in r.text, "ComfyUI hero must show it is local-only"
    assert "/settings" in r.text


# -------------------------------------------------------------- login throttle
def test_login_throttles_repeated_failures():
    ratelimit.reset()
    c = TestClient(app)
    codes = [c.post("/login", data={"password": "wrong"},
                    follow_redirects=False).status_code
             for _ in range(ratelimit.MAX_FAILURES)]
    assert set(codes) == {401}
    blocked = c.post("/login", data={"password": "wrong"}, follow_redirects=False)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    # api login is throttled too, and a good password still works once clear
    assert c.post("/api/login", json={"password": "wrong"}).status_code == 429
    ratelimit.reset()
    assert c.post("/api/login", json={"password": "test-password-123"}).status_code == 200


def test_successful_login_clears_the_counter():
    ratelimit.reset()
    c = TestClient(app)
    for _ in range(ratelimit.MAX_FAILURES - 1):
        c.post("/login", data={"password": "wrong"}, follow_redirects=False)
    assert c.post("/login", data={"password": "test-password-123"},
                  follow_redirects=False).status_code == 302
    assert ratelimit.allow("testclient")[0] is True
    ratelimit.reset()


# ------------------------------------------------------- router api key + argv
def test_router_key_lifecycle():
    llamakey.revoke()
    assert llamakey.configured() is False and llamakey.reveal() is None
    res = llamakey.generate()
    assert res["ok"] and res["created"] and res["key"]
    assert llamakey.configured() is True
    assert llamakey.reveal() == res["key"]
    assert llamakey.request_headers()["Authorization"].startswith("Bearer ")
    # 0600: the key file must not be group/world readable
    assert llamakey.KEY_FILE.stat().st_mode & 0o077 == 0
    # the env file carries only the *path*, never the secret
    env = serviceconf.read_service_env("llamacpp")
    assert env[llamakey.ENV_KEY] == str(llamakey.KEY_FILE)
    assert res["key"] not in open(serviceconf.SERVICE_CONF["llamacpp"]["env_file"]
                                 ).read()
    # idempotent without force, rotating replaces it
    assert llamakey.generate()["created"] is False
    rotated = llamakey.generate(force=True)
    assert rotated["created"] and rotated["key"] != res["key"]
    assert llamakey.revoke()["file_removed"] is True
    assert llamakey.configured() is False
    assert llamakey.request_headers() == {}


def test_router_argv_tracks_the_env_file():
    llamakey.revoke()
    serviceconf.write_service_env("llamacpp", {
        "LLAMA_BIN": "/tmp/fake/llama-server", "LLAMA_HOST": "0.0.0.0",
        "LLAMA_PORT": "9090", "LLAMA_METRICS": "1", "LLAMA_MODELS_MAX": "2",
        "LLAMA_MODELS_DIR": "/models", "LLAMA_PRESETS": "/p.ini",
        "LLAMA_ARGS": "--parallel 2 --cont-batching"})
    argv = router_argv()
    assert argv[:5] == ["/tmp/fake/llama-server", "--host", "0.0.0.0",
                        "--port", "9090"]
    assert "--metrics" in argv
    i = argv.index("--models-max")
    assert argv[i:i + 5] == ["--models-max", "2", "--models-dir", "/models",
                             "--models-preset"]
    assert argv[-3:] == ["--parallel", "2", "--cont-batching"], \
        "free-form LLAMA_ARGS are appended, word-split"

    # a key file adds --api-key-file; a missing one must not add a dangling arg
    llamakey.KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    llamakey.KEY_FILE.write_text("secret-key\n")
    serviceconf.write_service_env("llamacpp",
                                  {**serviceconf.read_service_env("llamacpp"),
                                   llamakey.ENV_KEY: str(llamakey.KEY_FILE)})
    argv = router_argv()
    i = argv.index("--api-key-file")
    assert argv[i + 1] == str(llamakey.KEY_FILE)
    llamakey.revoke()
    assert "--api-key-file" not in router_argv()
    assert serviceconf.read_service_env("llamacpp")[
        "LLAMA_ARGS"] == "--parallel 2 --cont-batching"


def test_ensure_service_generates_a_working_wrapper():
    """The generated wrapper must produce exactly the argv the portal computes.

    This is the safety net for the launch-config change: it proves the wrapper
    is valid and complete without exec'ing llama-server —
    ``AI_PORTAL_PRINT_ARGV=1`` makes it print its argv and exit. Everything
    lands in the throwaway AI_PORTAL_HOME (unit file included); the
    ``systemctl --user daemon-reload`` inside install_unit reads the real unit
    directory, which this test does not modify.
    """
    serviceconf.write_service_env("llamacpp", {
        "LLAMA_BIN": "/bin/echo", "LLAMA_HOST": "127.0.0.1", "LLAMA_PORT": "8080",
        "LLAMA_METRICS": "1", "LLAMA_MODELS_MAX": "1",
        "LLAMA_MODELS_DIR": "/models", "LLAMA_PRESETS": "/p.ini", "LLAMA_ARGS": ""})
    res = LlamaCpp().ensure_service(log=lambda m: None)
    assert res["wrapper"] and os.path.isfile(res["wrapper"])
    assert os.access(res["wrapper"], os.X_OK)
    assert res["unit_file"] and os.path.isfile(res["unit_file"])
    unit_text = open(res["unit_file"]).read()
    assert "EnvironmentFile=" in unit_text and res["wrapper"] in unit_text
    assert "EnvironmentFile=" in unit_text and "ai-env.env" in unit_text
    verify = res["verify"]
    assert verify["ok"] is True, verify
    assert verify["argv"] == verify["expected"]
    assert verify["argv"] == router_argv(), "wrapper and portal must agree"
    assert verify["argv"][:2] == ["/bin/echo", "--host"]


def test_wrapper_verify_reports_missing_wrapper(monkeypatch, tmp_path):
    """A verification against a not-yet-generated wrapper must be an explicit
    error, never an exception or a false 'ok'."""
    from app.providers import llamacpp as lp
    monkeypatch.setattr(units, "wrapper_path",
                        lambda name: tmp_path / "ai-llamacpp.sh")
    res = lp.verify_wrapper_argv()
    assert res["ok"] is False
    assert "not installed" in res["error"]


def test_wrapper_verify_reports_a_broken_wrapper(monkeypatch, tmp_path):
    bad = tmp_path / "ai-llamacpp.sh"
    bad.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    bad.chmod(0o755)
    from app.providers import llamacpp as lp
    monkeypatch.setattr(units, "wrapper_path", lambda name: bad)
    res = lp.verify_wrapper_argv()
    assert res["ok"] is False and "boom" in res["error"]


# --------------------------------- the bind report joined with the auth report
def test_an_open_port_and_a_missing_key_are_reported_together():
    """The two facts come from different files and drift apart on their own.

    A router that was opened on purpose in one month and whose key was revoked in the next is not a
    configuration any page displays as a single decision, unless the report makes it one.
    """
    open_no_key = serviceconf.auth_state("llamacpp", key_configured=False)
    assert open_no_key["kind"] == "optional"
    assert open_no_key["key_configured"] is False
    assert "no API key set" in open_no_key["warning"]

    open_with_key = serviceconf.auth_state("llamacpp", key_configured=True)
    assert open_with_key["warning"] is None


def test_the_service_with_no_auth_at_all_never_reports_itself_as_safe():
    """ComfyUI runs arbitrary Python through /prompt; there is no key to set."""
    comfy = serviceconf.auth_state("comfyui", key_configured=True)
    assert comfy["kind"] == "none"
    assert "no authentication at all" in comfy["warning"]
    assert serviceconf.auth_state("portal", key_configured=None)["warning"] is None


def test_the_exposure_endpoint_carries_auth_on_every_service(client):
    report = client.get("/api/exposure").json()
    for key, entry in report.items():
        assert entry["auth"]["kind"] == serviceconf.SERVICE_CONF[key]["auth_kind"]
    assert report["llamacpp"]["auth"]["key_configured"] in (True, False)
