# Adapting the terminology index to the Managed Agents setup

> How the `xas-ai-agent` terminology index (today: per-company, Titan-embedded,
> served by the `xioma-read` MCP) moves onto the Anthropic-hosted Managed Agents
> sandbox. Companion to `docs/xas-ai-agent-migration-plan.md`; this doc drills into
> the one subsystem that doesn't port cleanly. Design only — no code here.

---

## 1. What the index is, and why it doesn't port as-is

The index maps **human/business vocabulary** (7 languages incl. Hebrew, aliases,
locally-repurposed names) → the **opaque per-company system codes** used to filter
job cards, and back again (code → user-facing label, so the agent never shows raw
codes). Today that resolution is a **hybrid search** inside the `xioma-read` MCP:
exact `code_map` hits (score 1.0) unioned with **Titan vector** cosine similarity
(256-d, threshold 0.45), over per-company snapshots (~2 MB each, e.g.
`xioma-DMSDEV2023.json`).

Two facts break the direct port:

1. **The sandbox has zero egress and no Bedrock** — it can't call Titan to embed a
   query, and (in the mounted-file design) it isn't calling the MCP either.
2. **The rich snapshot is ~2 MB per company** — fine to *mount* as a file, but the
   model must never read it whole into context.

## 2. The principle that resolves both

> **Mount the index as a file (cheap — files don't touch context); never let the
> model read it whole; make it *queryable* so only tiny lookup results enter
> context.**

Titan/vector search was just *one* way to turn "a big table of vocabulary" into
"a few matched rows." In the sandbox we need a substitute matcher. Everything below
is a different substitute; they share one architecture and differ only in how smart
the matcher is — so we can move along the spectrum without re-plumbing.

## 3. The "index-flatten" transform (the key artifact)

A one-time, deterministic, **pure-code** step (host-side or offline — the analog of
the allocation agent's `flatten`) that turns the rich 2 MB tree into a
**lookup-optimized** shape:

- **Denormalized phrasebook** — one row per *surface string*:
  `{text, normalized, kind, system_code, app_name, parent(entity/classification/field)}`.
  `normalized` reuses today's `normalize()` (casefold + strip Hebrew niqqud/combining
  marks) so matching is language-robust.
- **Bidirectional** — the same rows answer `term → code` (build filters) and
  `code → app_name` (present without raw codes). One store, not two.
- **Traps encoded explicitly** — the "code vs. name diverge" cases (e.g. code
  `Transfer` actually meaning "Parts Return Request") are first-class rows/notes,
  **not** left for the model to infer. This is where a naive read-and-reason
  approach fails silently, so it's carried in the data, plus the snapshot's
  `about`/usage note.

This transform is re-runnable and testable on its own (like `flatten.py`), and it's
what every option in §4 consumes.

## 4. The matcher spectrum (cheapest → highest fidelity)

| # | Approach | How resolution works | Fidelity vs. today | Cost / notes |
|---|---|---|---|---|
| 1 | **Slim phrasebook + helper** | Mount the flattened table; a tiny `resolve` helper (bounded to the mount path, never `find /`) does exact/alias lookup; the model handles the fuzzy tail by reading matched rows. | Model is the fuzzy matcher; exact is deterministic | Lowest. Big file stays out of context — only matched rows enter. |
| 2 | **Local SQLite + FTS5** | Load the table into SQLite in the sandbox; agent runs `SELECT … WHERE` / FTS5 full-text queries (tokenized, prefix, multi-word, basic ranking). | Better-than-grep, deterministic, inspectable | Low. No ML. Unicode tokenizer + a pre-normalized column for Hebrew. |
| 3 | **Embeddings in the sandbox** | Precompute surface vectors offline, bundle them; run a small **multilingual** model locally (CPU) to embed the query + cosine search — today's hybrid, entirely in-sandbox, no Bedrock/MCP. | ~Full semantic parity | Medium. Must **bundle model weights** in the skill (pip fetches packages, but weight downloads need egress, which is off). |
| 4 | **Host-answered custom tool (Design B)** | Don't move the index — keep `xioma-read` as-is; the worker answers `index_lookup` (embed + vector search) host-side; the sandbox just calls the tool. | Exactly today's calibrated search | Medium. A host `tool_runner`; no index in the sandbox. Zero index change. |
| 5 | **Direct MCP (Design A)** | Expose `xioma-read` to Anthropic; sandbox calls it directly — but move `query_vec` computation **into the MCP** (embed the term server-side), since the sandbox can't produce Titan vectors. | Full parity, sandbox-native | Medium. Expose read MCP + a **company-level** credential (read is company-scoped, not per-user — a single `static_bearer`). |

## 5. Cross-cutting design choices (combine with any row above)

- **Exact-first, model-fallback** (mirrors today's hybrid): deterministic exact/alias
  lookup runs first (the `code_map` equivalent, score-1.0 hits); only misses fall to
  the fuzzy layer (model reasoning, FTS5, or vectors). Cheap and safe for the common
  cases, flexible for the long tail.
- **Per-company primer at session start**: a precomputed few-KB cheat sheet of the
  most-used classifications/statuses/branches with all-language names, injected as a
  session-start **mid-session system message** (or a small always-read file). Front-
  loads common vocabulary cheaply and **absorbs today's `index_orientation` call**;
  the long tail falls back to the queryable store.
- **Live-mount the data, skill-bundle the resolver**: the index is per-company and
  drifts as the DMS config changes, so fetch the *current* company index host-side at
  session create and **mount** it (like `pull.json`), while the `resolve` / SQLite-
  builder **code** ships in the **skill**. Data stays fresh with no redeploy; code
  changes go through `setup`.

## 6. Freshness & multi-tenant

- **Per company** — mount the tenant's own index file per session (the tenant is
  known from the request's `companyDB`). Never bundle all tenants' data in the skill.
- **Regeneration** — the index-flatten step re-runs whenever the DMS config changes;
  because the data is mounted live (not baked into the skill), regenerating it needs
  no agent/skill redeploy — only the resolver code does.
- **Reverse lookups** for presentation come from the same mounted store, so
  "never show raw codes/ObjectIds" holds without a second fetch.

## 7. Recommendation & upgrade path

- **v1: option #1 or #2** (slim phrasebook, ideally SQLite/FTS5) + exact-first/model-
  fallback + the per-company primer. Simple, deterministic, keeps the 2 MB file out of
  context, and leans on Claude's multilingual strength for the fuzzy tail.
- **If evals show resolution slipping** on hard multilingual/alias cases: upgrade the
  fuzzy layer to **#3** (local embeddings), or — if we'd rather not carry index logic
  in the sandbox at all — fall back to **#4/#5** and keep the calibrated MCP search.
- The nice property: **#1 → #2 → #3 are the same mounted-file architecture** with a
  progressively smarter matcher, so moving along the spectrum is a matcher swap, not a
  re-plumb. #4/#5 are the "keep the MCP" escape hatches if in-sandbox resolution ever
  proves not worth it.

## 8. How it plugs into the milestones

- **Milestone 0 (current spike)** already uses a hand-slimmed mounted index +
  model reasoning — i.e. a manual **#1**. First concrete step is to replace the
  hand-slim with the real **index-flatten** transform and (optionally) the `resolve`
  helper, and to add a couple of **eval questions** (incl. Hebrew, and a known
  code-vs-name trap) as the quality gate.
- **Milestone 1+** decides whether resolution stays in-sandbox (#1→#3) or moves to the
  MCP (#4/#5), based on those eval results.

## 9. Open decisions

| # | Decision | Leaning |
|---|---|---|
| IDX-1 | Matcher for v1 | **SQLite/FTS5 (#2)**; phrasebook+grep (#1) if we want zero deps first |
| IDX-2 | Store shape | Flattened bidirectional phrasebook; SQLite built from it in-sandbox |
| IDX-3 | Where the flatten runs | Host-side at session create (fresh), mounted like `pull.json` |
| IDX-4 | Orientation / primer | Fold `index_orientation` into a session-start primer (system message or small file) |
| IDX-5 | Fuzzy-tail upgrade trigger | Eval-driven: only add in-sandbox embeddings (#3) if #1/#2 miss on the multilingual set |
| IDX-6 | Keep `xioma-read` at all? | Retire for v1 (mounted file); keep the code as the #4/#5 escape hatch |
| IDX-7 | Trap coverage | Encode code-vs-name divergences as explicit rows + carry the `about` note; test them |

### One-line summary

Mount the tenant's index as a file, flatten it once into a compact bidirectional
phrasebook (exact + traps encoded), and query it in-sandbox with a progressively
smarter matcher (grep → SQLite/FTS5 → local embeddings) — keeping the 2 MB out of
context and the calibrated MCP search only as an eval-driven escape hatch.
