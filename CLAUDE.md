# XAS Allocation Agent — working notes

A Claude **Managed Agent** on a **self-hosted** sandbox: `worker.py` on your
machine runs every tool call. Read `README.md` first for architecture and run
order; this file covers what isn't obvious from the code.

Design docs: `docs/superpowers/specs/`, plans in `docs/superpowers/plans/`,
platform notes in `docs/managed-agents-adoption.md`.

## The invariant everything serves

> `plan = pure_function(data_snapshot, skill, ledger)`

If the mapping, graph, costs, or pins can't be regenerated from those three
inputs, state has leaked into model memory and determinism is gone. Concretely:

- **`seed` is the data snapshot.** `pull_allocation_snapshot` is a seeded
  generator, so one seed must be reused for every turn of a repair cycle. A
  replay against a different seed is not a replay.
- **The ledger is the session.** Steering instructions are appended and replayed;
  the sandbox is a performance convenience.
- **Residual spec-compat judgments are cached and written back**, so a replay
  inherits the judgment rather than re-making it.

`tests/test_invariant.py` proves this holds across a sandbox discard. It runs
host-side and needs no API key.

### Trust levels

| Process | Holds | Runs |
| --- | --- | --- |
| `setup_allocation_agent.py`, `web.py` | organization API key (`.env`) | no tool calls |
| `worker.py` | environment key only (`.env.worker`) | every tool call |

The two `.env` files are the boundary, not filing. `worker.py` exits if it finds
an `ANTHROPIC_API_KEY`, because the process that executes agent tool calls must
not hold an organization-scoped credential.

**There is no broker and no vault here, and that is a consequence, not an
omission.** The data is a seeded generator with nothing to protect, so the pull
tool is registered in the worker process alongside the builtin tools. That is
only safe while the data is synthetic: the `bash` tool is a subprocess under the
same uid, so anything in the worker's environment is readable from a shell the
agent controls. **If a credentialed XAS API arrives (DECIDE-7), the credential
does not go here** — it goes in a separate host-side process that answers the
data tool over the session, leaving the tool call unanswered by this worker. Do
not wire a credential into `alloc_tools.py` or the worker's environment.

## Invariants that bite if you change them

- **The tool contract has exactly one definition.** `alloc_tools.py` holds
  `PULL_TOOL` (what the agent declares) *and* `make_pull_tool()` (what the worker
  registers), both built from the same constants. Splitting them is how you get
  an `agent.custom_tool_use` nothing answers — which parks the session on a
  `requires_action` idle that **never times out**, so the failure looks like a
  hang, not an error. `tests/test_tool_contract.py` guards the wiring.
- **`agents.update()` preserves omitted array fields.** `setup_allocation_agent.py`
  always sends `tools` and `skills` explicitly. Changing `PULL_TOOL` without
  re-running setup does nothing.
- **One session at a time is structural.** `EnvironmentWorker.run()` serves a
  session to completion before claiming the next. `web.py` presents that as
  "new session stops the old"; the two agree by construction. Don't add
  concurrency to one side only.
- **The sandbox directory is flat and reused.** `web.py` moves
  `~/xas-alloc-sandbox` into `~/xas-alloc-sessions/<session_id>/` when a session
  ends. Skip that and the next session reads the previous one's ledger.
- **The sandbox lives outside the repo, and that is load-bearing.** `bash` is not
  confined to the workdir; a sandbox at `./sandbox` puts `.env` — the org API
  key — one `cd ..` away. Don't move it back for convenience.
- **The workdir is provisioned per session.** `tools_for()` copies
  `xas_allocation/` and `tests/test_invariant.py` in before the agent's first
  tool call, because the prompt promises the solver is importable there. Copied,
  not symlinked — the file tools reject symlinks resolving outside the workdir.
  When this was missing the agent ran `find /`, blew the bash tool's 120s
  timeout, and killed its own shell. A prompt that promises a layout the worker
  doesn't create is not a documentation bug; it is a hang.
- **The pull returns a summary, not rows.** 120 orders is ~100 KB of JSON;
  returning it would push the dataset through the context window every call. The
  summary carries the customer-name → `customer_id` map because §6 steering needs
  it to compile "prefer Colmobil" into an override.
- **No tool offers egress, but `bash` does.** `beta_agent_toolset_20260401` is
  `bash, read, write, edit, glob, grep` — no fetch, no search — and nothing is
  credentialed. But tools run as the host process, so `bash` inherits the host
  network. This is a local prototype, not an isolation boundary. A container +
  proxy allow-list is the later adaptation.

## Open decisions

`DECIDE-1..9` are stubbed defaults, not settled answers. Run
`uv run python -m xas_allocation.decisions` for the live list. The big ones for
anyone touching this: DECIDE-7 (no real XAS API — synthetic stands in), DECIDE-9
(the solver lives in-repo; it moves to a version-pinned repo before real dealer
data), DECIDE-5 (no platform session persistence assumed — the ledger is a JSON
artifact).

Not in the prototype, per spec: the CP-SAT + LNS escape hatch for *coupled*
orders, and any new hard constraint. **The prompt moves weights and pins; a human
moves the model** — a new constraint is a reviewed PR with tests, never a
live-session mutation.

## Verifying a change

```bash
uv run pytest                                       # tool contract + determinism
PYTHONPATH=. uv run python tests/test_invariant.py  # the invariant, standalone (5/5)
uv run ruff format . && uv run ruff check .
```

Tests need no credentials and no running worker.

Anything touching the credential boundary should also be checked live: ask the
agent to run `env` and confirm no `ANTHROPIC_API_KEY` appears.
