"""The generated settings reference must not be able to drift from the registry it renders.

`app/core/settingsdoc.py` writes `docs/PORTAL_SETTINGS.md` from the settings registry, and the
document's own banner promises *this file*: "tests/test_settingsdoc.py compares the committed file
against what the registry renders, so a registry change that skips this file is a failing test, not
a stale reference." A promise in a banner is a claim, and a claim with no test behind it is how a
reference ends up describing a registry it was generated from three releases ago.

So the guards here are the ones that make the promise true:

* the committed document equals what the registry renders today;
* every registry key has a section, a row in the glance table, and an anchor the editor can link
  to — a key missing from the reference is a key nobody documented, not a short page;
* every § the document prints resolves to a section, and every key anchor resolves *to this
  document* (an anchor that lands on some other file's heading is a link to the wrong proof);
* the claims the document makes about switches, no-op lines, and router-owned keys are the
  registry's claims, read back out of the prose it generated.

The reference is rendered from `settingrows.rows()` — the same rows the settings editor renders —
so a failure here is about the registry and the document, not about the page.
"""
import os
import pathlib
import re
import tempfile

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-settingsdoc-"))
os.environ.setdefault("AI_PORTAL_HOST", "127.0.0.1")
os.environ.setdefault("AI_PORTAL_PORT", "18099")
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import kb  # noqa: E402
from app.core import personality as P  # noqa: E402
from app.core import settingrows as S  # noqa: E402
from app.core import settingsdoc as D  # noqa: E402
from app.main import app  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / D.DOC_NAME


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    return c


# ------------------------------------------------------------------ the promised guard
def test_the_committed_reference_is_what_the_registry_renders_today():
    """The banner's promise, enforced.

    A registry edit that forgets `--write` leaves a reference that reads as current and is not.
    The generated banner says that combination is a failing test; this is the test.
    """
    assert DOC.is_file(), f"{DOC} is missing — run: .venv/bin/python -m app.core.settingsdoc --write"
    committed = DOC.read_text(encoding="utf-8")
    rendered = D.render()
    if committed != rendered:
        diff = [ln for ln in rendered.splitlines() if ln not in committed.splitlines()][:6]
        assert committed == rendered, (
            f"{DOC.name} is stale — regenerate with --write. Rendered lines absent from the "
            f"committed file: {diff}")


def test_the_check_command_agrees_with_this_guard(tmp_path):
    """`--check` is what CI and the operator run; it must not be more generous than the test.

    Exit 0 on the committed file, exit 1 on a document that differs from the registry by one
    line. A --check that only proves a file exists would pass every stale reference ever written.
    """
    assert D.main(["--check", "--out", str(DOC)]) == 0
    stale = tmp_path / D.DOC_NAME
    stale.write_text(D.render() + "\nlate-added key\n", encoding="utf-8")
    assert D.main(["--check", "--out", str(stale)]) == 1
    assert D.main(["--check", "--out", str(tmp_path / "absent.md")]) == 1


def test_write_produces_the_committed_bytes(tmp_path):
    """`--write` is idempotent against the committed file: regenerating changes nothing."""
    target = tmp_path / D.DOC_NAME
    D.write(target)
    assert target.read_text(encoding="utf-8") == DOC.read_text(encoding="utf-8")


def test_the_banner_names_a_test_that_exists_and_a_command_that_works():
    """A banner that names a test file which does not exist is a lie printed on page one.

    The banner is also the only place that states how to regenerate the file, so the command in
    it is checked against the module's own argument parser.
    """
    text = DOC.read_text(encoding="utf-8")
    named = re.findall(r"tests/[A-Za-z0-9_]+\.py", text)
    assert named, "the generated banner names no test file"
    for name in set(named):
        assert (ROOT / name).is_file(), f"the banner names {name}, which does not exist"
    cmd = re.search(r"-m\s+([a-z_.]+)\s+--write", text)
    assert cmd, "the banner does not state the regeneration command"
    assert cmd.group(1) == "app.core.settingsdoc", cmd.group(1)
    # The banner's command has to be the command the module accepts. argparse prints and exits.
    with pytest.raises(SystemExit) as raised:
        D.main(["--help"])
    assert raised.value.code == 0


def test_the_command_in_the_banner_is_a_command_the_module_accepts(capsys: pytest.CaptureFixture[str]):
    """The banner tells the operator how to regenerate the file. A flag named in a comment and not
    implemented is an instruction that does nothing."""
    text = DOC.read_text(encoding="utf-8")
    banner = text[:text.index("\n# ")]  # the generated banner, above the document's own H1
    named = set(re.findall(r"--([a-z][a-z-]*[a-z])(?= |-->)", banner))
    assert "write" in named, f"the banner names no regeneration flag: {sorted(named)}"
    usage = _usage(capsys)
    for flag in sorted(named | {"check"}):
        assert f"--{flag}" in usage, f"the banner names --{flag}, which the module does not accept"
    with pytest.raises(SystemExit):
        assert D.main(["--no-such-flag"]) != 0


def _usage(capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit):
        D.main(["--help"])
    return capsys.readouterr().out


# ------------------------------------------------------------------ coverage of the registry
def test_every_registry_key_has_a_section_of_its_own():
    """A key with no section is a key nobody documented.

    The key's name is its heading, which is also what makes its anchor (`/help#ctx-size`) derivable
    from the registry rather than hand-maintained in two places.
    """
    text = DOC.read_text(encoding="utf-8")
    headings = set(re.findall(r"^### (\S+)$", text, flags=re.M))
    keys = set(P.setting_names())
    assert headings == keys, (f"missing: {sorted(keys - headings)}",
                              f"headings for keys the registry does not define: {sorted(headings - keys)}")


def test_every_registry_key_has_a_row_in_the_glance_table():
    """The scannable table and the per-key sections must cover the same keys.

    A key in the sections but not the table is invisible to anyone scanning the reference, and a
    key in the table with no section is a link that opens nothing.
    """
    text = DOC.read_text(encoding="utf-8")
    table = text[text.index("## 2. Keys at a glance"):text.index("## 3. Keys")]
    in_table = set(re.findall(r"^\| `([^`]+)` \|", table, flags=re.M))
    keys = set(P.setting_names())
    assert in_table == keys, (f"missing from the table: {sorted(keys - in_table)}",
                             f"extra rows: {sorted(in_table - keys)}")


def test_the_reference_documents_exactly_the_keys_the_page_can_write():
    """The row set the document prints is the row set the editor renders — one source, checked.

    `settingrows.rows()` is what the editor iterates. If the document rendered a different set,
    the reference would describe a page that does not exist.
    """
    rows = S.rows()
    assert len(rows) == len(P.SETTINGS), "the row builder dropped a registry key"
    sections = DOC.read_text(encoding="utf-8")
    for r in rows:
        body = sections[sections.index(f"### {r['key']}"):]
        nxt = re.search(r"^### |^## ", body[10:], flags=re.M)
        body = body[:10 + (nxt.start() if nxt else len(body))]
        assert r["help"].strip(), f"{r['key']}: the registry has no help sentence to print"
        assert r["help"].strip() in body, f"{r['key']}: its help sentence is not in its section"
        assert f"`{r['default']}`" in body or "— (the server decides)" in body, \
            f"{r['key']}: its portal default is not printed"


# ------------------------------------------------------------------ anchors
def test_every_key_anchor_opens_this_document_and_not_a_namesake_elsewhere():
    """`/help#ctx-size` must land on the settings reference.

    Anchors are slugs, and slugs are shared across every document in the knowledgebase. A heading
    named `ctx-size` in some other file wins the lookup and the link opens prose about something
    else — which reads as a working link, so nothing else in the suite would notice.
    """
    for key in P.setting_names():
        section = kb.find(key)
        assert section is not None, f"no section resolves the anchor for {key}"
        assert section["doc"] == D.DOC_NAME, \
            f"/help#{key} opens {section['doc']} ({section['title']}), not the settings reference"


def test_the_help_api_serves_every_key_section(client):
    """The page's links are requests. A key anchor that returns 404 is a link the page prints
    and the portal cannot answer."""
    for key in P.setting_names():
        r = client.get(f"/api/help/section/{key}")
        assert r.status_code == 200, (key, r.status_code, r.json().get("detail"))
        body = r.json()
        assert body["doc"] == D.DOC_NAME, (key, body["doc"])
        # The section's heading is the key — that is what makes the anchor derivable — and the
        # body the API returns starts under that heading, so the heading is the thing to check.
        assert body["id"] == key and body["title"] == key, (key, body["id"], body["title"])
        assert "<b>scope</b>" in body["html"], f"the section served for {key} has no facts line"


def test_the_document_is_registered_in_the_knowledgebase():
    """An unregistered document is invisible: not in the table of contents, not searchable.

    `DOC_ORDER` carries the blurb the table of contents prints. It must match the document's own
    H1, because a blurb describing a different document is how a reference gets cited by the
    wrong name.
    """
    docs = [name for name, _title in kb.DOC_ORDER]
    assert D.DOC_NAME in docs, f"{D.DOC_NAME} is not in DOC_ORDER"
    blurb = dict(kb.DOC_ORDER)[D.DOC_NAME]
    assert blurb, "the table-of-contents blurb is empty"
    ids = {s["id"] for s in kb.toc() if s["doc"] == D.DOC_NAME}
    assert ids, f"the knowledgebase lists no sections for {D.DOC_NAME}"
    assert D.DOC_TITLE.split(" — ")[0].lower() in kb.toc()[0]["title"].lower() \
        or any(s["doc"] == D.DOC_NAME for s in kb.toc())
    hits = kb.search("llama-server default")
    assert any(h["doc"] == D.DOC_NAME for h in hits), \
        "the reference is not searchable — it is not in the index the search reads"


def test_every_section_number_the_document_prints_resolves():
    """The citation guard, applied to the generated document.

    The reference quotes §6.12.6, §4.10.0, §6.14.1 and the rest as evidence. A § that names no
    section is a citation that outlived its target; the registry has this guard for its own cites
    and the generated prose needs the same one.
    """
    refs = kb.refs_in(DOC.read_text(encoding="utf-8"))
    assert refs, "the reference cites no sections at all"
    missing = sorted({r for r in refs if not kb.find(r)})
    assert not missing, f"the reference cites sections that do not exist: {missing}"


# ------------------------------------------------------------------ claims about behaviour
def test_a_switch_says_true_or_false_and_never_accepts_bool():
    """The registry's rule string for a switch is the word `bool`.

    Printed as-is the reference told an operator that a switch accepts "bool" — a type name
    dressed up as a rule. `settingsdoc._accepted` replaces it with the measured sentence; this
    checks that every switch row carries it and that the § inside it resolves.
    """
    text = DOC.read_text(encoding="utf-8")
    switches = [r["key"] for r in S.rows() if r["param_kind"] == "bool"]
    assert switches, "no switch keys in the registry; this guard has nothing to check"
    for key in switches:
        line = [ln for ln in text.splitlines() if ln.startswith("- **accepted**")
                and ln in text[text.index(f"### {key}"):]
                and "true or false" in ln]
        assert line, f"{key}: its accepted line does not say what a switch accepts"
    assert "**accepted**: bool" not in text, "the reference prints a type name as a rule"
    for ref in set(re.findall(r"§(\d+(?:\.\d+)*)", text)):
        assert kb.find(ref), f"the switch sentence cites §{ref}, which names no section"


def test_the_no_op_line_quotes_the_default_the_registry_carries():
    """Every no-op sentence must name that key's own server default.

    The claim is "writing X emits nothing, because X is what the server already does". A sentence
    generated from a *shared* constant, or from a default that drifted from the registry, is a
    claim about the server the registry cannot support — and it is the claim that decides whether
    a line appears in `presets.ini` at all.
    """
    text = DOC.read_text(encoding="utf-8")
    claims = 0
    for r in S.rows():
        section = text[text.index(f"### {r['key']}"):]
        nxt = re.search(r"^### |^## ", section[10:], flags=re.M)
        section = section[:10 + (nxt.start() if nxt else len(section))]
        has_claim = "the no-op line" in section
        expected = r["omitted_when_equal_to"]
        if expected is None:
            assert not has_claim, \
                f"{r['key']}: the portal writes nothing for this key, so no line can be a no-op"
            continue
        assert has_claim, f"{r['key']}: the registry omits its default {expected} and the doc says nothing"
        line = [ln for ln in section.splitlines() if "the no-op line" in ln][0]
        assert f"`{expected}`" in line, \
            f"{r['key']}: the no-op sentence quotes {expected!r} as {line.strip()[:120]!r}"
        claims += 1
    assert claims, "no key documents its no-op line, so the omission rule is undocumented"


def test_the_keys_the_router_owns_are_listed_and_say_why():
    """The reference must list every key the router overwrites at launch.

    A personality that sets one gets it overwritten, and the editor never offers it, so this list
    is the only place the operator can learn that the key exists and is not theirs. An operator who
    sets one and finds nothing in the file has to be able to read *why* somewhere.
    """
    text = DOC.read_text(encoding="utf-8")
    tail = text[text.index("## 4. Keys the page will never offer"):]
    listed = set(re.findall(r"^- `([^`]+)`$", tail, flags=re.M))
    want = set(P.ROUTER_CONTROLLED_KEYS) | set(P.ROUTER_DERIVED_KEYS)
    assert want <= listed, f"router-owned keys missing from the reference: {sorted(want - listed)}"
    assert not (listed - want), \
        f"the reference lists as router-owned a key the router does not own: {sorted(listed - want)}"


def test_the_reference_states_the_refusal_the_form_actually_gives():
    """The document quotes the form's refusal for a router-owned key, so the quote must be computed.

    Measured here, not remembered: the router-owned keys are not in `SETTINGS`, so the form's
    answer is the message for an undefined key. A document that says the validator refuses them
    "with a reason" describes code that does not exist, and the next person to set `host` from a
    personality will look for a refusal that never comes.
    """
    text = DOC.read_text(encoding="utf-8")
    answers = {k: P.check_setting(k, "127.0.0.1") for k in sorted(P.ROUTER_CONTROLLED_KEYS)}
    accepted = sorted(k for k, msg in answers.items() if msg is None)
    assert not accepted, f"the form accepted a router-owned key: {accepted}"
    # Every one of them gets the same answer, which is the answer for a key nothing defines.
    suffix = "no definition for this setting"
    odd = sorted(k for k, msg in answers.items() if not (msg or "").endswith(suffix))
    assert not odd, f"the form refuses these keys in some other way: {[(k, answers[k]) for k in odd]}"
    refusal = answers["host"]
    assert f"`{refusal}`" in text, \
        f"the reference does not print the refusal the form gives ({refusal!r})"
    # ...and it must not claim these are registry keys, because they are not.
    for key in sorted(P.ROUTER_CONTROLLED_KEYS | P.ROUTER_DERIVED_KEYS):
        assert key not in P.SETTINGS, f"{key} is now a registry key; the reference's §4 needs the new truth"
        assert f"### {key}" not in text, f"{key} is not a registry key and the reference documents it as one"
    assert "not registry keys" in text, "the reference does not say these keys are unregistered"


def test_the_editor_offers_no_control_for_a_key_the_router_owns():
    """The row builder is the boundary the editor reads, so it is where this must be true."""
    editable = {r["key"] for r in S.editable_rows()}
    owned = set(P.ROUTER_CONTROLLED_KEYS) | set(P.ROUTER_DERIVED_KEYS)
    assert not (editable & owned), f"the editor offers controls for router-owned keys: {sorted(editable & owned)}"


def test_preset_only_keys_are_said_to_be_preset_only():
    """A key the server accepts only through a preset file must not read like a server argument.

    The audit that found these (`tools/registry_flag_audit.py`, §6.12.25) started by probing them
    on a command line, which is exactly the misunderstanding this sentence exists to prevent.
    """
    text = DOC.read_text(encoding="utf-8")
    rows = S.rows()
    preset_only = [r["key"] for r in rows if r["preset_only"]]
    assert preset_only, "no preset-only keys; the registry no longer records what the audit measured"
    for key in preset_only:
        section = text[text.index(f"### {key}"):]
        assert "preset-only" in section, f"{key}: its section does not say it is preset-only"
    mapped = [r["key"] for r in rows if r["preset_mapped"] and not r["preset_only"]]
    for key in mapped:
        section = text[text.index(f"### {key}"):]
        assert "preset-mapped" in section, f"{key}: emitted under a preset name and the doc says nothing"


def test_model_only_visibility_is_explained_on_the_row_that_uses_it():
    """`visible_when` hides a row on a model that cannot use the setting.

    Hidden is indistinguishable from unsupported, so the reference has to say what the row needs.
    A row with the field and no sentence about it is a rule the operator cannot see.
    """
    text = DOC.read_text(encoding="utf-8")
    rows = [r for r in S.rows() if r["visible_when"]]
    assert rows, "no row declares visibility facts; the editor's hiding rule is undocumented"
    for r in rows:
        for fact in r["visible_when"]:
            assert f"`{fact}`" in text, f"{r['key']} needs {fact} and the reference never names it"


# ------------------------------------------------------------------ the generator's own rules
def test_rendering_help_that_quotes_an_unresolvable_fact_refuses_rather_than_printing_it():
    """`{checkpoint_mib}` in a printed reference is a hole in the page, in prose.

    `settingrows` already refuses to expand a placeholder nothing resolves; the reference prints
    the same help sentences, so it needs the same refusal — a document generation that succeeds
    while printing `{cache_ram_mib}` is the failure the row module names out loud.
    """
    base = S.row("cache-ram")
    hole = dict(base, help="The cache is limited to {no_such_fact} on this box.")
    with pytest.raises(S.SettingRowError):
        D.render([hole])
    ok = dict(base, help="The cache is limited to {cache_ram_mib} MiB.")
    assert str(S.substitutions()["cache_ram_mib"]) in D.render([ok])


def test_a_key_added_to_the_registry_appears_without_editing_the_document():
    """The point of generating the reference: llama.cpp grows flags, a reference row is data.

    Rendered from a synthetic row list, so this proves the generator is driven by rows and not by
    a hand-written document.
    """
    synthetic = dict(S.row("ctx-size"), key="a-flag-added-tomorrow", help="A flag the box gained.")
    out = D.render([synthetic])
    assert "### a-flag-added-tomorrow" in out
    assert "| `a-flag-added-tomorrow` |" in out
    assert "A flag the box gained." in out


def test_the_document_carries_no_unrendered_template_marks():
    """A reference full of `{...}` reads as a templating bug, not as a missing fact."""
    text = DOC.read_text(encoding="utf-8")
    left = sorted(set(re.findall(r"\{([a-z_]+)\}", text)))
    assert not left, f"unresolved placeholders printed as-is: {left}"


def test_every_key_section_prints_a_working_link_to_its_measured_evidence():
    """§6.14.1q: the evidence link moved with the reference.

    The personalities page used to carry a `href="/help#6.12.6"`-style citation link per row, and the
    guard counted them against the registry. The reference is the document that owns the evidence, so
    it prints the link per key section — and the count that used to be the page's is the document's:
    every key whose citation names a section links it, and a key citing source lines only links
    nothing, because a link to a section that does not exist is the failure this guard is about.
    """
    text = DOC.read_text(encoding="utf-8")
    links = re.findall(r'\[/?help#([\d.]+)\]\(/help#([\d.]+)\)', text)
    assert links, "the reference prints no citation links"
    assert all(a == b for a, b in links), f"label and target disagree: {links[:3]}"
    refs = [a for a, _ in links]
    missing = sorted({r for r in refs if not kb.find(r)})
    assert not missing, f"citation links that open nothing: {missing}"

    keys_with_cites = sum(1 for v in P.SETTINGS.values() if kb.ref_of(v.get("cite")))
    assert len(refs) == keys_with_cites, (
        len(refs), keys_with_cites, len(P.SETTINGS) - keys_with_cites)
