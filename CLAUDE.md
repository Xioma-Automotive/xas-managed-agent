# XAS Agent — working notes

A Claude **Managed Agent** on an **Anthropic-hosted (cloud)** sandbox. Nothing
here runs the agent's tools; `web.py` is the only process. Read `README.md`
first for architecture and run order; this file covers what isn't obvious from
the code.

**One agent, two skills.** Specialisation lives in the skills, not in separate
agent objects: `xas-allocation` drives the deterministic solver,
`xas-qa` answers reporting questions over the mounted job-card records. Both are
declared on the single agent behind `ALLOC_AGENT_ID`; `setup_agent.py` sends both
every time. The reporting lane's own agent was never created — it exists only as
a skill.

The self-hosted variant lives on `claude/agent-spec-managed-i6tn8r`. It exists
because a self-hosted sandbox runs tools as your own uid — an agent there
enumerated every credential file on the host through `bash`, which is what
prompted this branch. Don't merge the two; they are alternatives.

Design docs: `docs/superpowers/specs/`, plans in `docs/superpowers/plans/`,
platform notes in `docs/managed-agents-adoption.md`.

## The invariant everything serves

> `plan = pure_function(data_snapshot, skill, override)`

If the mapping, graph, costs, or pins can't be regenerated from those three
inputs, state has leaked into model memory and determinism is gone. Concretely:

- **The pull is the data snapshot.** It comes from a callable source
  (`datasource.py`, host-side): the `scenario_engine/` fake fabricates
  `data/pull.json` (PO→PDN→Vehicle, Customer→SO with vehicle order rows) by
  default, or the real XAS endpoint by config (DECIDE-7). `web.py` fetches it and
  mounts it into the sandbox as a file; `flatten` explodes SOs into rows + unions
  the supply (vehicles ∪ PO-line slots) into the `orders/units/incumbent` snapshot.
  The *same* pull backs every turn of a repair cycle — re-applying the same
  override against a different pull is not the same turn.
- **The override is the session.** Steering is one combined override object
  (weights / pins / forbid / lambda / scope / bump) the agent edits in place and
  carries forward — no ledger, no replay. Same snapshot + same override →
  byte-identical plan; the sandbox is a performance convenience. Durable
  cross-session persistence of that override is deferred (DECIDE-5) — the real fix
  is a host-side store, not the ephemeral sandbox.
- **Flatten is pure code, not judgment.** Eligibility is a hard `sales_model`
  equality — there is no LLM spec-residual left to cache. If the rich→snapshot
  mapping were re-derived by the model each turn, that is the leak this guards
  against; `flatten.py` keeps it deterministic.

`tests/test_invariant.py` proves this holds across a sandbox discard, and
`tests/test_agent_contract.py` pins the two-skill wiring and the mount namespace.
Both run host-side and need no API key — which is why `setup_agent.py` builds its
client lazily rather than at import.

### Trust levels

| Where | Holds | Runs |
| --- | --- | --- |
| `web.py` here | organization API key (`.env`) | the one custom tool, and the three host-side fetches that become mounts |
| Anthropic's sandbox | nothing of ours | bash, file tools, the solver |

The agent's shell is on Anthropic's side, so it has no path to this host's
filesystem, credentials, or network. That is the whole reason this branch
exists.

**A custom tool is answered by the client wherever the sandbox lives.** That is
the one host-side obligation left: `web.py` runs a `tool_runner` task per session
answering `pull_allocation_snapshot`, and leaves every other tool name for the
cloud sandbox. The credentialed data pull (DECIDE-7) lives here too — `web.py`
calls `datasource.get_source()` host-side and mounts the result as a file, so the
XAS endpoint and its credential never touch the sandbox.

## Invariants that bite if you change them

- **The tool contract has exactly one definition.** `alloc_tools.py` holds
  `PULL_TOOL` (what the agent declares) *and* `make_pull_tool` / the module-level
  `pull_allocation_snapshot` (what `web.py` registers per session), all built from
  the same constants. Splitting them is how you get an `agent.custom_tool_use`
  nothing answers — which parks the session on a `requires_action` idle that
  **never times out**, so the failure looks like a hang, not an error.
  `tests/test_tool_contract.py` guards the wiring.
- **`web_search` / `web_fetch` are disabled on the agent.** `TOOLS` in
  `setup_agent.py` keeps the rest of `agent_toolset_20260401` and turns
  those two off per-tool. Everything the plan may depend on arrives in the pull, so
  a web lookup could only add state the snapshot doesn't hold — the invariant's
  first input stops being a snapshot. (The cloud environment already has no egress;
  this also keeps the two tools out of the agent's context.)
- **The tool answerer is owned by the session, not the browser.** `web.py` starts
  its `tool_runner` task when it creates the session and cancels it on stop. Tie
  it to the event-stream route instead and closing the tab hangs the next pull
  forever.
- **The skill bundles carry code, NOT data.** `skill_files(skill_dir, package)`
  builds both: `xas-allocation/` + the `xas_allocation` package, and `xas-qa/`
  (SKILL.md + `phrasebook.py`). All three datasets are mounted per session by
  `web.py` — the pull from `datasource.get_source()`, the taxonomy from
  `get_taxonomy(name)`, the records from `get_records()`. **Change either skill
  or the solver package and you must re-run `setup_agent.py`**; regenerating any
  dataset does not need a re-deploy.
- **Three mounts, and the namespace is load-bearing.** `/workspace/pull.json` is
  the allocation snapshot; `/workspace/reports/index.md` and
  `/workspace/reports/jobcards.json` are the reporting lane's. They are
  namespaced so the system prompt can forbid a **path**: every allocation claim
  comes from the solver, never from reading the records. With both lanes in one
  sandbox that rule is the only thing standing between a planner and a plausible,
  irreproducible answer — `tests/test_agent_contract.py` pins it, and
  `docs/evals/routing.md` question 4 is the behavioural gate.
- **The two datasets are not one world.** `pull.json` holds VSOs;
  `jobcards.json` holds Service job cards, fabricated by a different mechanism on
  purpose (decided 2026-08-18). They describe overlapping business objects with
  no guarantee they agree. Do not let the current disjoint fixtures stand in for
  the rule above.
- **The pull mounts a file, not a seed, and not the rows in-band.** The source
  runs here; the agent runs there; everything the *tool* returns crosses into its
  context. So the tool returns only a summary + a `flatten` command; the rows
  travel as the mounted file (fetched host-side, out of the sandbox's sight) and
  `flatten` reads them there — nothing dumps ~KBs of JSON into the transcript. The
  scenario engine's *code* (and any XAS credential) stays out of the sandbox; only
  the fetched *output* travels in. The summary still carries the customer-name →
  `customer_id` map, because §6 steering needs it to compile "prefer Colmobil" into
  an override.
- **`flatten_command` searches from `.`/`/workspace`, never from `/`.** The solver
  lands wherever the platform puts skills, so the command self-locates
  `xas_allocation/flatten.py` — but bounded to the sandbox tree. (The pull is *not*
  searched for: it's read from the known `MOUNT_PATH` the host mounted it at.) An
  unbounded `find /` exceeds the 120s bash timeout and kills the agent's shell;
  that is not hypothetical, it happened on the self-hosted build.
- **`agents.update()` preserves omitted array fields.** `setup_agent.py`
  always sends `tools` and `skills` explicitly. Changing `PULL_TOOL` without
  re-running setup does nothing.
- **Cloud and self-hosted resources are not interchangeable.** `check_environment_type()`
  refuses to update a self-hosted environment from this branch. Cross-wire them
  and sessions queue forever for a worker that isn't coming.

## Open decisions

`DECIDE-1..15` are stubbed defaults, not settled answers. Run
`uv run python -m xas_allocation.decisions` for the live list. The big ones for
anyone touching this: DECIDE-14 (`time_scale` knob — the solver reasons at
days/weeks/months, rounding gaps UP; changes the plan, fence stays in days),
DECIDE-15 (earliness is priced — linear + small so lateness dominates; a car
months-early is not a win), DECIDE-7 (no real XAS API yet — the pull is a callable
`datasource.py`, the `scenario_engine/` fake by default, real XAS by config,
shaped per `docs/xasdatamodel.md`), DECIDE-3 (which
`location_state` counts as committed), DECIDE-9 (the solver lives in-repo; it
moves to a version-pinned repo before real dealer data), DECIDE-5 (no durable
session persistence assumed — steering is one combined override carried in the
conversation; a host-side store is the deferred real fix), DECIDE-10
(reserved_for_customer eligibility, deferred).

Not in the prototype, per spec: the CP-SAT + LNS escape hatch for *coupled*
orders, and any new hard constraint. **The prompt moves weights and pins; a human
moves the model** — a new constraint is a reviewed PR with tests, never a
live-session mutation.

## Verifying a change

```bash
uv run python -m scenario_engine.generate           # (re)fabricate data/pull.json
uv run pytest                                       # engine, flatten, contracts, phrasebook, determinism
PYTHONPATH=. uv run python tests/test_invariant.py  # the invariant, standalone (4/4)
uv run ruff format . && uv run ruff check .
```

Tests need no credentials and no network — the tool and flatten are exercised
in-process, and `data/pull.json` is committed so the suite runs without the
engine. Regenerating the dataset means re-running `setup_agent.py`.

The sandbox being Anthropic's is worth confirming once by hand: ask the agent to
run `whoami; ls ~; cat /proc/1/environ | tr "\0" "\n"` and check that what comes
back is a container, not your laptop.
