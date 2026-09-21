"""Parse a chat template with Jinja's own AST instead of guessing with regexes.

Why this exists: the marker detector in modelmeta was a set of regexes over template
text, and it was wrong in ways that looked like facts about the models.

  * gemma-4-31B declares tools through a macro — `format_function_declaration(tool_data)`
    reading `tool_data['function']['name']` — and contains `tool_call` 14 times. The
    `tools` marker never fired because it looked for the delimiter `<|tool_call|>`, which
    gemma-4 does not use. The page said "tool-results" and not "tools" for a model that
    plainly emits tool declarations.
  * `thinking` fired only for gemma-4 even though other templates contain `think`/`thinking`
    (Qwen3.6 has `think` 13 times), because the pattern demanded a delimiter form.
  * `tools` and `tool-results` almost always co-occur because a template that emits tool
    calls nearly always also formats their results. Two tags for one capability is noise,
    and the near-perfect correlation was the clue.

The fix is not a better regex, it is to stop reading a Jinja program as a string:
jinja2 is already a dependency, `Environment().parse()` accepts these templates without
executing them, and the AST distinguishes a loop variable named `tools` (no evidence)
from a literal emitted string `<tool_call>` (evidence) — which is exactly the
distinction the regex approach kept getting wrong.
"""
from __future__ import annotations

import jinja2
import jinja2.ext
from jinja2 import nodes


class _GenerationExtension(jinja2.ext.Extension):
    """Teach Jinja the `{% generation %}` block Hugging Face adds.

    Plain Jinja rejects it: `{% generation %}` raises "Encountered unknown tag
    'generation'", which made Laguna-S-2.1 report no capabilities at all — a parse
    failure indistinguishable from a model that declares nothing. Surveying the 16
    templates on this box found exactly one tag outside the Jinja standard, this pair,
    so the fix is to teach Jinja that tag rather than fall back to regex and lose the
    AST advantage everywhere else.

    Parsed and passed through as ordinary output; nothing here renders anything.
    """

    tags = {"generation"}

    def parse(self, parser):
        lineno = next(parser.stream).lineno
        # parse_statements is the API that returns List[Node]; parse_tuple returns
        # Tuple | Expr, which is why wrapping it in list() failed to type-check.
        body = parser.parse_statements(("name:endgeneration",), drop_needle=True)
        return nodes.Output(body, lineno=lineno)


# A template is parsed, never rendered. Rendering one would execute whatever the model
# author wrote, and a chat template is untrusted input from a downloaded file.
#
# autoescape=True is belt-and-braces rather than load-bearing: this Environment is only
# ever used for .parse(), so nothing is compiled or rendered and no output is produced for
# autoescaping to protect. It is set anyway so the parse-only contract is explicit — and
# because a future reader who adds a .render() call here would otherwise get silent, raw
# output from an untrusted template.
_ENV = jinja2.Environment(
    undefined=jinja2.Undefined,
    autoescape=True,
    extensions=[_GenerationExtension],
)


def parse_template(template: str) -> tuple[nodes.Template | None, str | None]:
    """(ast, error). Returns (None, message) when the template will not parse.

    Reports the failure rather than swallowing it: "this template is not Jinja I can
    read" is a fact worth showing, and a silent fallback to regex would make a parse
    failure look like a model that has no capabilities.
    """
    if not isinstance(template, str) or not template.strip():
        return None, "no template"
    try:
        return _ENV.parse(template), None
    except jinja2.TemplateError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def template_facts(template: str) -> dict:
    """What a chat template actually emits, from its AST.

    Returns {"facts": [...], "notes": [...], "parsed": bool}.

    Evidence is taken from *emitted literals* and *compared values*, both of which a
    template author must write out, and never from a bare name. A `{% for tool in tools %}`
    loop is a variable reference and proves nothing; `{{- \"<tool_call>\" -}}` is a
    string the model is told to produce.
    """
    ast, err = parse_template(template)
    if ast is None:
        return {"facts": [], "notes": [f"template not parsed: {err}"], "parsed": False}

    literals: list[str] = []      # strings the template emits verbatim
    subscripts: list[str] = []    # constant subscripts anywhere: ['role'], ['function']
    compared: list[str] = []      # string literals appearing in a comparison
    names: set[str] = set()       # bare identifiers (loop vars, filters) — context only

    for node in ast.find_all(nodes.Const):
        if isinstance(node.value, str):
            literals.append(node.value)

    # Getitem.fields is ('node', 'arg', 'ctx'), and `arg` is ONE node — not a list.
    # `{{ a["role"] }}` gives Const('role'); `{{ a[b] }}` gives Name('b'). Reading it as
    # a sequence, or using the name `args` (which jinja2 does not define on Getitem),
    # raises. The subscript is what identifies a role key or a tool declaration key.
    for node in ast.find_all(nodes.Getitem):
        arg = node.arg
        if isinstance(arg, nodes.Const) and isinstance(arg.value, str):
            subscripts.append(arg.value)

    # Compare.fields is ('expr', 'ops') — the compared values live in `expr`, and there
    # is no `exprs` attribute. A first draft that iterated `node.exprs` failed here.
    for node in ast.find_all(nodes.Compare):
        for side in (node.expr, *node.ops):
            if isinstance(side, nodes.Const) and isinstance(side.value, str):
                compared.append(side.value)
            elif isinstance(side, nodes.Getitem) and isinstance(side.arg, nodes.Const) \
                    and isinstance(side.arg.value, str):
                compared.append(side.arg.value)
        # Compare.ops holds operator nodes (Eq, ...) and Compare.expr is the left operand;
        # the right operands are reached through the operator nodes' fields.
        for op in node.ops:
            for sub in op.find_all(nodes.Const):
                if isinstance(sub.value, str):
                    compared.append(sub.value)
            for sub in op.find_all(nodes.Getitem):
                if isinstance(sub.arg, nodes.Const) and isinstance(sub.arg.value, str):
                    compared.append(sub.arg.value)

    for node in ast.find_all(nodes.Name):
        names.add(node.name)

    blob = "\n".join(literals)
    low = blob.lower()
    facts: list[str] = []

    # Tool support: an emitted tool delimiter, a tool-declaration macro shape, or a
    # comparison whose value is the tool role. Any one is sufficient; the regex version
    # required the delimiter and so mislabelled every template that spelled tools another
    # way (gemma-4 here).
    emits_calls = ("<tool_call>" in low or "tool_call" in low
                   or "<function_call>" in low)
    handles_results = ("tool_response" in compared or "tool_results" in compared
                       or "<|tool_response|>" in low or "<tool_response>" in low)
    declares_tools = ("function" in subscripts or "parameters" in subscripts
                      or ("name" in subscripts and "tool" in low))
    # `tools` is the umbrella: a template that emits calls, declares a tool schema, or
    # handles tool-role results all need the loader to support tools. Reporting only the
    # narrow half is what produced the page's "tool-results but not tools" rows.
    if emits_calls or declares_tools or handles_results or "tool" in compared:
        facts.append("tools")

    # System role: the template must write the role name to bracket the instruction.
    if "system" in compared or "<|system|>" in low or "[system]" in low:
        facts.append("system")

    # Reasoning. Reported as one fact, not a verdict: a template that mentions a thinking
    # block formats one, which is what the loader needs to know. The regex demanded
    # `<think>`-style delimiters and so reported this for 1 of 15 models.
    # Checked against emitted literals, which is the strict reading: the template has to
    # write the block out. On this box that is ``/`</think>` (Qwen) and
    # `<|think|>` (gemma-4). A template that only names a variable `think` is not evidence
    # — see `test_reasoning_from_a_bare_variable_name_is_not_evidence`.
    if ("think" in low or "thinking" in compared or "reasoning" in low):
        facts.append("reasoning")

    # A plain chat envelope. `<|im_start|>` is the delimiter rather than a role name, so
    # it is checked against emitted literals, where it has to be spelled out.
    if any(r in compared for r in ("user", "assistant", "human", "bot")) or "<|im_start|>" in low:
        facts.append("chat")

    if "rerank" in low:
        facts.append("rerank")
    if "query_prefix" in low or "document_prefix" in low or "<|embed" in low:
        facts.append("embedding")

    notes: list[str] = []
    # Separate "the template knows about tools" from "it formats tool results", but only
    # state the second when it is actually distinct. Reporting both on every template is
    # the redundancy the table was showing.
    formats_results = ("tool_response" in low or "tool_results" in low
                       or "tool_results" in compared or "tool_response" in compared)
    # A tool-declaration shape is a subscript on a tool object: tool_data['function']
    # ['name']. gemma-4 spells tools that way and the old delimiter regex missed it
    # entirely, so the page showed "tool-results" without "tools" for a model that emits
    # tool declarations 14 times over.
    declares_tools = ("function" in subscripts or "name" in subscripts
                      or "parameters" in subscripts)
    if declares_tools and "tools" not in facts and ("function" in low or "tool" in low):
        facts.append("tools")

    if "tools" in facts and formats_results:
        notes.append("formats tool results as well as tool calls")
    elif formats_results:
        facts.append("tool-results")

    return {
        "facts": sorted(set(facts)),
        "notes": notes,
        "parsed": True,
        "literals": len(literals),
        "roles_compared": sorted(set(compared)),
        "subscripts": sorted(set(subscripts)),
        "names_seen": sorted(names),
    }
