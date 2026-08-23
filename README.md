# XAS Agent

A Claude **Managed Agent** for Xioma Automotive, running against an
**Anthropic-hosted** sandbox. The agent's bash and file tools execute in
Anthropic's container; nothing on your machine runs them. Uses the Managed
Agents REST surface via the Python `anthropic` SDK, model `claude-opus-4-8` (Opus 5 / Sonnet 5 / Haiku 4.5 selectable per session).

> The self-hosted variant is on `claude/agent-spec-managed-i6tn8r`. It runs the
> agent's shell as your own uid — in testing, an agent there enumerated every
> credential file on the host. These branches are alternatives, not a merge.

| File | Plane | Role |
| ---- | ----- | ---- |
| `setup_agent.py` | control (once) | Creates the cloud environment, uploads the skill **with the solver inside it**, creates the agent. Re-runnable: updates in place. |
| `web.py` + `static/index.html` | run | The only process. Session control, transcript, and the one custom tool the sandbox cannot answer for itself. |
| `alloc_tools.py` | both | The `pull_allocation_snapshot` contract — declared and implemented in one place. |
| `xas_allocation/` | — | The deterministic reference solver. Uploaded as part of the skill. |
| `skills/xas-allocation/SKILL.md` | — | The skill: cost model, procedure, steering contract. |
| `COMMANDS.md` | — | Every runnable command with its parameters — data generation knobs, the test gate, deploy, the typical loops. |

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

> `plan = pure_function(data_snapshot, skill, override)`

If the mapping, graph, costs, or pins can't be regenerated from those three
inputs, state has leaked into model memory and determinism is lost. That's the
bug guarded against everywhere. Steering is a single combined **override** object
the agent carries forward — no ledger, no replay. `tests/test_invariant.py`
proves it holds even after the sandbox is discarded: re-pull, re-apply the same
override, get the same plan.

This build is a **runnable prototype against fabricated data** shaped like XAS
(`PO → PDN → Vehicle`, `Customer → SO` with vehicle order **rows**, supply =
vehicles ∪ PO-line slots, real dates). The pull comes
from a callable data source (`datasource.py`) — a standalone `scenario_engine/`
fabricates the world by default, the real XAS endpoint by config; the agent can
work the whole book or a
**scope** (a customer/month/PO slice — a localized fix that leaves the rest
pinned). Every unresolved choice from the spec is a marked `DECIDE-n` — stubbed
with a labelled default (see below).

## Two lanes, one agent

The agent does two jobs, and which one runs is decided by **skills**, not by
separate agents:

| Lane | Skill | Reads | Answers |
| --- | --- | --- | --- |
| Allocation repair | `xas-allocation` | `/workspace/pull.json` via the pull tool + `flatten` | which order gets which vehicle, what a repair costs, who is bumped |
| Reporting | `xas-reporting` | `index.md` in its own skill dir + the `xas-app-mcp` read tools (LIVE) | how many, what status — and charts |

Both skills are on the same session, so a planner can repair an allocation and
then ask for a chart without switching tools.

**The rule that makes that safe:** every allocation claim comes from running the
solver. Never from an `xas-app-mcp` tool, and never from a file the agent read
itself. Reporting reads the LIVE system, which is a different view of the
business with no guarantee it agrees with the pull — so an allocation number
taken from it would look right and not be reproducible, the exact thing
`plan = pure_function(snapshot, skill, override)` exists to prevent. The system
prompt forbids it by toolset and `tests/test_agent_contract.py` pins the rule —
but whether the agent OBEYS it is model behaviour, and nothing here checks that.

**Reporting vocabulary.** Dealerships rename things — in the shipped tenant the
code `Service` displays as `Distinct_name`. `xas-reporting` flattens the taxonomy into a
normalized phrasebook (one row per surface string, casefolded and stripped of
combining marks) so Hebrew typed without niqqud still matches, then resolves
exact-first, then loosely, then through other wordings it proposes and the grep
confirms, then `phrasebook.py --suggest` for a misspelling. A term that survives
all of that unresolved gets no answer: the skill makes the agent name it, offer
the nearest entries and ask, because the closest-looking code returns a
real-looking number nobody can tell is wrong.

The taxonomy itself ships **inside the `xas-reporting` skill** as
`index.md` (DECIDE-16): one tenant, so static config beats a per-session upload,
at the cost of a redeploy when it changes and no per-session choice of
dealership. A second tenant moves it back to a host-side mount.

## Reference-solver package (the deterministic core)

Lives in `xas_allocation/` — this is the "reference solver in the skill for
day-one" (§10); the canonical version moves to a tested, version-pinned repo
before real dealer data (DECIDE-9).

| Module | Deliverable | Role |
| ------ | ----------- | ---- |
| `decisions.py`   | —      | Every `DECIDE-1..16` stub + its labelled default, surfaced at runtime. |
| `snapshot.py`    | §11.1  | The flattened, date-based solver snapshot (`orders/units/incumbent`) + JSON (de)serialization. |
| `flatten.py`     | §11.3  | Pure rich-pull → snapshot mapping (the "flatten + freeze" hop). Eligibility is a hard `sales_model` equality — no LLM judgment. |
| `solver.py`      | §11.2  | OR-Tools `SimpleMinCostFlow`: integer index tables (§4), §2 cost model, data/instruction pins (§5), the **λ sweep**, deterministic read-back. Also `repairability()` — is a broken order even re-slottable, or locked in? |
| `session.py`     | §11.5  | The §8 per-turn loop; discrepancy map (fixable vs locked-in), data-prep flow chart, the finished **planner report** (`repair_and_report`). Steering is one combined override the agent carries forward — no ledger. |
| `overrides_schema.json` | §11.6 | The typed steering object the planner's NL compiles to (§6). |
| `../scenario_engine/`   | —     | **Standalone, outside the agent**: fabricates the rich PO→PDN→Vehicle / SO-with-rows dataset (good → disrupted). |
| `../datasource.py`      | —     | **Host-side pull interface** (DECIDE-7): `ScenarioEngineSource` (the fake, default) / `XASApiSource` (real, stubbed), selected by `XAS_DATA_SOURCE`. `web.py` calls it and mounts the result into the sandbox. |
| `../tests/`      | §11.7  | 50 tests — the determinism invariant (`test_invariant.py`, also runnable standalone), the tool contract, flatten, and one file per priced behaviour (bump, earliness, reschedule fairness, scope, time-scale, report, datasource). |

The skill knowledge (cost model §2 verbatim, encodings, procedure §8, steering
contract, infeasibility policy) is in `skills/xas-allocation/SKILL.md`.

## Run the prototype locally (no API key needed)

```bash
uv sync
uv run python -m scenario_engine.generate        # (re)fabricate data/pull.json + baseline
uv run python -m xas_allocation.session          # full §8 loop over the repo dataset
uv run python -m xas_allocation.decisions        # dump every open DECIDE + default
uv run pytest                                    # the gate — 50 tests, no network, no key
PYTHONPATH=. uv run python tests/test_invariant.py   # determinism proof (4/4), standalone
```

`scenario_engine.generate` takes knobs (`--seed`, `--customers`, `--orders`,
`--spare-ratio`, `--delay-days`) for varying the starting conditions —
**`COMMANDS.md` documents every command and flag in this repo**, including the
deploy path and the typical change→verify loops.

`session.py` prints the discrepancy map (what the disruption broke), the
data-prep flow chart, the λ-sweep Pareto frontier, the hard-constraint
self-check, and a reason-coded change list — first for a base repair, then after
a steering turn (defer an order, prefer Colmobil, set λ). `data/pull.json` is
committed, so nothing above needs the engine re-run.

## Run it as a Managed Agent (Anthropic-hosted)

```bash
uv sync
cp .env.example .env                  # fill in ANTHROPIC_API_KEY
uv run python setup_agent.py
#   paste the printed ALLOC_AGENT_ID / ALLOC_ENV_ID / ALLOC_SKILL_ID / REPORTING_SKILL_ID into .env

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

The solver ships **inside the skill**. `skill_files()` uploads `xas_allocation/`
alongside `SKILL.md`, and the platform materializes skills into the sandbox. The
package stays at the repo root — the bundle is synthesized at upload time, so the
tests and the sandbox run the same source.

The dataset is **not** bundled (it used to be). The pull comes from a callable
data source, fetched host-side per session and mounted into the sandbox as a file
(next section). The consequence to remember: **edit the solver package or
`SKILL.md` and you must re-run `setup_agent.py`**; regenerating
`data/pull.json` no longer needs a re-deploy, because it's fetched live.

### Why the pull mounts a file instead of returning the rows

The data source runs here; the agent runs in Anthropic's sandbox. Everything the
*tool* returns crosses into the agent's context, so dumping the rows would push
the whole dataset through the context window every pull.

So on session start `web.py` calls `datasource.get_source().pull()` **host-side**,
uploads the result, and mounts it into the sandbox as a file at
`alloc_tools.MOUNT_PATH` (`/workspace/pull.json`). The tool then returns only a
summary plus a `flatten` command that reads that mounted file into
`snapshot.json`. The rows travel as a file, out of the sandbox's sight when
fetched and out of the transcript when read. The scenario engine's *code* (and any
XAS credential) stays out of the sandbox; only the fetched *output* travels in.

`datasource.py` is the pull interface. `XAS_DATA_SOURCE=scenario` (default) uses
the fabricated dataset — offline, no credentials; `XAS_DATA_SOURCE=xas` uses the
real endpoint (DECIDE-7, stubbed until it exists). Either way the `flatten`
contract is unchanged; only the source of the rows differs. Because the endpoint
is called host-side, the sandbox's zero-egress policy is untouched.

### One session at a time

A product choice here rather than a constraint — with a cloud sandbox nothing
queues. Kept because the steering override and the planner's attention are both
singular.
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
| 2 | Time-fence boundaries | frozen ≤14d, slushy 15–42d, liquid >42d |
| 3 | Committed `location_state` | `{bonded, pdi}` |
| 4 | Pin mechanism | inf-cost (soft) for instruction pins; pre-commit for data pins |
| 5 | Managed Agents session-persistence API | steering is one combined override carried in the conversation; durable host-side store deferred |
| 6 | xas-code MCP liveness pattern | single `directory_tree` at start (skipped in prototype) |
| 7 | XAS API data contract | callable `datasource.py` (host-side): `scenario_engine/` fake by default, real XAS by config (see `docs/mcp-response-schema.md`) |
| 8 | Infeasibility strategy | high-cost soft pins (always returns; conflict shows as a cost line) |
| 9 | Solver repo location + versioning | in-repo `xas_allocation/`; skill pins `SOLVER_VERSION` |
| 10 | `reserved_for_customer` eligibility | ignored (deferred; not in the minimal build) |
| 11 | Reschedule fairness (`times_rescheduled`) | `γ=0.75` escalation on W(o) — protect an already-bumped order from being delayed again |
| 12 | PO-line slot committed-ness | a slot is `location_state='future'` → never committed until it explodes into vehicles |
| 13 | Bumping an untouched order | never without explicit planner authorization (the `bump` override); the agent asks who may be bumped |
| 14 | Time-scale granularity | planner knob `time_scale` (days/weeks/months); day-gaps rounded **up** to whole units; fence stays in days; default days |
| 15 | Earliness penalty | `EARLY_WEIGHT=0.15`, linear — a little early is cheap, a lot early is costly; lateness always dominates; earliness only |

**Not in this prototype (deferred to reviewed PRs, per spec):** the CP-SAT + LNS
escape hatch for *coupled* orders (fleet all-or-nothing, transport batching), and
any new hard constraint. The prompt moves weights and pins; a human moves the
model.
