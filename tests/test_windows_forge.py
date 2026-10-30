"""The Windows forge kit ships to a box we do not control, running
Windows PowerShell 5.1. Three run failures on RainbowBlock.local taught
the kit's hard rules (journal 6.14.1p): UTF-8 punctuation arrives as ANSI
mojibake and breaks quoting; PS7-only syntax (ternary, null-coalescing)
is a parse error on 5.1; multi-line expressions inside throw(...) break
under 5.1's parser. The rules are ASCII and 5.1-safety - this file keeps
them true for every future edit, and pins the parse preflight the owner
runs first on the target box."""
from __future__ import annotations

import json
import re
from pathlib import Path

KIT = Path(__file__).resolve().parents[1] / "deploy" / "windows-forge"

# PS7-only syntax that Windows PowerShell 5.1 cannot parse. The ternary
# pattern needs a real operand context ('x ? a : b'); a lone '?' in prose
# inside a string is caught by the owner's parse preflight anyway.
PS7_ONLY = [
    re.compile(r"\)\s*\?|\]\s*\?|\w\s*\?\s*[^:\s]", re.MULTILINE),  # 'a ? b : c'
    re.compile(r"\?\?"),                                             # null-coalescing
    re.compile(r"-\?|\$\{[^}]*\?\}"),                                # ternary in interpolation
]


def scripts():
    return sorted(KIT.glob("*.ps1"))


def test_the_kit_is_pure_ascii():
    """5.1 reads scripts as ANSI: a UTF-8 em-dash arrives as three
    mojibake bytes, breaks string quoting, cascades into parse errors
    (owner's first run). ASCII parses identically under any code page -
    and no BOM either, the kit rule is plain bytes."""
    assert scripts(), "the kit has no .ps1 files"
    for ps1 in scripts():
        data = ps1.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), f"{ps1.name}: BOM is not plain ASCII bytes"
        bad = [i for i, b in enumerate(data) if b > 127]
        assert not bad, (
            f"{ps1.name}: {len(bad)} non-ASCII byte(s) at {bad[:5]} - "
            "see README 'File encoding'; keep the kit pure ASCII")


def test_the_kit_parses_under_powershell_5_1():
    """No PS7-only constructs: 5.1 chokes on the ternary and '??' with
    'Unexpected token' (owner's second run). The ternary net also catches
    encoding fossils - a '§' that an ASCII pass scrubbed to a bare '?'
    (found in start_comfyui.ps1's header comment): plain ASCII, but it
    reads as mojibake and the kit rule's whole point is readable files."""
    for ps1 in scripts():
        text = ps1.read_text()
        for pat in PS7_ONLY:
            m = pat.search(text)
            assert not m, (
                f"{ps1.name}: PS7-only construct {m.group(0)!r} - "
                "the target shell is Windows PowerShell 5.1")


def test_the_parse_preflight_ships_with_the_kit():
    """The owner runs parse_check.ps1 FIRST on RainbowBlock.local: it
    reports every parse failure (per line) before any installer touches
    the disk. It must parse files via the PowerShell parser - not
    execute them - and the README must tell the owner to run it.

    The parser type is pinned by name: the modern surface is
    Language.Parser::ParseInput (v3+); the legacy PSParser class only
    has Tokenize, and calling ParseInput on it is 'MethodNotFound' -
    which is how this script itself first failed on the target box."""
    pc = KIT / "parse_check.ps1"
    assert pc.is_file(), "the parse preflight must ship in the kit"
    text = pc.read_text()
    assert "Language.Parser]::ParseInput" in text, (
        "ParseInput lives on Language.Parser; PSParser has only Tokenize")
    assert "PSParser]::ParseInput" not in text, "the legacy PSParser has no ParseInput"
    assert "$($PSVersionTable.PSVersion)" in text, (
        "interpolating the whole $PSVersionTable prints 'Hashtable', not a version")
    assert "Extent.StartLineNumber" in text, (
        "ParseError carries Extent.StartLineNumber (the legacy Token.StartLine does not)")
    assert "Get-ChildItem" in text and "*.ps1" in text, "it must sweep every .ps1"
    assert re.search(r"exit\s+1", text), "parse failures must reach a nonzero exit code"
    readme = (KIT / "README.md").read_text()
    assert "parse_check.ps1" in readme, "the steps must tell the owner to run the preflight"


AGENT = Path(__file__).resolve().parents[1] / "deploy" / "windows-agent"


def test_the_agent_config_declares_the_router_lan_safe():
    """opencode ships no llama.cpp provider and needs none: a llama.cpp
    server speaks the OpenAI wire, so the kit DECLARES one
    (@ai-sdk/openai-compatible -> the router's /v1). Pins: the LAN IP
    (names freeze at resolution time), the placeholder apiKey (the router
    is unauthenticated; no secret material in any kit file), the default
    model is one the provider offers, and the 84 GB resident MoE
    (IQ3_S - the controller's own brain) is absent: requesting it evicts
    the portal harness that governs this box."""
    cfg = json.loads((AGENT / "opencode.json").read_text())
    prov = cfg["provider"]["llamarouter"]
    assert prov["npm"] == "@ai-sdk/openai-compatible"
    assert prov["options"]["baseURL"] == "http://192.168.1.228:8080/v1"
    assert prov["options"]["apiKey"] == "local", "placeholder only - no secrets in the kit"
    assert cfg["model"].startswith("llamarouter/")
    assert cfg["model"].split("/", 1)[1] in prov["models"], "default model must be declared"
    blob = json.dumps(cfg)
    assert "IQ3_S" not in blob, "the resident MoE stays out of the agent config (eviction)"
    assert "halo.local" not in blob, "pin the static lease IP, not the name"


def test_the_agent_brief_carries_the_hard_rules():
    """AGENTS.md is the 'session/instructions' the owner asked for: the
    mission, the kit steps, and every rule the kit learned the expensive
    way. The test pins the rules that cost real runs."""
    text = (AGENT / "AGENTS.md").read_text()
    for phrase in ("parse_check.ps1", "pure ASCII", "PowerShell 5.1",
                   "Language.Parser", "IQ3_S", "copy_from_halo",
                   "192.168.1.228", "Definition of done",
                   "Never delete model weights"):
        assert phrase in text, f"the mission brief lost {phrase!r}"


def test_the_link_checker_is_5_1_safe():
    """check_agent_link.ps1 proves the provider is reachable from Windows:
    TCP 8080 + /v1/models. Same kit rules: ASCII, no PS7 syntax - and the
    ${portal} form, because '$portal:' inside a string parses as a drive
    qualifier."""
    ps1 = AGENT / "check_agent_link.ps1"
    assert ps1.is_file(), "the link checker must ship with the agent kit"
    data = ps1.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert not [i for i, b in enumerate(data) if b > 127], "the link checker must stay pure ASCII"
    text = data.decode()
    for pat in PS7_ONLY:
        m = pat.search(text)
        assert not m, f"link checker: PS7-only construct {m.group(0)!r}"
    assert "${portal}" in text, "bare '$portal:' in a string is the drive-qualifier trap"
    assert "Test-NetConnection" in text and "Invoke-RestMethod" in text
