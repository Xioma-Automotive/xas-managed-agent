# XAS Allocation Agent — working notes

A Claude **Managed Agent** on an **Anthropic-hosted (cloud)** sandbox. Nothing
here runs the agent's tools; `web.py` is the only process. Read `README.md`
first for architecture and run order; this file covers what isn't obvious from
the code.

The self-hosted variant lives on `claude/agent-spec-managed-i6tn8r`. It exists
because a self-hosted sandbox runs tools as your own uid — an agent there
enumerated every credential file on the host through `bash`, which is what
prompted this branch. Don't merge the two; they are alternatives.

Design docs: `docs/superpowers/specs/`, plans in `docs/superpowers/plans/`,
platform notes in `docs/managed-agents-adoption.md`.

## The invariant everything serves

> `plan = pure_function(data_snapshot, skill, ledger)`

If the mapping, graph, costs, or pins can't be regenerated from those three
inputs, state has leaked into model memory and determinism is gone. Concretely:

- **The fabricated dataset is the data snapshot.** `scenario_engine/` (outside
  the agent) fabricates `data/pull.json`; the pull ships it and `flatten` maps it
  to the `orders/units/incumbent` snapshot. The *same* bundled dataset backs every
  turn of a repair cycle — a replay against different data is not a replay.
- **The ledger is the session.** Steering instructions are appended and replayed;
  the sandbox is a performance convenience.
- **Flatten is pure code, not judgment.** Eligibility is a hard `sales_model`
  equality — there is no LLM spec-residual left to cache. If the rich→snapshot
  mapping were re-derived by the model each turn, that is the leak this guards
  against; `flatten.py` keeps it deterministic.

`tests/test_invariant.py` proves this holds across a sandbox discard. It runs
host-side and needs no API key.

### Trust levels

| Where | Holds | Runs |
| --- | --- | --- |
| `web.py` here | organization API key (`.env`) | the one custom tool, nothing else |
| Anthropic's sandbox | nothing of ours | bash, file tools, the solver |

The agent's shell is on Anthropic's side, so it has no path to this host's
filesystem, credentials, or network. That is the whole reason this branch
exists.

**A custom tool is answered by the client wherever the sandbox lives.** That is
the one host-side obligation left: `web.py` runs a `tool_runner` task per session
answering `pull_allocation_snapshot`, and leaves every other tool name for the
cloud sandbox. A credentialed XAS API (DECIDE-7) would live here too — and here
is the right place for it, since the sandbox never sees this process.

## Invariants that bite if you change them

- **The tool contract has exactly one definition.** `alloc_tools.py` holds
  `PULL_TOOL` (what the agent declares) *and* `pull_allocation_snapshot` (what
  `web.py` registers), both built from the same constants. Splitting them is how
  you get an `agent.custom_tool_use` nothing answers — which parks the session on
  a `requires_action` idle that **never times out**, so the failure looks like a
  hang, not an error. `tests/test_tool_contract.py` guards the wiring.
- **The tool answerer is owned by the session, not the browser.** `web.py` starts
  its `tool_runner` task when it creates the session and cancels it on stop. Tie
  it to the event-stream route instead and closing the tab hangs the next pull
  forever.
- **The skill bundle carries the solver AND the dataset.** `skill_files()` uploads
  `xas_allocation/` *and* `data/pull.json` under the skill directory, because there
  is no host workdir to copy them into and having the model retype either from a
  prompt is the exact determinism leak this design exists to prevent. **Change the
  package or regenerate the dataset and you must re-run `setup_allocation_agent.py`**,
  or the sandbox keeps solving with the previous version — with no error to tell you.
- **The pull ships the bundled dataset, not a seed, and not the rows.** The tool
  runs here; the agent runs there; everything returned crosses into its context.
  So the tool returns a summary + a `flatten` command; the rows travel in the
  bundle (like the solver code), and `flatten` reads them there — nothing dumps
  ~KBs of JSON into the transcript. The scenario engine's *code* stays out of the
  sandbox; only its *output* travels in. The summary still carries the
  customer-name → `customer_id` map, because §6 steering needs it to compile
  "prefer Colmobil" into an override.
- **`flatten_command` searches from `.`/`/workspace`, never from `/`.** The solver
  + dataset land wherever the platform puts skills, so the command self-locates
  `xas_allocation/flatten.py` — but bounded to the sandbox tree. An unbounded
  `find /` exceeds the 120s bash timeout and kills the agent's shell; that is not
  hypothetical, it happened on the self-hosted build.
- **`agents.update()` preserves omitted array fields.** `setup_allocation_agent.py`
  always sends `tools` and `skills` explicitly. Changing `PULL_TOOL` without
  re-running setup does nothing.
- **Cloud and self-hosted resources are not interchangeable.** `check_environment_type()`
  refuses to update a self-hosted environment from this branch. Cross-wire them
  and sessions queue forever for a worker that isn't coming.

## Open decisions

`DECIDE-1..11` are stubbed defaults, not settled answers. Run
`uv run python -m xas_allocation.decisions` for the live list. The big ones for
anyone touching this: DECIDE-7 (no real XAS API — `scenario_engine/` fabricates
PDN/Vehicle/SO data shaped per `docs/xasdatamodel.md`), DECIDE-3 (which
`location_state` counts as committed), DECIDE-9 (the solver lives in-repo; it
moves to a version-pinned repo before real dealer data), DECIDE-5 (no platform
session persistence assumed — the ledger is a JSON artifact), DECIDE-10
(reserved_for_customer eligibility, deferred).

Not in the prototype, per spec: the CP-SAT + LNS escape hatch for *coupled*
orders, and any new hard constraint. **The prompt moves weights and pins; a human
moves the model** — a new constraint is a reviewed PR with tests, never a
live-session mutation.

## Verifying a change

```bash
uv run python -m scenario_engine.generate           # (re)fabricate data/pull.json
uv run pytest                                       # engine, flatten, contract, determinism
PYTHONPATH=. uv run python tests/test_invariant.py  # the invariant, standalone (4/4)
uv run ruff format . && uv run ruff check .
```

Tests need no credentials and no network — the tool and flatten are exercised
in-process, and `data/pull.json` is committed so the suite runs without the
engine. Regenerating the dataset means re-running `setup_allocation_agent.py`.

The sandbox being Anthropic's is worth confirming once by hand: ask the agent to
run `whoami; ls ~; cat /proc/1/environ | tr "\0" "\n"` and check that what comes
back is a container, not your laptop.
