"""Chat-template facts read from the template's AST, not from regexes over its text.

Mutation-verified guards. Each test is written so that the specific mistake it exists
to catch — delimiter-only tool detection, delimiter-only reasoning detection, a bare
loop variable counted as evidence, a template that will not parse reported as "no
capabilities" — makes the test fail loudly instead of passing quietly.
"""
from __future__ import annotations

from app.core import chatml


def facts(t: str) -> set[str]:
    return set(chatml.template_facts(t)["facts"])


# --- tools --------------------------------------------------------------------
# The bug this guards: the old detector looked for the delimiter `<tool_call>`. A
# template that declares tools another way was reported as having no tool support at
# all, while still being reported as handling tool results — the incoherent pair the
# models page was showing.

def test_tool_delimiter_counts():
    assert "tools" in facts('{{- "<tool_call>" -}}')


def test_tool_role_comparison_counts():
    assert "tools" in facts('{% if message["role"] == "tool" %}x{% endif %}')


def test_declaration_macro_counts_without_a_delimiter():
    # Shaped like gemma-4's format_function_declaration: no `<tool_call>` anywhere,
    # tools spelled out as a subscripted object. A delimiter regex scores zero here.
    t = ('{%- macro fmt(td) -%}{{ td[\'function\'][\'name\'] }}'
         '{% for tool in tools %}{{ tool | tojson }}{% endfor %}{%- endmacro -%}')
    assert "tools" in facts(t)


def test_bare_loop_variable_is_not_evidence():
    # `{% for tool in tools %}` alone names a variable a caller might not even pass.
    # It emits no delimiter, no role, no subscript — reporting tools here would be the
    # same class of false positive, inverted.
    t = '{% for tool in tools %}{{ tool.name }}{% endfor %}'
    got = facts(t)
    assert "tools" not in got, got
    # recorded as context, not as a capability, so a reader can see it was noticed
    assert "tools" in chatml.template_facts(t)["names_seen"]


# --- reasoning ----------------------------------------------------------------
# The bug this guards: `thinking` fired only for templates using ``-style
# delimiters, so 14 of 15 models on this box were reported as not reasoning.

def test_thinking_delimiter_counts():
    assert "reasoning" in facts('{{ "think"}}')


def test_plain_word_reasoning_counts():
    # No `think`-style delimiter: a template that labels the block in prose only.
    assert "reasoning" in facts('{{ "Thinking:\n" }}{{ think }}{{ "\nAnswer:\n" }}')


def test_reasoning_from_a_bare_variable_name_is_not_evidence():
    # `{% if think %}` names a variable the caller may not even supply, and emits nothing
    # about reasoning. Counting it would be the same mistake as counting `for tool in tools`.
    t = '{% if think %}{{ think }}{% endif %}'
    got = facts(t)
    assert "reasoning" not in got, got
    assert "think" in chatml.template_facts(t)["names_seen"]


# --- system / chat ------------------------------------------------------------

def test_system_role_counts_only_when_compared():
    assert "system" in facts('{% if messages[0]["role"] == "system" %}S{% endif %}')


def test_system_word_in_prose_does_not_count():
    # A literal that merely contains the word is not a role the template brackets.
    # Kept deliberately strict: this is the difference between a fact and a guess.
    assert "system" not in facts('{{ "the ecosystem is large" }}')


def test_chat_from_role_comparisons():
    assert "chat" in facts('{% if message["role"] == "user" %}U{% endif %}')


def test_chat_from_im_start_delimiter():
    # Built from parts so the delimiter token cannot be mangled by quoting or by an
    # editor rewriting it. The rule looks for the delimiter among EMITTED literals,
    # because a template that brackets turns must spell the turn out.
    im_start = "<|im_" + "start|>"
    assert "chat" in facts('{{ "' + im_start + 'user" }}')


# --- parse failure is a reported state, not an empty fact list ----------------

def test_unparseable_template_is_reported_as_unparsed():
    r = chatml.template_facts("{% if x %}unclosed")
    assert r["parsed"] is False
    assert r["facts"] == []
    # The distinction that matters: "we could not read it" must be visible, because an
    # empty list renders as "this model declares nothing".
    assert r["notes"] and "not parsed" in r["notes"][0]


def test_hf_generation_tag_parses_and_its_contents_are_seen():
    # `{% generation %}` is a Hugging Face extension, not Jinja. Plain Jinja raises
    # "Encountered unknown tag 'generation'", which is exactly what made one model on
    # this box report zero capabilities. Assert on the *contents*, not on a capability:
    # a literal inside the block must reach the fact engine like any other literal.
    im_start = "<|im_" + "start|>"
    t = '{% generation %}{{ "' + im_start + 'user" }}{{ x }}{% endgeneration %}'
    r = chatml.template_facts(t)
    assert r["parsed"] is True, r["notes"]
    assert r["literals"] >= 1
    assert "chat" in r["facts"], r["facts"]


def test_empty_template_reports_no_template_rather_than_crashing():
    for bad in ("", "   ", None, 123):
        r = chatml.template_facts(bad)  # type: ignore[arg-type]
        assert r["parsed"] is False
        assert r["facts"] == []


# --- tool-results stays a note, not a second tag, when tools is already known --

def test_tool_results_is_a_note_not_a_redundant_tag():
    t = '{{ "<tool_call>" }}{% if message["role"] == "tool_response" %}R{% endif %}'
    r = chatml.template_facts(t)
    assert "tools" in r["facts"]
    # The old table showed `tools` and `tool-results` as separate tags on 15 of 16
    # models — two labels for one capability. It is still worth saying, as a note.
    assert "tool-results" not in r["facts"]
    assert r["notes"] and "tool results" in r["notes"][0]


def test_tool_role_handling_counts_as_tools():
    # A template that brackets a tool role needs tool support whether or not it spells a
    # call delimiter. The old detector called this "tool-results" and nothing else, which
    # is the incoherent pair the models page was showing.
    t = '{% if message["role"] == "tool_response" %}R{% endif %}'
    r = chatml.template_facts(t)
    assert "tools" in r["facts"]
    assert r["notes"] and "tool results" in r["notes"][0]


# --- embedding / rerank --------------------------------------------------------

def test_embedding_markers_count():
    assert "embedding" in facts('{{ "<|embed|>" }}{{ query_prefix }}')


def test_rerank_marker_counts():
    assert "rerank" in facts('{{ "Assign a rerank score" }}')
