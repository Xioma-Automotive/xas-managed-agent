# Migrating `xas-ai-agent` onto the Managed Agents platform

> Goal: move the dealership Q&A agent (`xas-ai-agent`) off the
> LangGraph-on-Bedrock-on-EC2 stack and onto the **Anthropic-hosted Managed
> Agents** platform that `xas-managed-agent` already runs on — **preserving every
> user-facing capability and enhancing it** with platform primitives (outcomes,
> budgets, permission gates, scheduling, live previews).
>
> This is a design/plan document, not an implementation. It reuses the patterns
> proven by the allocation agent in this repo (`web.py` + `alloc_tools.py` +
> `setup_allocation_agent.py`).

---

## 1. What we are moving

`xas-ai-agent` is a **single-loop LangGraph agent** that answers dealership-staff
questions in natural language (any language, including Hebrew). Its whole job is:

> **resolve human terms → opaque per-company system codes → fetch job-card data →
> present it (text / table / chart / graph).**

### Current architecture (source of truth)

| Concern | Today |
| --- | --- |
| Agent loop | LangGraph `create_agent` (ReAct), `recursion_limit: 25` |
| Model | AWS **Bedrock** — Haiku 4.5 / Sonnet 4.5 / Opus 4.7 (EU inference profiles), 3 config variants |
| Transport | FastAPI `POST /run` → **202** → async **callback** POST (`text`, `html`, `usage`) |
| Session state | **Postgres** via LangGraph `AsyncPostgresSaver`, keyed by `session_id` |
| Middleware | Bedrock prompt caching · context-editing (`clear_tool_uses`, trigger 80k) · tool-arg-sanitize · dynamic date prompt |
| Data tools (network + credentialed) | MCP: `index_lookup`, `describe_entity`, `index_orientation` (xioma-read) · `get_job_cards`, `get_job_card` (xas-app-mcp) |
| Presentation tools (pure) | `make_date_range`, `render_table`, `render_chart`, `render_graph` (HTML/SVG via `report_style.py`) |
| Semantic index | xioma-read MCP: **Titan embeddings** (`amazon.titan-embed-text-v2:0`, 256-d) over per-company snapshots |
| Auth | `XIOMA_MCP_KEY` (shared, caller auth) + per-request **`X-Xioma-User-Token`** (`__DMS_app_token`, user/tenant scope); `company_id` + `query_vec` injected server-side, hidden from the LLM |
| Per-request context | `userId`, `companyDB` (tenant), `locale`, `userToken` — flow via `RunnableConfig.configurable` |
| Deployment | Docker on dev **EC2**, xioma-read as in-container sidecar (`:8001`), Postgres sidecar, seccomp workarounds, STS credential refresh |
| Observability / evals | LangSmith tracing + LLM-as-judge eval suite |

### The constraint that shapes the data plane

The agent must reach **live, per-user, per-tenant** DMS data. Two facts interact:
the sandbox has **zero egress and holds no secrets**, and the per-user scope today
rides a **custom header** (`X-Xioma-User-Token`) *alongside* a shared key. The
platform gives **two supported ways** to satisfy this, and the choice is the
biggest decision in the port:

- **Direct MCP via per-user vaults** — Anthropic injects a per-user credential
  into the MCP call on its side; the sandbox calls the MCP directly and never sees
  the secret. First-class per-user, per-session (`vaults.create` +
  `sessions.create(vault_ids=[...])`). Requires reshaping the MCP's auth to one
  vault-injectable credential and exposing the MCP to Anthropic.
- **Host-answered custom tools** — the worker stays the MCP client and answers the
  data tools host-side (the allocation-agent pattern). Zero MCP changes; keeps the
  two-header auth and host-side embedding as-is.

Both are viable. §2 lays out each with its conditions and a recommendation.

---

## 2. Target architecture on Managed Agents

We keep the **same two-plane split** this repo already uses:

- **Control plane (run once, re-runnable):** a `setup_xas_agent.py` that creates the
  cloud **environment**, uploads the **skill** (presentation code + procedure), and
  creates/updates the **agent** (system prompt, model, tool declarations). Modeled
  on `setup_allocation_agent.py`.
- **Run plane (the only long-running process):** a `web.py`-style **worker** that
  keeps the existing bot contract (`POST /run` → callback) on the outside, and on
  the inside drives Managed-Agent **sessions**, **answers the credentialed data
  tools host-side**, and streams results back. Modeled on `web.py`.

```
  xas-ai-bot ──POST /run (message, session_id, context, userToken, callback)──►  ┌───────────────────────────┐
                                                                                 │  worker (our host process)│  holds: ANTHROPIC_API_KEY (org),
  xas-ai-bot ◄─────────────── callback POST (text, html, usage) ────────────────│                           │         XIOMA_MCP_KEY, Bedrock creds
                                                                                 │  • session-per-conversation
                                                                                 │  • tool_runner per session ──answers──► index_lookup / describe_entity /
                                                                                 │  • is the MCP client                     index_orientation / get_job_cards /
                                                                                 │  • embeds query_vec (Titan)              get_job_card  (host-side, credentialed)
                                                                                 └──────────┬────────────────┘
                                    Anthropic Managed Agents beta API (managed-agents-2026-04-01)
                                                        │
                                   ┌────────────────────▼─────────────────────┐
                                   │  Anthropic-hosted sandbox (per session)   │  zero egress, no secrets
                                   │  • the agent loop (Claude Opus/Sonnet/Haiku)
                                   │  • bash + file tools (agent_toolset)
                                   │  • the SKILL: date logic + report_style renderers (pure code)
                                   └───────────────────────────────────────────┘

  xioma-read MCP  ◄── worker is the client ──►  xas-app-mcp (job cards) ── gateway ── DMS
  (Titan index, host-side)                       (per-user X-Xioma-User-Token)
```

### The core mapping: LangGraph tools → Managed-Agent primitives

The **presentation** tools are pure (no network, no secrets) and move the same way
under either design — into the **skill** as code the sandbox runs:

| Tool | Managed-Agent home | Mechanism |
| --- | --- | --- |
| `make_date_range` | **skill code** in the sandbox | ship `date_tools.py` in the skill bundle; agent runs it |
| `render_table` / `render_chart` / `render_graph` | **skill code** in the sandbox | ship `report_style.py` + renderers; agent writes `report.html`; worker downloads it for the callback |

The **five data** tools (`index_lookup`, `describe_entity`, `index_orientation`,
`get_job_cards`, `get_job_card`) all need network + a credential, and their home
depends on which data-plane design we pick.

#### Data-plane Design A — Direct MCP via per-user vaults (recommended)

Per-user vaulting is a **first-class** platform feature, so the token is *not* a
problem to inject on Anthropic's side:

```python
vault = client.beta.vaults.create(metadata={"external_user_id": user_id})     # once per user
client.beta.vaults.credentials.create(vault_id=vault.id, auth={              # the user's DMS credential
    "type": "mcp_oauth" | "static_bearer", "mcp_server_url": XAS_MCP_URL, ...})
session = client.beta.sessions.create(agent=..., environment_id=...,          # bind per session
    vault_ids=[vault.id], ...)
```

The sandbox then calls the MCP tools **directly** (`mcp_toolset`); Anthropic
injects the per-user credential at connection time and the sandbox never sees it.
**No host-answered tools, no `tool_runner` pool.** Three conditions, all inside
Xioma's control:

1. **Fold the two-header auth into one vault-injectable credential.** Vault injects
   a single `Authorization: Bearer` (or an OAuth token) per `mcp_server_url`, and
   only one credential wins per URL — it **cannot** add a custom
   `X-Xioma-User-Token`, and cannot inject the shared key *and* the user token
   together. So the **per-user token becomes the bearer/OAuth credential**, and the
   shared-key check moves to a gateway Anthropic reaches (or is dropped in favor of
   the per-user auth). `mcp_oauth` per user is the clean target (**auto-refreshed**
   by Anthropic); `static_bearer` per user works if tokens are refreshed via
   `vaults.credentials.update()`.
2. **Expose the MCP to Anthropic** (public inbound / gateway) and allow it via the
   environment `networking` (`allow_mcp_servers` / `allowed_hosts`). Today it is
   internal (`dev_appmcp:5075`).
3. **Move the Titan `query_vec` computation into the xioma-read MCP** — embed
   `term` server-side so `index_lookup` needs no host step. The MCP already uses
   Bedrock to build the index, so this is natural.

Result: a **thin worker** (bot-contract shim + per-user vault provisioning +
session management), sandbox-native tools, data flowing via Anthropic-injected MCP
calls. This is the cleaner end state and the recommended target **if** we accept
the auth reshape + MCP exposure.

#### Data-plane Design B — Host-answered custom tools (fallback, zero MCP changes)

The worker stays the MCP client and answers the five tools host-side via a
per-session `tool_runner` — the exact `alloc_tools.py` pattern. It embeds
`query_vec` host-side (Titan), injects `company_id`, attaches
`Authorization: Bearer XIOMA_MCP_KEY` + `X-Xioma-User-Token` for the session's
user, and calls the **unchanged, internal** MCP servers. More host code and a
`tool_runner` pool, and job-card results cross the context window — but **no MCP
auth reshape and no inbound exposure**. (Custom tools *do* work on cloud Managed
Agents — this repo's `pull_allocation_snapshot` proves it; a common claim to the
contrary is wrong.)

#### Recommendation

Target **Design A** for the app MCP (per-user `mcp_oauth` vault), since the token
injects cleanly per user and it yields the thinnest host. **Hybrid is fine and
likely fastest to ship:** app MCP direct via vault (A), and keep **xioma-read
host-answered** (B) until we're ready to move embedding into it and expose it.
Design B alone is the zero-MCP-change path if inbound exposure is off the table.

---

## 3. How each subsystem ports

### 3.1 The bot contract stays byte-for-byte (zero change to `xas-ai-bot`)

The worker keeps `POST /run` with the same `RunRequest`
(`message`, `session_id`, `context{userId,companyDB,locale}`, `callback_url`,
`token`, `userToken`) → returns `202 {"status":"processing"}` → later POSTs the
same callback (`{status, text, html, usage}`). Internally `/run` now:

1. **Get-or-create** the Managed-Agent session for this `session_id`
   (§3.3). Capture `userToken` / `companyDB` / `locale` into the session's
   per-turn credential context.
2. Inject a **mid-session system message** carrying `tenant=companyDB`,
   `locale`, and `CURRENT DATE (UTC)` (replaces the `dynamic_prompt` date hack;
   no new agent version, no cache invalidation).
3. `events.send` the user message; ensure a `tool_runner` is attached for this
   session (§3.2).
4. Stream `events` until the session goes **idle**; collect the final
   `agent.message` text, the stashed/downloaded `html` artifact, and token
   `usage` (map platform usage → `{promptTokens, completionTokens, toolCalls}`).
5. POST the callback (keep the existing retry/backoff/DNS-pin logic verbatim).

### 3.2 Credentialed data tools (Design B: one `tool_runner` per active session)

*This section details the **host-answered** design. Under Design A the sandbox
calls the MCP directly via a per-user vault and this machinery is not needed for
that server.* Port `mcp_tools.py`'s client logic into the worker's tool answerers. For each
session the worker registers custom tools whose implementations **close over that
session's credential context** (the allocation agent does exactly this with
`make_pull_tool(lambda: _pull_for(session_id))`):

```python
tools = make_xas_tools(creds_for(session_id))          # index_lookup, get_job_cards, ...
runner = client.beta.sessions.events.tool_runner(session_id, tools=tools)
async for call in runner:
    ...   # runner invokes our async fns on agent.custom_tool_use, posts the result
```

Inside each answerer we reuse the existing, proven logic verbatim:
- `index_lookup`: embed `term`→`query_vec` via Titan (`embeddings.py`) unless
  `exact=True`; inject `company_id`; call xioma-read with
  `Authorization: Bearer XIOMA_MCP_KEY`.
- `get_job_cards`/`get_job_card`: attach `X-Xioma-User-Token` for this session's
  user; call xas-app-mcp. On an `UNAUTHORIZED` result, surface a
  token-expired error to the agent (and map it to the callback `error.code`).

**Critical invariants inherited from this repo** (they bite the same way):
- The `tool_runner` is **owned by the session lifecycle**, started before the
  first user message and cancelled on session end — never tied to the SSE/stream
  route. A `requires_action` idle with no answerer **never times out** (silent
  hang).
- **Every declared custom tool must have an answerer.** Declaration
  (`setup_xas_agent.py` `tools=[...]`) and implementation (worker) are built from
  one shared contract module (mirror `alloc_tools.py`), guarded by a
  `test_tool_contract.py`.
- `agents.update()` **preserves omitted array fields** → always send `tools` and
  `skills` explicitly on every setup run.

**Large results:** job-card queries can be big. Keep the existing `compact=true`
trim, return a **summary in-band**, and lean on the platform's large-tool-output
offload (>~100k chars → file + preview). For the common case (counts, short
lists) in-band rows are fine and let the sandbox pass them straight into the
render skill.

> **Multi-tenant concurrency — the one place we diverge from `web.py`.** The
> allocation worker is deliberately **single active session**. The dealership bot
> is **many concurrent users**. The ai-agent worker must therefore manage a
> **pool** of live sessions + one `tool_runner` per session (keyed by managed
> session id), not the singleton `_active`/`_answering` globals. This is the main
> structural change to the `web.py` template.

### 3.3 Session state without a checkpointer

The Managed session **is** the conversation — Anthropic stores the event history
durably, so LangGraph's Postgres checkpointer disappears. We keep only a tiny
**`bot_session_id → managed_session_id` map** host-side:

- v1: in-memory dict; on cold start, rebuild by `sessions.list(title=...)` (store
  `session_id` in the session `title`/metadata).
- durable option: a small Postgres table (or reuse the existing agent DB) so the
  map survives worker restarts. This is far smaller than the current checkpoint
  store — we persist an ID mapping, not the transcript.

`index_orientation` (company entity map, fetched once per new conversation today)
becomes either a host-answered tool the agent calls once, or — cleaner — a
**session-start system message** the worker injects at create time.

### 3.4 Presentation moves into the skill

`make_date_range` and the three renderers are **pure** (no network, no secrets),
so they ship as **skill code** (`date_tools.py`, `data_tools.py`,
`report_style.py`) alongside a `SKILL.md`, exactly like `xas_allocation/` ships
inside the allocation skill. The agent:
- runs `make_date_range` logic to build absolute UTC ranges (still "never write a
  date yourself");
- renders to an HTML/SVG file (`report.html`) using `report_style.py` (RTL/locale
  aware);
- the worker retrieves it via `files.list(scope_id=session_id)` +
  `files.download` and puts it in the callback `html` field — replicating
  today's `content_and_artifact` split (summary → context, HTML → the app), with
  the HTML kept **out of the model context**.

> Alternative kept on the table: render as **host-side custom tools** returning a
> short confirmation to the agent while the worker stashes the HTML for the
> callback. Simpler to preserve the exact contract; costs a little host
> complexity. Recommendation: **skill-code rendering** (thinner host, sandbox is
> good at file generation), fall back to host-side if the file round-trip proves
> awkward.

### 3.5 The system prompt ports almost verbatim

The prompt (workflow "resolve names → fetch data → answer", the strict
plain-text/locale formatting rules, the kind→filter-key mapping, "never show
system codes/UUIDs/raw field names", the "ask before rendering" viz rule) is
**model-agnostic** and moves into `setup_xas_agent.py`'s `system=`. Only the
Bedrock-specific mechanics fall away (cache points, temperature quirks). The 3
config variants (`v2-haiku/opus/sql-sonnet`) collapse to **one agent + per-session
model override** via `agent={"type":"agent_with_overrides","model":...}` — the
same mechanism `web.py` uses to pick opus/sonnet/haiku per session.

---

## 4. What we drop, and what stays

**Dropped / simplified (all handled by the platform now):**
- LangGraph + langchain + langchain-mcp-adapters and the whole **middleware
  stack** (prompt caching, context-editing, tool-arg-sanitize, dynamic prompt) —
  the platform owns the loop, context compaction, and caching.
- The **Postgres checkpointer** → replaced by platform-stored session events + a
  tiny id map.
- **Bedrock chat** + the **STS credential-refresh** dance + seccomp/`clone3`
  Docker workarounds + the **entrypoint.sh sidecar** topology → the worker is
  effectively the only moving part; Claude runs on Anthropic.
- The **LangSmith thread-limit monkeypatch** and Bedrock **empty-key
  tool-arg sanitize** — both are host/Bedrock-specific and vanish.
- `recursion_limit: 25` → replaced by a **task budget** (§5).

**Stays host-side (the irreducible core):**
- **xioma-read MCP** unchanged (Titan index; the worker becomes its client) and
  **Bedrock Titan embeddings** — the one remaining AWS dependency. (Future: swap
  the embedding model + re-embed the index to drop AWS entirely.)
- **xas-app-mcp** unchanged, still per-user-token authenticated.
- The **bot callback** contract and its retry/DNS logic.

---

## 5. Enhancements the platform unlocks ("even enhance")

These are net-new capabilities, not just parity. Prioritized:

1. **Outcomes (gradeable rubric).** The prompt is already a rubric written as
   prose ("verify every number", "resolve every code before presenting", "answer
   in the user's language", "no markdown"). Encode it as a `user.define_outcome`
   so a grader **enforces** it and the agent self-revises — a stronger, cheaper
   replacement for part of the LangSmith LLM-judge suite.
2. **Task budget** instead of `recursion_limit`. A token ceiling the agent can
   *see and pace against*, so it **wraps up** a long multi-lookup answer instead
   of being hard-cut mid-turn when the client disconnects.
3. **Permission policy (`always_ask`) on `bash`.** Defense in depth: auto-allow
   read/grep/file tools, gate raw `bash`. (Custom tools are gated in our own
   answerer code.)
4. **Mid-session system messages** for tenant / locale / current-date injection —
   no new agent version, no cache invalidation, replacing the `dynamic_prompt`
   date re-emit.
5. **Live previews (`content_delta`).** Stream the answer to the UI/bot as it
   generates instead of waiting for the whole buffered message.
6. **Scheduled deployments.** Nightly dealership KPI digests (stock levels, open
   service cards) with **no scheduler of our own** — each firing opens its own
   session; a manual-run endpoint tests the schedule immediately.
7. **Agent version pinning.** A prompt edit mints an immutable version; sessions
   pin for reproducibility and roll back on regression — replaces the
   docker-retag `/rollback` skill.
8. **Console trace URL** per session — replaces most of `/debug-answer`.
9. **Multiagent (later).** One subagent per data source (terminology vs job
   cards) fanning into a synthesizer for complex multi-part questions; capped at
   25 concurrent threads, one level of delegation.

---

## 6. Risks & open decisions (resolve before/while building)

| # | Decision | Leaning |
| --- | --- | --- |
| D-1 | Per-user token to a sandbox with zero egress | **Direct MCP via per-user vault (Design A)** is first-class and recommended — needs the MCP auth reshaped to one bearer/OAuth credential + MCP exposed to Anthropic + embedding moved into xioma-read. **Host-answered custom tools (Design B)** is the zero-MCP-change fallback. Hybrid (app MCP = A, xioma-read = B) likely ships fastest. |
| D-1a | Per-user token form | Move from custom `X-Xioma-User-Token` to **`mcp_oauth`** (auto-refresh) or `static_bearer` per user, so vault can inject it. Requires an OAuth/bearer path on the app MCP + gateway for the shared key. |
| D-2 | Where rendering runs | **Skill code → HTML file → worker downloads**; host-side custom tool as fallback. |
| D-3 | Embeddings | **Keep Titan host-side** in the worker (matches the pre-built index space). Re-embedding the index is required if the model ever changes. |
| D-4 | Session-id map durability | In-memory for v1; small Postgres table if worker restarts must preserve continuity. |
| D-5 | Large job-card results crossing context | `compact` + summary in-band + platform large-output offload; revisit if real queries blow the window. |
| D-6 | Concurrency model | **Session pool + tool_runner per session** (not the `web.py` singleton). |
| D-7 | Model & cost | Anthropic-hosted Opus/Sonnet/Haiku via per-session override; validate latency (sandbox spin-up) and cost vs today's Bedrock-Haiku default. |
| D-8 | Evals | Keep a harness that drives real sessions; move the rubric to **Outcomes**; decide whether LangSmith stays for traces or the console trace replaces it. |
| D-9 | Keep xioma-read as a separate process vs fold into the worker | **Keep separate** for v1 (least change); the worker just calls it. |

---

## 7. Phased rollout

- **Phase 0 — Spike (1–2 days).** Stand up a `setup_xas_agent.py` that creates a
  cloud env + minimal agent + a skill carrying `date_tools.py`/`report_style.py`.
  Prove one host-answered custom tool (`get_job_cards`) end-to-end with a
  per-session token via `tool_runner`. Confirm the sandbox is Anthropic's
  (`whoami` sanity check).
- **Phase 1 — Data plane (decide A vs B first, D-1).**
  - *Design A (app MCP):* stand up an OAuth/bearer path + gateway on the app MCP,
    expose it to Anthropic, provision a per-user vault, attach via `mcp_toolset`,
    bind `vault_ids` at session create. Move `query_vec` into xioma-read if taking
    it direct too.
  - *Design B (fallback / xioma-read):* port the five MCP tools into host answerers
    (reusing `mcp_tools.py` client + injection), Titan embedding for
    `index_lookup`. `test_tool_contract.py` for every declared custom tool.
- **Phase 2 — Presentation + prompt.** Ship the renderers + `make_date_range` in
  the skill; wire the HTML file → callback. Move the full system prompt into the
  agent. Reach output parity with today's `text`/`html`/`usage`.
- **Phase 3 — Bot contract shim.** Wrap it all behind the unchanged `POST /run`
  → callback worker with the concurrency pool + session-id map. Point a staging
  bot at it.
- **Phase 4 — Evals & cutover.** Run the existing eval questions against the new
  worker; add an **Outcome** rubric. Compare answer quality/latency/cost to the
  Bedrock build. Canary a tenant, then cut over.
- **Phase 5 — Enhancements.** Budgets, bash permission gate, live previews,
  scheduled KPI digests, version pinning.

---

## 8. Proposed repo layout for the port (mirrors this repo)

```
xas-managed-agent-qa/            # or a new branch of xas-ai-agent
  setup_xas_agent.py             # control plane: env + skill + agent (← setup_allocation_agent.py)
  web.py                         # run plane: /run→callback worker, session pool, tool_runners (← web.py)
  xas_tools.py                   # ONE contract: custom-tool declarations + host-side answerers (← alloc_tools.py)
  mcp_client.py                  # xioma-read/xas-app client + auth + company_id/query_vec injection (← mcp_tools.py)
  embeddings.py                  # Titan query embedding (host-side, unchanged)
  skills/xas-qa/
    SKILL.md                     # workflow, formatting, presentation procedure
    date_tools.py                # pure date range logic
    report_style.py + data_tools.py  # pure HTML/SVG renderers
  tests/
    test_tool_contract.py        # declaration == implementation, every tool answered
    test_mcp_client.py           # auth headers, injection, UNAUTHORIZED handling
    test_render.py               # HTML/SVG parity with today
```

---

### One-line summary

Keep the **bot contract** on the outside, move the **agent loop, context
management, and presentation into the Managed Agent** (renderers as **skill
code**), and reach the DMS data by **injecting each user's token via a per-user
vault** so the sandbox calls the MCP directly (**Design A** — recommended; needs
the MCP auth reshaped to one bearer/OAuth credential and exposed to Anthropic),
falling back to **host-answered custom tools** (**Design B** — the
`alloc_tools.py` pattern, zero MCP changes) where we don't want to expose a
server. Layer on the platform's **outcomes / budgets / permission gates /
scheduling** to come out ahead of the LangGraph build.
