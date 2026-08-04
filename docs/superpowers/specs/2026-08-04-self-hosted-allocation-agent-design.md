# Self-hosted XAS Allocation Agent — design

> **Superseded on this branch.** This documents the *self-hosted* build, which
> lives on `claude/agent-spec-managed-i6tn8r`. `olga/cloud-sandbox` replaced it
> with an Anthropic-hosted sandbox after an agent enumerated the host's
> credential files through `bash`. `worker.py`, `make_pull_tool()`, and
> `ALLOC_SANDBOX_ROOT` referenced below no longer exist here. Kept as the record
> of why this branch exists.

Date: 2026-08-04
Branch: `claude/agent-spec-managed-i6tn8r`
Status: approved manifest, pending implementation

## Goal

Run the XAS Allocation Agent on a **self-hosted** sandbox: one long-lived worker
process on our host serves every session's tool calls, the reference solver sits
on that host beside it, and a small web UI drives sessions.

Today the agent is not self-hosted. `setup_allocation_agent.py` creates a
`"type": "cloud"` environment, and `allocation_agent.py` gets the solver into the
sandbox by pasting nine files into a prompt and asking the model to copy them
byte-for-byte. That is the state-leak the core invariant exists to prevent:

> `plan = pure_function(data_snapshot, skill, ledger)`

Self-hosting removes the retyping step. The solver is imported, not reproduced.

## Non-goals

Deliberately excluded — "start simple, adapt later":

- No merge with `olga/self-hosted-sandbox-split`. No shared worker code.
- No broker process. No vault, no credentials, no per-session container.
- No per-session sandbox isolation. One sandbox serves all calls.
- No auth on the web UI. Local play only.
- No real XAS data (DECIDE-7 stands). Synthetic only.

## Shape

Two planes, as before.

**Control plane — run once.** `setup_allocation_agent.py` creates the self-hosted
environment and the agent, uploads the skill, and prints the IDs.

**Run plane — two processes, both long-lived.**

```
  browser ──► web.py ──────────────────────────► Anthropic API
                 │  creates/stops sessions,           │
                 │  streams events back               │ work queue
                 │                                    ▼
                 └──── archives sandbox/ ───►  worker.py  (EnvironmentWorker.run())
                       on session stop            │
                                                  ├─ builtin toolset  → ./sandbox
                                                  └─ pull_allocation_snapshot
                                                        └─ xas_allocation.synth_data
```

`web.py` holds the org API key (control plane). `worker.py` holds only the
environment key. Neither holds data credentials, because there are none.

### Why there is no broker

On the billing agent the broker exists for one reason: data credentials must not
enter the sandbox, because `bash` is a subprocess under the same uid and can read
`/proc/1/environ`. Here the data is a seeded synthetic generator — there is
nothing to protect. `EnvironmentWorker(tools=…)` accepts a factory, so the pull
tool is registered in the same process as the builtin toolset. If a real
credentialed XAS API arrives (DECIDE-7), the broker split comes back with it.

## Files

Verified against `anthropic==0.120.2`.

### Unchanged

| Path | Role |
| --- | --- |
| `xas_allocation/` (7 modules + `overrides_schema.json`) | the allocation script: solver, ledger, spec-match, session loop, synth data |
| `tests/test_invariant.py` | determinism proof (5/5) |
| `skills/xas-allocation/SKILL.md` | the skill |

### New

| Path | Role |
| --- | --- |
| `worker.py` | the self-hosted sandbox |
| `alloc_tools.py` | the `pull_allocation_snapshot` custom tool |
| `web.py` | session control + event stream |
| `static/index.html` | the UI |

### Edited

| Path | Change |
| --- | --- |
| `setup_allocation_agent.py` | `cloud` → `self_hosted`; drop the vault; upload + attach the skill; declare the custom tool; keep the system prompt |
| `.env.example` | `ALLOC_AGENT_ID`, `ALLOC_ENV_ID`, `ANTHROPIC_ENVIRONMENT_KEY`, `ALLOC_SKILL_ID`; drop `ALLOC_VAULT_ID`, `XAS_HOST`, `XAS_DATA_TOKEN` |
| `pyproject.toml` | web server dependency |
| `README.md`, `CLAUDE.md` | run order, invariants |
| `.gitignore` | `sandbox/`, `sessions/` |

### Retired

`allocation_agent.py` — superseded by `web.py` plus a host-side solver. Assumed
approved; recoverable from git if wrong.

Untouched: `setup_agent.py`, `billing_agent.py` (billing agent).

## Component contracts

### `setup_allocation_agent.py` — run once, re-runnable

1. `client.beta.environments.create(config={"type": "self_hosted"})`
2. Upload `skills/xas-allocation/` via `client.beta.skills`; capture `skill_id`.
3. `client.beta.agents.create(model="claude-opus-5", system=SYSTEM_PROMPT,
   skills=[{"type": "custom", "skill_id": …}], tools=[agent_toolset_20260401,
   pull_allocation_snapshot])`
4. Print `ALLOC_AGENT_ID` / `ALLOC_ENV_ID` / `ALLOC_SKILL_ID`.

Re-running with IDs present updates the agent and pushes a new skill version
rather than creating duplicates. **`agents.update()` preserves omitted array
fields**, so `tools` and `skills` are always sent explicitly.

The environment key is generated in the Console, not by this script.

### `alloc_tools.py`

One `@beta_tool` function, `pull_allocation_snapshot`, returning a snapshot from
`xas_allocation.synth_data`. Its name and JSON schema must match the `custom`
tool declared on the agent in `setup_allocation_agent.py` **exactly** — two
declarations of one contract, in two files. A mismatch surfaces as a tool call
that never resolves, which parks the session on `requires_action` and does not
time out.

Signature and payload shape: **open — deferred to the user.**

### `worker.py`

```python
worker = client.beta.environments.work.worker(
    environment_id=ENV_ID,
    environment_key=ENVIRONMENT_KEY,
    workdir=SANDBOX_ROOT,
    tools=lambda env: [*builtin_tools(env), pull_allocation_snapshot],
)
await worker.run()
```

`run()` polls, serves one session to completion, force-stops its work item, and
loops. Sessions are served **sequentially** — a second session waits. That is
the same single-active-session rule the UI enforces, so the two agree by
construction rather than by coordination.

`SIGTERM`/`SIGINT` cancel the in-flight item so its work-item lease is released
rather than left to TTL.

The solver is importable because the worker runs on the host with the repo on
`PYTHONPATH`. No `pip install ortools` at runtime, no file materialization, and
`SOLVER_VERSION` is whatever the repo is checked out at.

### `web.py`

| Route | Behaviour |
| --- | --- |
| `GET /` | serve `static/index.html` |
| `GET /models` | the selectable model list |
| `POST /session` | stop the active session, archive its `sandbox/` into `sessions/<id>/`, create a new one |
| `POST /session/stop` | interrupt the running agent, then stop the session |
| `GET /sessions` | list past sessions (id, title, model, started, stopped) |
| `GET /session/<id>/events` | stream events to the browser |

Model choice is a **per-session override**, not an agent mutation:
`agent={"type": "agent_with_overrides", "id": ALLOC_AGENT_ID, "model": …}`. The
agent resource stays at its configured default.

Because `sandbox/` is flat and shared, "start a session" archiving the previous
one is what makes the session list have contents to point at. Sessions are
serialized anyway, so no two sessions ever contend for the directory.

## Network posture (item 7, honestly)

**"No outside connections" is not enforceable by the platform here.** A
self-hosted environment has no Anthropic-side network policy — the tools run as
our host process, so `bash` inherits the host's network. The worker itself must
reach `api.anthropic.com` to poll, so a blanket block is not available either.

What this design does:

- Registers an **explicit tool list** omitting `web_fetch` / `web_search`, so no
  tool offers egress.
- Attaches no credentials, so nothing authenticates anywhere.

What it does not do: stop `bash` from opening a socket. A real egress jail
(container + proxy allowlist) is the later adaptation. This is written down
rather than assumed away.

## Determinism

Unchanged and load-bearing. What this design changes is only *how the solver
arrives*: imported from the host rather than retyped from a prompt. The ledger
stays the source of truth, replayed per turn; residual spec-compat judgments
stay cached and written back.

## Testing

- `tests/test_invariant.py` keeps passing unmodified — it is host-side and does
  not care about the sandbox.
- New: `alloc_tools`' declared schema matches the agent's `custom` tool
  declaration. This is the one contract split across two files, so it gets the
  one new test.
- Manual: start the worker, start a session from the UI, ask for a repair, stop
  it mid-run, confirm the work item is released and the archive appears.

`uv run ruff format . && uv run ruff check .` before done.

## Open questions

1. **Synthetic-data shape.** What `pull_allocation_snapshot` takes and returns —
   deferred to the user by agreement.
2. **Ledger scope across sessions.** Flat `sandbox/` + archive-on-start is the
   simple choice. Resuming a past session's ledger is not designed yet.
3. **Model list.** Which models `web.py` offers. Note `system.message` is gated
   to Opus 5 / 4.8 / Sonnet 5 / Fable 5 if it is ever used.
