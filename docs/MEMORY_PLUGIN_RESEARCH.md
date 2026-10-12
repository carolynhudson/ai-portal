# Memory plugins for the harness — researched and measured (2026-10-12)

The question, in the operator's words: pi's `pi-hermes-memory` was chosen because its design looked
research-based, and updating it from inside pi has been awkward. Opencode has a memory-plugin
ecosystem. Which of them fit *this* box and *this* portal — and specifically: a memory subsystem
should be more than durable notes. It should also keep valuable context from being lost (smart
passive storage), and it should make project file contents quickly searchable.

Everything below is measured: npm registry metadata, GitHub API, package READMEs read in full, a
plugin list installed here, and one engine run against the portal's own source tree.

## 1. The candidates, with their real shape

| Package | Version / cadence | Licence | Stars | Storage + retrieval | Capture model |
|---|---|---|---|---|---|
| `@mathew-cf/opencode-memory` | 2.3.0, 15 releases, 2026-04-24 → 10-07 | Apache-2.0 | 4 | git-tracked markdown tree at `~/opencode-memory/` + hybrid keyword ✚ semantic search via `@mathew-cf/rag-cli` (a 36 MB Rust binary, local ONNX MiniLM) | explicit tools **plus** hooks: search-first nudge at 8 tool calls, discovery nudge on subagent output, retrospective reminder **at compaction time** |
| `@cioffi_ai/opencode-memory` | 1.7.2, 7 releases, 2026-08-06 → 09-30 | MIT | 9 | local JSON with file locking, **zero external database**; lexical pipeline ✚ optional semantic rerank with abstention | WRITE (tools) → DREAM (headless consolidation by the harness's own model) → SURFACE (relevance-gated injection) |
| `opencode-memory-pro` | 1.6.2, 32 releases in 4 days (2026-09-06 → 09-09), then quiet | **NOASSERTION** (GitHub) | 1 | LanceDB vectors, IVF ✚ BM25, sqlite entity graph, episodic learning | hybrid: offline heuristics, or LLM extraction through the SDK |
| `@openviking/opencode-plugin` | 2026.10.9, 44 releases | Apache-2.0 (plugin) / **AGPL-3.0** (server) | 39,685 (server repo) | OpenViking server: `viking://` virtual filesystem — memories, resources, skills as paths; three pre-computed summary tiers (L0/L1/L2) | full passive capture: messages, tool calls at execution boundaries, session commits, auto-recall injected as hidden synthetic context |
| `@mastra/opencode` | 0.1.35, 800 releases (monorepo) | Apache-2.0 | 28,723 (Mastra) | Mastra framework store (`@mastra/memory`, libsql) | Observational Memory (background observation) |
| `claude-mem` (Claude Code, also `--ide opencode`) | 13.35.0 on npm | AGPL-3.0 | 99,761 | SQLite FTS5 ✚ Chroma vectors, local worker on `:37777`, 3-layer MCP search (index → timeline → details) | 5 lifecycle hooks capture everything; compression runs through **Claude's Agent SDK** |
| `ainative-opencode-memory-mcp` | 0.1.2, 3 releases | MIT | — | **cloud** (ZeroDB), `AINATIVE_API_KEY` required, $5/mo hobbyist | explicit tools only |
| `@staticeng/opencode-agent-memory` | 0.1.3 | **UNLICENSED** | — | — | — |
| `@aifcoding/opencode-memory` | 2.1.1 | MIT | 5 | local, three-layer (Chinese-language project) | explicit ✚ consolidation |

Two findings that changed my reading of the field:

- **`opencode-memory-pro` has 32 releases in four days and then stops, with an unrecognized
  licence.** Cadence like that is automation, not maintenance, and `NOASSERTION` means we would be
  adopting an unlicensed dependency. It is out on those two facts alone.
- **`claude-mem` is the most-adopted memory system in this space (99,761 stars) and it has a
  documented token-bomb failure.** Issue #618: *"Claude Code consumes all my tokens in <10 messages.
  It wasn't like that before."* The mechanism is legible from its own architecture: every
  `PostToolUse` fires the Agent SDK to summarize, and `SessionStart` injects a slab. It also sends
  the captured content to Anthropic for compression, and needs Bun **and** `uv` (Chroma).

## 2. The engine I measured, on this box, on our own source

`@mathew-cf/opencode-memory` is thin (deps: `zod`, `@mathew-cf/rag-cli`) because the engine is a
separate package: `@mathew-cf/rag-cli` is a 4-file shim that selects a prebuilt **native binary per
platform** (36.3 MB for linux-x64; the fallback is `cargo install rag-cli`). I ran it against a copy
of the portal tree (26 MB, 2,121 chunks):

```
$ rag index ragproj
Embedding 2121 unique chunks (0 duplicates reused)...
Loading model: sentence-transformers/all-MiniLM-L6-v2
  Cache: /home/cortana/.cache/huggingface/hub
  Downloading onnx/model_quint8_avx2.onnx...
  Model loaded (hidden_size=384, backend=onnx-quint8-avx2, threads=32)
Index saved to .rag (2121 chunks, 11.8s)

$ rag search "which process owns the llama router"
  [1] docs/PLAN.md (score: 0.6271)  offset: 2394 bytes
```

What that proves for the portal's requirements:

- **Local embeddings, no cloud, no API key, no router call.** The model is a ~90 MB ONNX MiniLM
  cached under `~/.cache/huggingface/hub`, quantized (`quint8_avx2`), 32 threads, and it does not
  touch the llama.cpp router at all.
- **Indexing the whole portal repo takes 11.8 seconds.**
- `rag keyword` is a documented mode that searches **without loading the embedding model** — the
  degraded path is built in, which is exactly the shape of robust behaviour wanted here.
- Retrieval returns ranked chunks **with byte offsets**, so an agent can go to the exact place, and
  knowledge bases are separate indexes over source repositories (`knowledge_base_search`,
  `knowledge_base_read`) — that is the "quick project file contents search and retrieval" ask.
- `session_search` searches **OpenCode, Pi, and Codex** session history, and the plugin drops its
  skill at `~/.agents/skills/opencode-memory` — the directory both pi and opencode read. Passive
  context across the two harnesses, from one install.

## 3. The portal's constraints, applied as a filter

| Constraint | What it rules in | What it rules out |
|---|---|---|
| Local-first, no cloud keys | mathew-cf, cioffi, (pro), OpenViking-with-local-providers | ainative (paid cloud), claude-mem's compression step (Agent SDK → Anthropic) |
| The router runs **`max_instances: 1`**, `models_autoload: false` (measured from `/props`) | anything that embeds on its own CPU | any design that needs an embedding model resident on the router: the router answers `400 model '…' not found` for a registered-but-unloaded model, so an embedding service on the router would evict the chat model that is currently serving the session |
| Inspectable (the operator must be able to see what the agent remembers) | mathew-cf (git-tracked markdown, diffable), cioffi (JSON + `/memory` inspector, provenance to source messages) | LanceDB/vector-only stores where the corpus is opaque |
| Long-running tasks with compaction | hooks at compaction time, and consolidation that reuses the harness's own model | capture pipelines that bill a cloud model per tool call |
| Family users on the LAN, sessions that outlive the browser | a store on disk, per project | stores that need a second server to be running for the agent to be usable |
| Licence | Apache-2.0 / MIT | UNLICENSED (`@staticeng`), `NOASSERTION` (`opencode-memory-pro`), AGPL-3.0 for anything the portal offers as a network service (`claude-mem`, OpenViking server) |

OpenViking deserves a longer note, because it is the one with a research pedigree the operator would
recognise: it is the subject of the **VikingMem** paper (accepted to VLDB 2026), and its design is
"memory as a filesystem" — `viking://` paths, `ls`/`tree`/`find`/`grep`, three summary tiers
pre-computed per entry, and a **browsing trajectory** kept for every query, so a wrong answer can be
traced to the path that produced it. Its own published numbers (project-run, on Doubao 2.0 Pro) show
LoCoMo going 57.21 % → 80.32 % for Claude Code with 63 % fewer input tokens. Its honest trade-offs,
from the same review: LightRAG beats it by 9 points on single-turn RAG accuracy; write-time cost is
front-loaded (8.67 M indexing tokens in their own benchmark); 598 open issues with same-day version
churn; access control is thin; and it needs a VLM **and** an embedding model, whose published numbers
were measured on Volcengine's models, not local ones. So: a strong design, and for this box it is a
second server, a licence conversation, and a model budget — not a plugin.

## 4. Recommendation

**Adopt `@mathew-cf/opencode-memory` as the harness-side memory, and keep the portal's own context
as portal MCP tools (queue 25).** It is the only candidate that covers all three of the operator's
asks at once — durable notes *and* passive context retention (compaction-time retrospective, session
history search) *and* project file retrieval (knowledge bases) — with local embeddings measured
working on this box, a git-tracked corpus an ordinary person can read and diff, Apache-2.0, and a
skill that lands in the directory both pi and opencode read.

**Adopt `@cioffi_ai/opencode-memory` as the behaviour model for the portal's own memory decisions,
not as a second store.** Its ideas are the ones the portal should copy into `PORTAL_*` docs and
skills: a tier model (core / archival / temporary-with-expiry / pinned), **provenance for every
fact** (which session, which messages, what confidence), a contradiction lifecycle where a conflicted
fact is flagged and resolved rather than silently overwritten, retrieval feedback (`memory_useful` /
`memory_irrelevant`) that tunes ranking from real usage, and typed options that **fail setup on an
unknown name instead of silently falling back** — the same robustness bar the operator set for the
inspection tool.

**Do not adopt:** `ainative` (cloud, paid), `@staticeng` (UNLICENSED), `opencode-memory-pro`
(NOASSERTION plus release-cadence red flag), `claude-mem` (its compression step is a cloud call, its
token-burn issue is documented, and it wants Bun and `uv` for Chroma). **Do not adopt OpenViking yet**
— pilot it as its own project if cross-session context becomes the bottleneck; AGPL-3.0 is a
network-copyleft decision the operator should make, not a footnote, and it wants a resident embedding
model this box cannot give it while the router runs one instance.

Gates to verify if we adopt (measured before writing any portal code around them):

1. `plugins: ["@mathew-cf/opencode-memory"]` in the portal-generated opencode config, server
   restart, and `GET /tool` shows `memory_search` / `knowledge_base_search` for the router model.
2. `bunx @mathew-cf/opencode-memory init` runs offline-capable: model cached, `~/opencode-memory/`
   created as a git repo, skill symlinked to `~/.agents/skills/` (pi reads it too — check `pi list`).
3. `rag keyword` answers with the model absent (the degraded path).
4. A knowledge base over `~/ai-projects` (the portal's project root) indexes and answers.
5. Nothing writes to the router: the memory engine must never need `:8080`.

## 5. Sources

npm registry metadata and publish times: `@mathew-cf/opencode-memory`, `@cioffi_ai/opencode-memory`,
`opencode-memory-pro`, `@openviking/opencode-plugin`, `@mastra/opencode`,
`ainative-opencode-memory-mcp`, `@staticeng/opencode-agent-memory`, `@aifcoding/opencode-memory`,
`@tarquinen/opencode-dcp`, `@mathew-cf/rag-cli`, `claude-mem`, `open-codebase-index`,
`codebase-memory-mcp`. GitHub API: `mathew-cf/opencode-memory`, `cioffiAI/opencode-memory`,
`tman204-50/opencode-memory-pro`, `volcengine/OpenViking`, `thedotmack/claude-mem`,
`Tarquinen/opencode-dynamic-context-pruning`, `aifcoding/opencode-memory`, `chriswritescode-dev/opencode-memory`.
Package READMEs via unpkg (`@mathew-cf/opencode-memory@2.3.0`, `@cioffi_ai/opencode-memory@1.7.2`,
`opencode-memory-pro@1.6.2`, `@openviking/opencode-plugin@2026.10.9`,
`ainative-opencode-memory-mcp@0.1.2`). Reviews and reports: `docs.claude-mem.ai`, the `claude-mem`
review at andrew.ooo (Issue #618, SQLite ✚ Chroma, AGPL, worker on `:37777`), the OpenViking review
at andrew.ooo and vibecodinghub.org (VikingMem/VLDB 2026, LightRAG comparison, AGPL and write-cost
caveats, Ollama support), opendock.net plugin listing, `jczhu.com/blog/opencode-plugin-memory`
("project-scoped, inspectable, no cloud service or background consolidation job").
Local measurements: `rag index` / `rag search` on a copy of this repo (2,121 chunks, 11.8 s, ONNX
MiniLM quint8_avx2), the router's `/props` (`role: router`, `max_instances: 1`,
`models_autoload: false`) and `POST /v1/embeddings` → `400 model '…' not found` for registered
embedding ids. Related: `docs/HARNESS_LANDSCAPE_RESEARCH.md` §10 (plugin door measured:
`plugin: ["opencode-websearch-cited"]` put `websearch_cited` in the tool list), §7 (ACP), and
`docs/PI_HARNESS_RESEARCH.md` (the pi-web spike).
