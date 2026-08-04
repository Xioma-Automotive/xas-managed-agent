# XAS Allocation Agent

A Claude **Managed Agent** for Xioma Automotive, running against an
**Anthropic-hosted** sandbox. The agent's bash and file tools execute in
Anthropic's container; nothing on your machine runs them. Uses the Managed
Agents REST surface via the Python `anthropic` SDK, model `claude-opus-5`.

> The self-hosted variant is on `claude/agent-spec-managed-i6tn8r`. It runs the
> agent's shell as your own uid — in testing, an agent there enumerated every
> credential file on the host. These branches are alternatives, not a merge.

| File | Plane | Role |
| ---- | ----- | ---- |
| `setup_allocation_agent.py` | control (once) | Creates the cloud environment, uploads the skill **with the solver inside it**, creates the agent. Re-runnable: updates in place. |
| `web.py` + `static/index.html` | run | The only process. Session control, transcript, and the one custom tool the sandbox cannot answer for itself. |
| `alloc_tools.py` | both | The `pull_allocation_snapshot` contract — declared and implemented in one place. |
| `xas_allocation/` | — | The deterministic reference solver. Uploaded as part of the skill. |
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

## Run it as a Managed Agent (Anthropic-hosted)

```bash
uv sync
cp .env.example .env                  # fill in ANTHROPIC_API_KEY
uv run python setup_allocation_agent.py
#   paste the printed ALLOC_AGENT_ID / ALLOC_ENV_ID / ALLOC_SKILL_ID into .env

uv run uvicorn web:app --port 8000    # the only process — open localhost:8000
```

No worker, no environment key, no `.env.worker`. One process.

| Where | Holds | Runs |
| --- | --- | --- |
| `web.py` (here) | organization API key | the one custom tool |
| Anthropic's sandbox | nothing of yours | bash, file tools, the solver |

**If `.env` already holds self-hosted IDs**, clear all three `ALLOC_*` values
before running setup. The two sandbox types need separate resources, and setup
refuses to cross-wire them rather than producing an agent whose sessions queue
for a worker that will never arrive.

### How the solver reaches a sandbox we don't run

It ships **inside the skill**. `skill_files()` uploads `xas_allocation/` under
the skill directory alongside `SKILL.md`, and the platform materializes skills
into the sandbox. The package stays at the repo root — the bundle is synthesized
at upload time, so the tests and the sandbox run the same source.

The consequence to remember: **edit the solver and you must re-run
`setup_allocation_agent.py`**, or the sandbox keeps using the previous version
with nothing to tell you.

### Why the pull returns a seed instead of rows

The tool runs here; the agent runs in Anthropic's sandbox. Everything the tool
returns crosses into the agent's context, and a 120-order snapshot is ~100 KB of
JSON the agent never reads directly — the solver reads it.

So the tool returns a summary plus a `materialize` command, and the agent runs
that command to rebuild the snapshot in its own sandbox. The generator is seeded,
so the result is byte-identical to what the tool generated here; the same
determinism argument the core invariant rests on is what makes the shortcut
sound. The tool is still the pull interface — it decides the parameters and is
where a real XAS API plugs in. Only the transport differs.

When the real XAS pull exists (DECIDE-7) the rows stop being reproducible from a
seed, and the payload question comes back wanting a real answer.

### One session at a time

A product choice here rather than a constraint — with a cloud sandbox nothing
queues. Kept because the ledger and the planner's attention are both singular.
"New session" stops the current one; `/session/{id}/files` lists what the agent
wrote, and `/session/{id}/files/download` pulls it to `~/xas-alloc-outputs`.

### What "no outside connections" means here

The environment is created with `networking: limited` and an empty
`allowed_hosts`, so the sandbox reaches nothing. `allow_package_managers` stays
on because the agent needs `pip install ortools` for the solver.

Unlike the self-hosted variant, this is an actual boundary: the agent's shell is
in Anthropic's container, with no path to your filesystem, your credentials, or
your network.

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
