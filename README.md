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
| `skills/xas-allocation/SKILL.md` | — | The allocation skill: data model, cost model, procedure, steering contract, planner-report contract. |
| `phrasebook.py` | — | Builds `phrasebook.tsv` from `index.md` at deploy time. Host-side only; imports `normalize` from the skill so the two cannot drift. |
| `skills/xas-reporting/` | — | The reporting skill: `SKILL.md`, the tenant taxonomy `index.md` (source, never shipped), `resolve.py` that queries the table, and `dates.py` for period words. |
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

This build is a **runnable prototype**. Its data IS real XAS: one row of the
export's `orders.csv` is **one order for one car**, keyed by its own `OrderId`
(`502377`). There are no job-card lines and no `Quantity` column in this export,
so there is nothing to expand and no per-line grain to decide.
Supply is ONE
flat pool of vehicles, real and future together; there is no PO/PDN/slot layer,
and a vehicle is always exactly one car. Dates are real dates. The pull is a
**scenario directory of the real XAS export** (`orders.csv` + `vehicles.csv`),
read and translated host-side by `datasource.py` and mounted into the sandbox as
two JSON files; the planner picks which scenario when the session starts. The
agent can work the whole book or narrow it to a slice (`may_move.only` — a
model/month/order cut, a localized fix that leaves the rest alone). Every unresolved choice from the spec is a marked `DECIDE-n` (see
below), and every solver PARAMETER is in `xas_allocation/solver_config.yaml`.

## Two lanes, one agent

The agent does two jobs, and which one runs is decided by **skills**, not by
separate agents:

| Lane | Skill | Reads | Answers |
| --- | --- | --- | --- |
| Allocation repair | `xas-allocation` | `/workspace/orders.json` + `/workspace/vehicles.json` via the pull tool + `flatten` | which order gets which vehicle, what a repair costs, who is bumped |
| Reporting | `xas-reporting` | `phrasebook.tsv` in its own skill dir + the `xas-app-mcp` tools (LIVE dev system) | how many, which branch, what status — and charts |

Both skills are on the same session, so a planner can repair an allocation and
then ask for a chart without switching tools.

**The rule that makes that safe:** every allocation claim comes from running the
solver. Never from an `xas-app-mcp` tool, and never from a file the agent read
itself. Reporting reads a LIVE view of the business with no guarantee it agrees
with the pull, so an allocation number read from it would look right and not be
reproducible — the exact thing `plan = pure_function(snapshot, skill, override)`
exists to prevent. The system prompt forbids it by TOOLSET (there is no records
path left to forbid), `tests/test_agent_contract.py` pins the rule,
and `docs/evals/routing.md` is the hand-run behavioural check.

**Reporting vocabulary.** Dealerships rename things — in the shipped tenant the
code `Evaluation` displays as `Service Lead`. `xas-reporting` flattens the taxonomy into a
normalized phrasebook (one row per surface string, casefolded and stripped of
combining marks) so Hebrew typed without niqqud still matches, then resolves
exact-first, then loosely, then through other wordings it proposes and the grep
confirms, then `resolve.py --suggest` for a misspelling. A term that survives
all of that unresolved gets no answer: the skill makes the agent name it, offer
the nearest entries and ask, because the closest-looking code returns a
real-looking number nobody can tell is wrong.

The taxonomy itself ships **inside the `xas-reporting` skill** (DECIDE-16): one
tenant, so static config beats a per-session upload, at the cost of a redeploy
when it changes and no per-session choice of dealership. A second tenant moves it
back to a host-side mount. It ships **already flattened** — `setup_agent` renders
`index.md` into `phrasebook.tsv` at bundle time and ships only the table, so the
agent greps a file that is already there instead of spending its first turn
rebuilding one it cannot change.

**Period words** (`last week`, `last month`, `last 30 days`) resolve through
`dates.py`, which holds the three conventions that were otherwise re-derived
every turn: the dealership's clock is UTC+3 while the filter compares in UTC, the
week starts Monday, and the range is half-open. A phrase it does not know is a
question for the planner, never a guessed range.

## Reference-solver package (the deterministic core)

Lives in `xas_allocation/` — this is the "reference solver in the skill for
day-one" (§10); the canonical version moves to a tested, version-pinned repo
before real dealer data (DECIDE-9).

| Module | Deliverable | Role |
| ------ | ----------- | ---- |
| `decisions.py`   | —      | Every `DECIDE-1..16` — default, rationale and STATUS — surfaced at runtime. One is still OPEN, five are RETIRED. Decisions only: the numbers live in the config. |
| `solver_config.yaml` | — | **Every parameter the solver prices with**, in one file: the lateness exponent, the earliness weight, break cost, the priority steps, the churn sweep, the no-car cost and the version stamped on a saved plan. Read by `solver.py` alone. Editing it means re-running `setup_agent.py`. |
| `snapshot.py`    | §11.1  | The flattened, date-based solver snapshot (`orders/vehicles/allocations`) + JSON (de)serialization. |
| `flatten.py`     | §11.3  | Pure two-mounted-payloads → snapshot mapping (the "flatten + freeze" hop), and the only part of the data path that runs in the sandbox. Eligibility is a hard `sales_model` equality — no LLM judgment. |
| `solver.py`      | §11.2  | OR-Tools `SimpleMinCostFlow`: integer index tables (§4), §2 cost model, the free/pinned partition (§5), the **churn-price sweep**, deterministic read-back. Two halves — `partition` (who may move, no maths) and `_solve_one` (the arithmetic). |
| `session.py`     | §11.5  | The §8 per-turn loop; discrepancy map, whole-book state report (`current_state_report`), the finished **planner report** (`repair_and_report`). Steering is one combined override the agent carries forward — no ledger. |
| `overrides_schema.json` | §11.6 | The typed steering object the planner's NL compiles to (§6). |
| `../scenario_engine/`   | —     | **Standalone, outside the agent**: carves a solvable scenario out of the real export (`real_unallocated` / `real_delayed` / `real_mixed`, one shared `carve`) into `data/scenario-*/`. |
| `../datasource.py`      | —     | **Host-side pull** (DECIDE-7): `ScenarioSource` reads a scenario's two CSVs and `translate` — the ONE mapping — filters, counts every drop by reason and writes the two payloads. `web.py` calls it per session and mounts them. |
| `../tests/`      | §11.7  | 182 tests — the determinism invariant (`test_invariant.py`, also runnable standalone), the tool contract, flatten, the mapping, and one file per priced behaviour (bump, earliness, may_move, report). |

The skill knowledge (cost model §2 verbatim, encodings, procedure §8, steering
contract, infeasibility policy) is in `skills/xas-allocation/SKILL.md`.

## Run the prototype locally (no API key needed)

```bash
uv sync
uv run python -m datasource --list               # the scenarios available
uv run python -m datasource --census             # what the default scenario kept vs dropped
uv run python -m xas_allocation.session          # full §8 loop over that scenario
uv run python -m xas_allocation.decisions        # dump every open DECIDE + default
uv run pytest                                    # the gate — 182 tests, no network, no key
PYTHONPATH=. uv run python tests/test_invariant.py   # determinism proof (4/4), standalone
```

The three scenarios are committed, so nothing above re-carves anything.
**`COMMANDS.md` documents every command and flag in this repo**, including the
deploy path and the typical change→verify loops.

### Three scenarios cut from the real export

`data/vehicles.csv` + `data/orders.csv` are a real XAS export (3523 cars, 1641
orders). There is nothing to solve in it as it stands — every order already holds
a car, and a car's `status.name` *is* its allocation state — so three scripts
manufacture the decision, all running one `carve` in
`scenario_engine/real_export.py`:

```bash
uv run python -m scenario_engine.real_unallocated   # orders that lost their car
uv run python -m scenario_engine.real_delayed       # orders whose car turned late
uv run python -m scenario_engine.real_mixed         # both, competing for one pool
```

Every book they cut has **three classes of order** and they differ only in the
mix: orders with no car, orders whose car lands late, and orders that are
allocated and on time. The third is a *share* (`--on-time-pct`, 20% by default)
rather than a count, because it is the control group — what a plan leaves alone is
only readable if some orders needed nothing. It also fixes the scale: 8 disturbed
orders at a 20% share is a 10-order book, and the car subset follows from the
book, so neither size is a knob.

The rest of the knobs: how many orders to disturb, how far past the promise
(`--days-late`, delayed/mixed only, and only an inbound car can slip), how many
*further* cars to free by deleting an allocation (those orders leave the book, so
the pool gains slack), the available share of the cars, and how many sales models
to narrow to. Each writes
`data/scenario-{unallocated,delayed,mixed}/{orders,vehicles}.csv` in the export's
own shape. The first two are the mixed one with a count pinned to zero. Every
knob is a flag too, and one `--seed` makes a run reproducible.

Two things worth knowing before picking knobs. The export already ships 256 late
orders, so the on-time draw takes only orders that really are on time — otherwise
the share is a lie and `--late 100` comes back as 124; every run re-measures the
finished files and stops if one slipped in. And because eligibility is exact
sales-model equality across 66 models, a *small* book needs `--models` (default 2)
to pose any choice at all — the available percentage cannot fix 0.9 cars per
model. `COMMANDS.md` has every flag and the numbers behind that.

As committed, all three are **10-order books**: 8 with no car / 0 late / 2 on time,
0 / 8 / 2, and 4 / 4 / 2. A bare run of each script reproduces exactly that.

These directories ARE the pull as of 2026-08-27: `datasource.translate` reads
them and the fabricated world that used to stand in for them is gone. Each also
carries a `scenario.json` sidecar with the pull date, because no column does and
a wall clock would make static files mean something new tomorrow.

`session.py` prints the discrepancy map (what is late, and who holds no car at
all), the churn-price frontier, the hard-constraint
self-check and the finished planner report — first for a base repair, then after
three steering turns (mark an order urgent, narrow to one model, hold changes
down) — and the two on-time orders visibly stay put through all four. The three
scenarios are committed, so nothing needs re-carving.

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

The data is **not** bundled (it used to be). The pull is read host-side per
session and mounted into the sandbox as two files (next section). The consequence
to remember: **edit the solver package or `SKILL.md` and you must re-run
`setup_agent.py`**; re-carving a scenario does not need a re-deploy, because the
data is mounted rather than shipped.

### Why the pull mounts files instead of returning the rows

The source runs here; the agent runs in Anthropic's sandbox. Everything the
*tool* returns crosses into the agent's context, so dumping the rows would push
the whole book through the context window every pull.

So on session start `web.py` calls `datasource.get_source(scenario).pull()`
**host-side** and mounts the two translated payloads at
`alloc_tools.ORDERS_MOUNT_PATH` and `VEHICLES_MOUNT_PATH`
(`/workspace/orders.json`, `/workspace/vehicles.json` — about 130KB together for
the mixed scenario). The tool then returns only a summary plus a `flatten`
command that reads both files into `snapshot.json`. The rows travel as files, out
of the transcript entirely. The scenario scripts' *code* stays out of the sandbox;
only the translated *output* travels in.

`datasource.py` is the pull. `XAS_SCENARIO` sets the default scenario and the web
form's picker overrides it per session; `XAS_PULL_NOW` overrides the pull date for
a what-if. There is no live source and no credential on this path any more —
`uv run python -m datasource --census` prints the funnel for whichever scenario is
configured, and `--list` names them all.

The app MCP was this pull's source for a week (2026-08-20 → 08-27) and is not one
now: its list projection returned no `jobitems`, so every dev job card dropped and
the live pull came back empty. The change request behind it was closed by
dropping the source rather than widening the projection, and its two spec docs
went with it on 2026-08-30. The MCP tools the agent holds are the reporting
lane's and are unaffected.

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

## Decisions (`DECIDE-n`) — reviewed 2026-08-25, no longer all stubs

Run `uv run python -m xas_allocation.decisions` for the live register; its header
line counts what is genuinely undecided. **One is OPEN (5).** Two are settled in
shape but carry a value no planner has validated (3, 15). **Five are RETIRED**
(1, 2, 4, 11, 14) — built, reviewed and removed on 2026-08-26; they stay in the
register with what went wrong, so nobody makes them again. One is DEFERRED (10).
Summary:

| # | Decision | Default | Status |
|---|----------|---------|--------|
| 1 | Aging term: additive vs multiplicative | **deleted** — the whole escalation term went; all three fields it read are zero on every real row | RETIRED |
| 2 | Time-fence boundaries | **deleted** — it fired before the authorisation check and cancelled bumps a planner had asked for; a settled order is protected by not being in the free set | RETIRED |
| 3 | Break cost: disturbing a kept promise | ONE `break_cost=200`, charged only when the displaced order's car was arriving on time. The hard/soft split retired 2026-08-27 — it read a real-vs-future binding the export does not carry. Config, not steering | value unvalidated |
| 4 | Pin mechanism | **deleted** with the instruction pin: deferring an order is a NEW PROMISED DATE, which lateness and earliness already price | RETIRED |
| 5 | Managed Agents session-persistence API | steering is one combined override carried in the conversation; durable host-side store deferred | **OPEN** |
| 6 | xas-code MCP liveness pattern | none, and there will not be one — the pull happens host-side before the session exists | settled (not applicable) |
| 7 | XAS API data contract | `datasource.ScenarioSource` reads a scenario directory of the real export host-side — two CSVs, no credential, no MCP. `AppMcpSource` and the fabricated world are both gone | settled and unblocked |
| 8 | Infeasibility strategy | large finite costs, never walls — since the pins went, `no_car_cost` is the only one | settled |
| 9 | Solver repo location + versioning | in-repo `xas_allocation/`; `solver_config.yaml` pins the version. Extraction is triggered by the first NON-DEV tenant | settled |
| 10 | `reserved_for_customer` eligibility | a `Reserved-*` car is out of the pool entirely — supply for NO ONE. Modelling it as earmarked supply is the upgrade | DEFERRED |
| 11 | Reschedule fairness (`times_rescheduled`) | **deleted** — nothing ever wrote the field, so it never fired on a real row. Needs the approved write-back first | RETIRED |
| 12 | Future vehicle = soft supply | one pool of real ∪ future; the classification is the whole distinction — no slot step and no committed flag | settled |
| 13 | Bumping an untouched order | never without explicit planner authorization (`may_move.also`); the agent asks who may be bumped | settled |
| 14 | Time-scale granularity | **deleted** — nobody asked for days/weeks/months, and it cost a rounding helper, a threaded argument, report phrasing and a test file to stop the solver telling three days from six | RETIRED |
| 15 | Earliness penalty | `early_weight=0.15`, linear — a little early is cheap, a lot early is costly; lateness always dominates; earliness only | value unvalidated |
| 16 | Where the tenant taxonomy comes from | bundled — rendered from `index.md` into `phrasebook.tsv`, which ships inside the `xas-reporting` skill; a SECOND TENANT flips it back to a host-side mount | settled |

**Not in this prototype (deferred to reviewed PRs, per spec):** the CP-SAT + LNS
escape hatch for *coupled* orders (fleet all-or-nothing, transport batching), and
any new hard constraint. The prompt moves priority and who may move; a human
moves the model — and a human moves `solver_config.yaml`, which is a reviewed
change with tests, never something a live session does.
