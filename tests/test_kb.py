"""The knowledge base: the docs a setting cites must be findable from the setting.

The registry explains a key briefly and cites the measurement that earned it ("§6.12.6"). Those
citations were text pointing at a 215 KB file nobody opens from the page they are reading.
`app/core/kb.py` makes them real: sections parsed from docs/, addressed by the number the heading
carries, searchable, linked from the settings pages.

The test that matters most here is the citation guard: a § that resolves to no section is a
citation that has quietly outlived its target, and nothing else in the suite would notice.
"""
import os
import pathlib
import tempfile
import time

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-kb-"))
os.environ.setdefault("AI_PORTAL_HOST", "127.0.0.1")
os.environ.setdefault("AI_PORTAL_PORT", "18099")
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import kb  # noqa: E402
from app.core import personality as P  # noqa: E402
from app.main import app  # noqa: E402

DOCS = pathlib.Path(__file__).resolve().parents[1] / "docs"


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    return c


def _fixture_docs(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "docs"
    d.mkdir()
    (d / "PORTAL_PLAN.md").write_text(
        "# Title\n\nintro line\n\n"
        "## 1. First\n\nbody of one\n\n"
        "### 1.2 Nested — the deep one\n\nnested body mentioning kv_unified\n\n"
        "#### 1.2.3 Deeper\n\ndeepest body\n\n"
        "## 2. Second\n\nbody of two with a §1.2 citation\n\n"
        "## Naming has no number\n\nnamed body\n",
        encoding="utf-8")
    return d


# ---------------------------------------------------------------- the citation guard
def test_every_citation_in_the_settings_registry_resolves(tmp_path):
    """A setting may not point at a section that does not exist.

    The registry is the only place a reader learns what a key does, and its citations are how a
    short answer stays honest. A citation that names nothing is worse than no citation: it reads
    like evidence.
    """
    cited = set()
    for spec in P.SETTINGS.values():
        for field in ("help", "side_effects"):
            cited.update(kb.refs_in(spec.get(field, "")))
    dangling = sorted(c for c in cited if kb.find(c, docs_dir=DOCS) is None)
    assert not dangling, (
        f"{len(dangling)} citation(s) in the registry name no section in docs/: {dangling}. "
        "Add the section, or drop the citation -- do not leave a pointer to nothing.")


def test_citations_in_the_page_templates_resolve_too():
    """Templates carry prose citations as well; the same rule applies to what ships as HTML."""
    web = pathlib.Path(__file__).resolve().parents[1] / "web" / "templates"
    cited = set()
    for p in sorted(web.glob("*.html")):
        cited.update(kb.refs_in(p.read_text(encoding="utf-8", errors="replace")))
    dangling = sorted(c for c in cited if kb.find(c, docs_dir=DOCS) is None)
    assert not dangling, f"templates cite sections that do not exist: {dangling}"


def test_a_cited_number_names_the_section_it_was_written_for():
    """`find` returns the section the number belongs to, not a neighbour."""
    s = kb.find("§4.10", docs_dir=DOCS)
    assert s is not None and s["id"] == "4.10"
    deeper = kb.find("4.10.1", docs_dir=DOCS)
    assert deeper is not None and deeper["id"] == "4.10.1"
    assert deeper["doc"] == s["doc"]


# ---------------------------------------------------------------- ids and bodies
def test_section_ids_come_from_the_heading_text(tmp_path):
    """No hand-maintained id table: the number in the heading is the address."""
    rows = kb.sections(_fixture_docs(tmp_path))
    ids = [r["id"] for r in rows]
    assert ids == ["title", "1", "1.2", "1.2.3", "2", "naming-has-no-number"], ids


def test_text_before_the_first_heading_is_not_dropped(tmp_path):
    """A file that opens with prose still has that prose in the index."""
    d = tmp_path / "d2"
    d.mkdir()
    (d / "PORTAL_PLAN.md").write_text("lead paragraph\n\n## 3. Later\n\nbody\n", encoding="utf-8")
    rows = kb.sections(d)
    assert rows[0]["id"] == "preamble" and "lead paragraph" in rows[0]["body"]


def test_a_body_stops_at_the_next_heading(tmp_path):
    """A section contains its own prose, not its subsection's."""
    rows = {r["id"]: r for r in kb.sections(_fixture_docs(tmp_path))}
    assert "body of one" in rows["1"]["body"]
    assert "nested body" not in rows["1"]["body"]
    assert "deepest body" in rows["1.2.3"]["body"]
    assert "§1.2 citation" in rows["2"]["body"]


def test_an_unnumbered_heading_still_gets_an_address(tmp_path):
    rows = {r["title"]: r for r in kb.sections(_fixture_docs(tmp_path))}
    assert rows["Naming has no number"]["id"] == "naming-has-no-number"


def test_a_changed_file_is_reparsed(tmp_path):
    """The cache is keyed on mtime+size; a stale parse would serve deleted sections."""
    d = _fixture_docs(tmp_path)
    assert kb.find("2", docs_dir=d) is not None
    (d / "PORTAL_PLAN.md").write_text("## 9. Replaced\n\nnew body\n", encoding="utf-8")
    time.sleep(0.01)          # mtime granularity: force the stamp to move
    (d / "PORTAL_PLAN.md").touch()
    assert kb.find("2", docs_dir=d) is None
    assert kb.find("9", docs_dir=d) is not None


def test_a_missing_docs_directory_is_said_out_loud(tmp_path):
    with pytest.raises(FileNotFoundError):
        kb.sections(tmp_path / "not-here")


# ---------------------------------------------------------------- search
def test_search_prefers_the_section_named_for_the_term(tmp_path):
    d = _fixture_docs(tmp_path)
    hits = kb.search("nested kv unified", docs_dir=d)
    assert hits and hits[0]["id"] == "1.2"


def test_search_ignores_a_query_with_no_terms(tmp_path):
    assert kb.search("a b", docs_dir=_fixture_docs(tmp_path)) == []


def test_search_carries_an_excerpt_a_reader_can_recognise(tmp_path):
    hits = kb.search("nested", docs_dir=_fixture_docs(tmp_path))
    assert any("nested body" in h["excerpt"] for h in hits)


# ---------------------------------------------------------------- rendering
def test_the_renderer_escapes_before_it_decorates():
    """Docs prose that looks like markup must not become markup in a portal page."""
    out = kb.render_markdown("evil <script>alert(1)</script> and `code`")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<code>code</code>" in out


def test_a_script_url_is_rendered_as_plain_text():
    out = kb.render_markdown("[click](javascript:alert(1))")
    assert "javascript:" not in out
    assert "click" in out


def test_a_safe_link_survives_and_a_citation_becomes_a_link():
    out = kb.render_markdown("see [docs](https://example.com/a) and §6.12.6 for the measurement")
    assert 'href="https://example.com/a"' in out
    assert 'href="/help#6.12.6"' in out


def test_a_table_and_a_fenced_block_survive_the_renderer():
    out = kb.render_markdown("| key | verdict |\n|---|---|\n| mmap | rejected |\n\n"
                            "```\nllama-server -c 0\n```")
    assert "<table" in out and "<th>key</th>" in out and "rejected" in out
    assert "llama-server -c 0" in out and "<pre" in out


def test_a_real_docs_section_renders_with_its_own_body():
    s = kb.find("6.12", docs_dir=DOCS)
    assert s is not None
    html = kb.section_html("6.12", docs_dir=DOCS)
    assert len(html) > 200
    assert "<script>" not in html


# ---------------------------------------------------------------- the page
def test_the_help_page_lists_sections(client):
    r = client.get("/help")
    assert r.status_code == 200
    body = r.text
    assert "6.12" in body and "PORTAL_PLAN.md" in body


def test_the_help_page_answers_a_search(client):
    r = client.get("/help", params={"q": "kv unified"})
    assert r.status_code == 200
    assert "kv" in r.text.lower()


def test_the_help_api_returns_sections_as_json(client):
    r = client.get("/api/help", params={"q": "fit"})
    assert r.status_code == 200
    data = r.json()
    assert data["hits"] and all("id" in h for h in data["hits"])


def test_the_help_api_answers_a_citation(client):
    r = client.get("/api/help/section/4.10")
    assert r.status_code == 200
    d = r.json()
    assert d["id"] == "4.10" and d["html"]


def test_a_citation_to_nothing_is_reported_as_such(client):
    r = client.get("/api/help/section/99.99.99")
    assert r.status_code == 404


def test_help_is_reachable_from_the_navigation(client):
    """A knowledge base nobody can navigate to is a file in the repo again."""
    body = client.get("/help").text
    assert 'href="/help"' in body


def test_the_documented_files_are_the_ones_on_disk():
    names = [n for n, _ in kb.docs_available(docs_dir=DOCS)]
    assert "PORTAL_PLAN.md" in names and "MODEL_SIGNALS.md" in names
    assert "PLAN.md" not in names        # superseded; searching it would mislead


def test_warm_reports_the_sections_it_parsed():
    assert kb.warm() >= 50

# ------------------------------------------- a citation the page prints must resolve
#
# The UI harness caught this the honest way: it clicked the page's own section links and got
# 404s. `/help` links headings that carry no number, and it links them by slug. `find()` read
# the digits inside a slug as a section number, so `measured-catalogue-2026-10-02-b11146-this-box`
# became `2026.10.02`, named nothing, and the page's own link failed.

def test_every_link_the_help_page_prints_opens_a_section():
    """No dead links on the citation page. That is the page's whole job.

    Run against the real docs on purpose: the page serves those, and a fixture docs dir would
    prove the resolver against files nobody cites.
    """
    toc = kb.toc()
    assert len(toc) > 20, f"the docs parsed to {len(toc)} sections -- check the docs path"
    dead = [e["id"] for e in toc if kb.find(e["id"]) is None]
    assert not dead, f"{len(dead)} section links resolve to nothing: {dead[:5]}"
    # A section's html is its body, not its heading: the endpoint ships the title as its own
    # field, so the page can put it in a <h3> rather than dig it out of markup. An empty body is
    # legal (a heading followed straight by a subheading), so the honest assertion is that the
    # html matches the body -- non-empty for non-empty, empty for empty.
    for e in toc[:20]:
        found = kb.find(e["id"])
        assert found is not None and found["title"] == e["title"], e["id"]
        html = kb.section_html(e["id"])
        assert bool(html) == bool(found["body"].strip()), (
            f"{e['id']}: body {'has' if found['body'].strip() else 'has no'} text, "
            f"html {'has' if html else 'has none'}")


def test_a_slug_that_contains_digits_stays_a_slug():
    """The exact reference the UI harness hit, plus the numeric citation forms."""
    slug = "measured-catalogue-2026-10-02-b11146-this-box"
    hit = kb.find(slug)
    assert hit is not None and hit["id"] == slug, hit
    for ref in ("6.12.6", "\u00a76.12.6", "  \u00a7 6.12.6 ", "help#6.12.6", "#6.12.6"):
        found = kb.find(ref)
        assert found is not None and found["id"] == "6.12.6", (ref, found)


def test_a_reference_that_names_nothing_is_none_not_a_guess():
    assert kb.find("no-such-section-anywhere") is None
    assert kb.find("") is None
    assert kb.find("\u00a7") is None
