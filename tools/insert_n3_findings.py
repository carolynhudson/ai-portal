"""Insert the N3/N4-session findings into docs/PORTAL_PLAN.md as pure additions.

Written as a script rather than a shell heredoc: a heredoc truncated this file's
sibling test file mid-string earlier in the session, and a truncated insert into a
1500-line plan is worse than a slow one. Every edit here is an insertion; the script
refuses to run unless the result has zero deleted lines.
"""
from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path("/home/cortana/ai-projects/ai-portal")
DOC = ROOT / "docs" / "PORTAL_PLAN.md"

ANCHOR = "Params are absent from every header on this box and stay `NULL`."

INSERT = r"""**N3 shipped 2026-09-18** (`31b6860`, `6e52d82`, `b9a0e05`): one library table that
answers *what is it / what is it paired with / can it run here*, a live light replacing
the separate router table, capability facts read from the chat template, and a peak-RAM
figure a loader can plan against.

**`ctx_that_fits` divided by a one-token slope and under-reported every window model.**
`avail // kv_bytes_at(meta, 1)` assumed KV grows linearly with context. It does not: on
a sliding-window model the layers under the window stop growing once ctx passes it, so
gemma-4 charges all 60 layers at ctx=1 and only 10 at ctx=262144 — the one-token slope
was ~2× the true marginal cost at scale. It printed "reduced max 128K" for a 31B model
with 84 GiB of its 108 GiB aperture free and a full 262144 cache costing 7 GiB. Now a
search of the power-of-two ladder using the real cost function, bounded by trained
context: ~21 evaluations of a closed-form sum, never off by more than one doubling, and
monotonic in budget by construction. *The error pointed the safe direction, which is
exactly why it survived review — an estimate that under-promises reads as caution. A
conservative estimate is not a correct one, and it costs capability silently.*

**Two numbers, two questions, and the gap must be pinned.** `verdict()` answers "is
weights+cache within the aperture" (a display fact). `peak_bytes()` answers "what is
resident if this loads" and adds `HEADROOM_BYTES`, because the aperture is not the only
consumer of RAM. A model can therefore be labelled *fits* at a budget `peak_bytes` says
is not enough to load it. `test_peak_is_stricter_than_the_displayed_verdict_by_exactly_
headroom` pins the difference at exactly `HEADROOM_BYTES` so a future change to either
side is a decision, not a drift. `peak_bytes` returns `None` rather than a guess: a
scheduler given 0 loads, one given `None` refuses.

**Capability facts from the chat template, not the vocabulary.** A GGUF carries its own
`tokenizer.chat_template` — kilobytes of Jinja, present on 15 of the 16 models here —
and the delimiters it must spell out literally are evidence of what the weights were
trained to speak: `<tool_call>`, `"role": "tool"`, `<|think|>`. Library counts:
chat 15, system 14, tool-results 14, tools 12, thinking 1.

Two traps in getting there, both worth the length of their write-up:

* **An alternation of delimiters must escape every pipe that is punctuation.**
  `<\|tool_call|>` was read by the regex engine as "`<|tool_call` OR the empty string",
  matched at offset 0 of every template, and reported tool support on 13 of 15 models.
  A pattern whose alternation can be empty does not fail — it reports true for every
  input, forever.
* **Substrings lie about Jinja.** `"tools" in template` matched the loop variable
  `{% for tool in tools %}`, which proves nothing about what the model emits. Matching
  is structural now. The correction in the other direction also mattered: anchoring the
  comparison operator right after the closing quote matched JSON `{"role": "tool"}` but
  missed `message["role"] == "tool"` — the closing bracket comes first in Jinja — so
  tool support and the system role went unreported on every Jinja-templated model.
  Under-reporting also reads as caution and also ships.

**What the tokenizer cannot tell us on this box, recorded so nobody re-derives it.**
`tokenizer.ggml.tokens` is a 262144-string array; when this reader parsed it, it
returned a 24-character *string* — garbage that looked exactly like data.
`tokenizer.ggml.token_type` is parallel and equally large. `tokenizer.ggml.added_tokens`,
the short list where special tokens would live, is present on **0 of 16** models. So
there is no token-derived role column, and the constraint is a comment rather than a
column that would always render blank and read as "this model declares nothing" when
the truth is "we have no way to ask".

**Only shard 1 carries the tokenizer.** Same rule as the architecture: shards 2–3 parse
as valid GGUF with `arch=None` *and* `has_chat_template=False`. A probe that globs
`*.gguf` and takes the first hit will report a sharded model as having no chat template
and no architecture. This trap has now cost this project measurements nine times.

**The live light replaced the router table.** "Live" is a property of a model, so it
belongs on the model's row. Matching runs through `modelindex.router_key()`, because the
router registers some models under a HuggingFace-style alias (`unsloth/Qwen3.8-27B-GGUF:
Q8_K_XL`) that no directory name resembles. A raw compare shows a serving model as idle,
and *that* is the one wrong answer this column can give: "not running" invites loading a
second copy of a model already taking traffic, which is how you double the memory cost of
a model that is up. Live models with no library row are listed in their own section — the
merge must not quietly become a filter, and "16 models, all accounted for" is a lie when
the router is serving a 17th.

**`_declared_type()`, and a helper that quietly did nothing.** The migration ALTERed
every missing column in as `INTEGER`, so `template_markers` (TEXT in SCHEMA) would have
arrived as INTEGER on an upgraded box and TEXT on a fresh one; SQLite stores a string in
an INTEGER column without complaining, so the only symptom is surprising sort order. The
first implementation then returned its TEXT fallback for *every* column: SCHEMA puts
several columns on a line, so the token after a name is `INTEGER,` — type plus separator —
and `INTEGER,` never matched the allowed set. A helper written to remove a second opinion
had become a no-op. Verify a new helper returns different answers for different inputs.

"""

def main() -> int:
    text = DOC.read_text()
    if ANCHOR not in text:
        print(f"anchor not found: {ANCHOR!r}")
        return 1
    if "**N3 shipped 2026-09-18**" in text:
        print("already inserted; nothing to do")
        return 0

    # Insert immediately after the paragraph that ends the capability-facts section.
    head, tail = text.split(ANCHOR, 1)
    end = tail.index("\n\n**Lessons carried over")
    new = head + ANCHOR + tail[:end] + "\n\n" + INSERT + tail[end:]
    DOC.write_text(new)

    numstat = subprocess.run(["git", "-C", str(ROOT), "diff", "--numstat",
                              "docs/PORTAL_PLAN.md"],
                             capture_output=True, text=True).stdout.strip()
    print("numstat:", numstat)
    added, deleted = (numstat.split("\t")[:2] if numstat else ("0", "0"))
    if deleted != "0":
        print(f"REFUSING: this edit deleted {deleted} line(s); docs edits are "
              f"insertions only")
        subprocess.run(["git", "-C", str(ROOT), "checkout", "--", "docs/PORTAL_PLAN.md"])
        return 1
    print(f"pure insertion: +{added} lines, 0 deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
