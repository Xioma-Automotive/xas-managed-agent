# XAS Allocation Agent

A Claude **Managed Agent** for Xioma Automotive, running against a
**self-hosted** sandbox: `worker.py` on your own machine claims each session from
Anthropic's work queue and executes the agent's tool calls locally. Uses the
Managed Agents REST surface via the Python `anthropic` SDK, model
`claude-opus-5`.

| File | Plane | Role |
| ---- | ----- | ---- |
| `setup_allocation_agent.py` | control (once) | Creates the self-hosted environment, uploads the skill, creates the agent. Re-runnable: updates in place. |
| `worker.py` | run | The sandbox. Serves every session's tool calls. Holds the environment key only. |
| `web.py` + `static/index.html` | run | Session control and transcript. Holds the organization API key. |
| `alloc_tools.py` | both | The `pull_allocation_snapshot` contract — declared and implemented in one place. |
| `xas_allocation/` | — | The deterministic reference solver. |
| `skills/xas-allocation/SKILL.md` | — | The skill: cost model, procedure, steering contract. |

The split is **control** (create the agent and environment once — persistent,
versioned resources referenced by ID forever after) and **run** (open a session
per conversation and drive it). Never call `agents/environments create()` in the
per-conversation path.

---

## What it does

Helps a planner **repair** a vehicle-to-order allocation
after a disruption (delayed shipment, changed inbound, manual steering). It does
**not** allocate by reasoning — it translates the situation + planner
instructions into inputs for a **deterministic min-cost-flow solver**, runs the
solver, and explains the result.

**Core invariant — the whole design in one line:**

> `plan = pure_function(data_snapshot, skill, ledger)`

If the mapping, graph, costs, or pins can't be regenerated from those three
inputs, state has leaked into model memory and determinism is lost. That's the
bug guarded against everywhere. `tests/test_invariant.py` proves it holds even
after the sandbox is discarded and the ledger is replayed from disk.

This build is a **runnable prototype against synthetic data** shaped like XAS
bins. Real MCP data wiring and the production solver repo come later. Every
unresolved choice from the spec is a marked `DECIDE-n` — not guessed, but stubbed
with a clearly-labelled default and surfaced (see below).

## Reference-solver package (the deterministic core)

Lives in `xas_allocation/` — this is the "reference solver in the skill for
day-one" (§10); the canonical version moves to a tested, version-pinned repo
before real dealer data (DECIDE-9).

| Module | Deliverable | Role |
| ------ | ----------- | ---- |
| `decisions.py`   | —      | Every `DECIDE-1..9` stub + its labelled default, surfaced at runtime. |
| `synth_data.py`  | §11.1  | Seeded synthetic pull: orders, inbound units, a complete incumbent plan, and a disruption to repair. |
| `spec_match.py`  | §11.3  | Rule-driven `is_compatible()` + residual-resolution hook with **cached-decision write-back** (the one place LLM judgment can leak). |
| `solver.py`      | §11.2  | OR-Tools `SimpleMinCostFlow`: integer index tables (§4), §2 cost model, data/instruction pins (§5), the **λ sweep**, deterministic read-back. |
| `ledger.py`      | §11.4  | Append-only override store; replay-with-TTL fold into one combined override. The ledger **is** the session. |
| `session.py`     | §11.5  | The §8 per-turn loop; emits a **reason-coded change list**, not a bare plan. |
| `overrides_schema.json` | §11.6 | The typed steering object the planner's NL compiles to (§6). |
| `tests/test_invariant.py` | §11.7 | Determinism invariant: same plan across two runs **and** across sandbox discard. |

The skill knowledge (cost model §2 verbatim, encodings, procedure §8, steering
contract, infeasibility policy) is in `skills/xas-allocation/SKILL.md`.

## Run the prototype locally (no API key needed)

```bash
uv sync
uv run python -m xas_allocation.session          # full §8 loop over synthetic data
uv run python -m xas_allocation.decisions        # dump every open DECIDE + default
PYTHONPATH=. uv run python tests/test_invariant.py   # determinism proof (5/5)
```

`session.py` prints the λ-sweep Pareto frontier (changes vs weighted late-days),
the hard-constraint self-check, and a reason-coded change list — first for a base
repair, then after a steering turn (defer an order, prefer Colmobil, set λ).

## Run it as a Managed Agent (self-hosted)

The sandbox is **your machine**. `worker.py` claims each session from Anthropic's
work queue and executes the agent's tool calls locally, which is what lets the
agent `import xas_allocation` directly instead of being handed the solver's
source to retype. Determinism is the reference solver's, never the model
re-deriving it.

```bash
uv sync
cp .env.example .env                  # fill in ANTHROPIC_API_KEY
uv run python setup_allocation_agent.py
#   paste the printed ALLOC_AGENT_ID / ALLOC_ENV_ID / ALLOC_SKILL_ID into .env
#   then create .env.worker with ALLOC_ENV_ID + ANTHROPIC_ENVIRONMENT_KEY
#   (Console: Workspace > Environments > your env > Generate key)

uv run python worker.py               # terminal 1 — the sandbox
uv run uvicorn web:app --port 8000    # terminal 2 — the UI, then open localhost:8000
```

Three processes, three trust levels:

| Process | Holds | Runs |
| --- | --- | --- |
| `setup_allocation_agent.py`, `web.py` | organization API key | no tool calls |
| `worker.py` | environment key only | every tool call |
| — | — | there is no fourth: no broker, no vault, no container |

`worker.py` refuses to start if it finds `ANTHROPIC_API_KEY` in its environment,
which is why `.env` and `.env.worker` are separate files.

### One session at a time

`EnvironmentWorker.run()` serves one session to completion before claiming the
next, so a second live session would queue behind the first with nothing to show.
"New session" in the UI therefore stops the current one and moves its working
files from `~/xas-alloc-sandbox` into `~/xas-alloc-sessions/<session_id>/`. That
archive is what the session list points at.

### The sandbox lives outside the repo

`~/xas-alloc-sandbox`, not `./sandbox`. The file tools confine themselves to the
workdir, but `bash` does not — and a sandbox sited inside the repo puts `.env`,
which holds the organization API key, one `cd ..` away from a shell the agent
controls. Override with `ALLOC_SANDBOX_ROOT` if you must, but keep it out of the
repo.

At the start of every session the worker copies `xas_allocation/` and
`tests/test_invariant.py` into the workdir, because the system prompt promises
the solver is importable there. Copied rather than symlinked: the file tools are
symlink-aware and reject links resolving outside the workdir. Skip this and the
agent goes hunting — and `find /` outlives the bash tool's 120s timeout, which
takes its shell with it.

### The data pull is a tool, not a file read

The agent calls `pull_allocation_snapshot` (declared and implemented once, in
`alloc_tools.py`). The worker answers it on the host: it writes `snapshot.json`
into the sandbox and returns a summary — the disruption, the orders it freed, and
the dealer-name → `customer_id` map the §6 steering contract needs. The rows stay
on disk; the solver reads the file, the conversation does not.

### What "no outside connections" does and does not mean

The builtin toolset is `bash, read, write, edit, glob, grep` — there is no fetch
or search tool, and no credential is attached to anything, so **no tool offers
egress**. But the tools run as your host process, so `bash` inherits your host's
network. A real egress jail (container + proxy allow-list) is a later adaptation;
until then this is a local prototype, not an isolation boundary.

The agent never writes back to XAS — the plan is a proposal the planner approves.

## Open decisions (`DECIDE-n` — stubbed defaults, NOT settled answers)

Run `uv run python -m xas_allocation.decisions` for the live list. Summary:

| # | Decision | Prototype default |
|---|----------|-------------------|
| 1 | Aging term: additive vs multiplicative | additive into W(o) |
| 2 | Time-fence boundaries | frozen ≤2w, slushy 3–6w, liquid >6w |
| 3 | Commit-point unit states | `{shipped, in_prep}` |
| 4 | Pin mechanism | inf-cost (soft) for instruction pins; pre-commit for data pins |
| 5 | Managed Agents session-persistence API | ledger is a local JSON artifact; platform persistence not assumed |
| 6 | xas-code MCP liveness pattern | single `directory_tree` at start (skipped in synthetic prototype) |
| 7 | XAS API data contract | synthetic generator; invented field schema stands in |
| 8 | Infeasibility strategy | high-cost soft pins (always returns; conflict shows as a cost line) |
| 9 | Solver repo location + versioning | in-repo `xas_allocation/`; skill pins `SOLVER_VERSION` |

**Not in this prototype (deferred to reviewed PRs, per spec):** the CP-SAT + LNS
escape hatch for *coupled* orders (fleet all-or-nothing, transport batching), and
any new hard constraint. The prompt moves weights and pins; a human moves the
model.
