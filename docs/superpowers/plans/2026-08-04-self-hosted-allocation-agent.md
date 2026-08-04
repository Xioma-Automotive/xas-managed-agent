# Self-hosted XAS Allocation Agent Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking. Implement task-by-task, running the stated command after each step.

**Goal:** Run the XAS Allocation Agent on a self-hosted sandbox — one long-lived worker process on our host serves every session's tool calls, imports the reference solver directly, and answers a synthetic-data pull tool; a small FastAPI UI drives sessions.

**Architecture:** Two planes. `setup_allocation_agent.py` creates the self-hosted environment + agent + skill once. At run time two long-lived processes: `worker.py` (`EnvironmentWorker.run()`, holds the environment key, serves sessions sequentially) and `web.py` (holds the org API key, creates/stops sessions, streams events to the browser). No broker, no vault, no per-session container — there are no credentials to isolate because the data is a seeded generator.

**Tech Stack:** Python 3.11, `anthropic==0.120.2` (Managed Agents beta), FastAPI + uvicorn + sse-starlette, OR-Tools (already used by `xas_allocation/`), pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-08-04-self-hosted-allocation-agent-design.md`

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `alloc_tools.py` | **new** — the single source of truth for the `pull_allocation_snapshot` contract: name, description, input schema, the agent-side declaration, and the worker-side implementation |
| `worker.py` | **new** — the self-hosted sandbox: poll, serve, loop |
| `web.py` | **new** — session lifecycle + SSE event relay |
| `static/index.html` | **new** — the UI |
| `setup_allocation_agent.py` | **rewrite** — self-hosted env, skill upload, agent with the custom tool; no vault |
| `tests/test_tool_contract.py` | **new** — the declared tool and the registered tool are one object |
| `.env.example`, `pyproject.toml`, `.gitignore` | **edit** — config |
| `README.md`, `CLAUDE.md` | **edit** — run order and invariants |
| `allocation_agent.py` | **delete** — superseded |

Unchanged: `xas_allocation/` (all 8 files), `tests/test_invariant.py`, `skills/xas-allocation/SKILL.md`.

### Key decision locked in here

`alloc_tools.py` holds **both** sides of the tool contract in one module. The spec anticipated two declarations that could drift and a test to catch the drift; defining them once is strictly better. `PULL_TOOL` (what the agent declares) and `make_pull_tool()` (what the worker registers) are both built from `TOOL_NAME` / `TOOL_DESCRIPTION` / `PULL_TOOL_INPUT_SCHEMA`. The test then guards the wiring rather than a copy-paste.

### Snapshot payload shape (assumption — open question #1)

`pull_allocation_snapshot` writes the full snapshot to `snapshot.json` in the workdir and **returns a summary**, not the rows. 120 orders + ~200 units is ~100 KB of JSON; returning it would push the whole dataset through the context window every pull, and the solver reads the file anyway. Marked as an assumption to revisit when the real shape is specified.

---

## Task 1: The tool contract

**Files:**
- Create: `alloc_tools.py`
- Test: `tests/test_tool_contract.py`

- [ ] **Step 1: Write the failing test**

```python
"""The agent's tool declaration and the worker's implementation are one contract."""

import json
from pathlib import Path

import alloc_tools


def test_declaration_matches_implementation(tmp_path):
    tool = alloc_tools.make_pull_tool(tmp_path)
    assert tool.name == alloc_tools.PULL_TOOL["name"]
    assert tool.input_schema == alloc_tools.PULL_TOOL["input_schema"]


def test_declared_as_custom_tool():
    assert alloc_tools.PULL_TOOL["type"] == "custom"
    assert 1 <= len(alloc_tools.PULL_TOOL["description"]) <= 4096


async def test_pull_writes_snapshot_and_returns_summary(tmp_path):
    tool = alloc_tools.make_pull_tool(tmp_path)
    summary = json.loads(
        await tool.call({"seed": 20, "n_orders": 12, "spare_ratio": 0.5, "delay_weeks": 2})
    )

    written = tmp_path / alloc_tools.SNAPSHOT_FILENAME
    assert written.exists()
    snapshot = json.loads(written.read_text())
    assert len(snapshot["orders"]) == 12
    assert summary["snapshot_path"] == alloc_tools.SNAPSHOT_FILENAME
    assert summary["orders"] == 12
    assert summary["disruption"]["delay_weeks"] == 2
    assert summary["disrupted_orders"] == len(snapshot["disruption"]["disrupted_orders"])


async def test_same_seed_same_bytes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    await alloc_tools.make_pull_tool(a).call({"seed": 7, "n_orders": 10})
    await alloc_tools.make_pull_tool(b).call({"seed": 7, "n_orders": 10})
    assert (a / alloc_tools.SNAPSHOT_FILENAME).read_bytes() == (
        b / alloc_tools.SNAPSHOT_FILENAME
    ).read_bytes()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tool_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'alloc_tools'`

- [ ] **Step 3: Write the implementation**

`alloc_tools.py` defines `TOOL_NAME`, `TOOL_DESCRIPTION`, `PULL_TOOL_INPUT_SCHEMA`, `PULL_TOOL` (the `{"type": "custom", ...}` dict the agent declares), `SNAPSHOT_FILENAME = "snapshot.json"`, and:

```python
def make_pull_tool(workdir: str | Path) -> BetaAsyncFunctionTool[Any]:
    @beta_async_tool(
        name=TOOL_NAME, description=TOOL_DESCRIPTION, input_schema=PULL_TOOL_INPUT_SCHEMA
    )
    async def pull_allocation_snapshot(
        seed: int = 42, n_orders: int = 120, spare_ratio: float = 0.6, delay_weeks: int = 3
    ) -> str:
        snapshot = generate_snapshot(
            seed=seed, n_orders=n_orders, spare_ratio=spare_ratio, delay_weeks=delay_weeks
        )
        path = Path(workdir) / SNAPSHOT_FILENAME
        path.write_text(json.dumps(snapshot.as_dict(), indent=2, sort_keys=True))
        return json.dumps({...summary...}, indent=2)

    return pull_allocation_snapshot
```

It must be `beta_async_tool`: `beta_agent_toolset_20260401` returns `BetaAsyncFunctionTool`, and the session runner is async-only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tool_contract.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add alloc_tools.py tests/test_tool_contract.py
git commit -m "Add the pull_allocation_snapshot tool contract"
```

---

## Task 2: The worker

**Files:**
- Create: `worker.py`

- [ ] **Step 1: Write it**

```python
worker = EnvironmentWorker(
    client,
    environment_id=ENV_ID,
    environment_key=ENVIRONMENT_KEY,
    workdir=SANDBOX_ROOT,
    tools=lambda env: [*beta_agent_toolset_20260401(env), alloc_tools.make_pull_tool(env.workdir)],
)
await worker.run()
```

`run()` polls, serves one session to completion, force-stops its work item, loops. Sessions are served **sequentially** — the UI's one-active-session rule and the worker's behaviour agree by construction.

A `check_config()` preflight mirrors the billing poller's: exit if `ENV_ID` or `ANTHROPIC_ENVIRONMENT_KEY` is missing, and exit if `ANTHROPIC_API_KEY` is present — the org key must never reach the process that runs agent tool calls.

`SIGTERM`/`SIGINT` cancel the in-flight item so its work-item lease is released rather than left to TTL.

- [ ] **Step 2: Verify the preflight fires**

Run: `uv run python worker.py`
Expected: exits non-zero naming the missing `.env` values (no environment configured yet).

- [ ] **Step 3: Commit**

```bash
git add worker.py
git commit -m "Add the self-hosted worker"
```

---

## Task 3: Control-plane setup

**Files:**
- Modify: `setup_allocation_agent.py` (full rewrite)

- [ ] **Step 1: Rewrite it**

Three creates, all idempotent on re-run:

```python
environment = client.beta.environments.create(
    name="xas-allocation-env", config={"type": "self_hosted"}
)
skill = client.beta.skills.create(
    files=[("xas-allocation/SKILL.md", SKILL_PATH.read_bytes())],
    display_title="XAS allocation repair",
)
agent = client.beta.agents.create(
    name="XAS Allocation Agent",
    model="claude-opus-5",
    system=SYSTEM_PROMPT,
    tools=[{"type": "agent_toolset_20260401"}, alloc_tools.PULL_TOOL],
    skills=[{"type": "custom", "skill_id": skill.id}],
)
```

Skill files must share one top-level directory with `SKILL.md` at its root — hence the `xas-allocation/` prefix on the upload tuple.

Re-running with IDs already in `.env` calls `agents.update()` and `skills.versions.create()` instead of creating duplicates. **`agents.update()` preserves omitted array fields**, so `tools` and `skills` are always sent explicitly.

The vault is gone entirely: no credentials, so nothing to store. `SYSTEM_PROMPT` keeps its hard rules, with the data-pull paragraph rewritten to point at the tool instead of at in-sandbox synthetic generation.

- [ ] **Step 2: Verify it refuses without a key**

Run: `uv run python setup_allocation_agent.py`
Expected: exits naming `ANTHROPIC_API_KEY` (or creates the resources, if a key is present).

- [ ] **Step 3: Commit**

```bash
git add setup_allocation_agent.py
git commit -m "Self-host the allocation environment; drop the vault"
```

---

## Task 4: The web server

**Files:**
- Create: `web.py`

- [ ] **Step 1: Write the routes**

| Route | Behaviour |
| --- | --- |
| `GET /` | serve `static/index.html` |
| `GET /models` | the selectable model list |
| `GET /sessions` | `sessions.list(agent_id=…)` — id, title, model, status, created_at |
| `POST /session` | stop + archive the active session, archive `sandbox/` into `sessions/<old_id>/`, create a new session with the chosen model |
| `POST /session/stop` | `user.interrupt`, then `sessions.archive()` |
| `POST /session/interrupt` | `user.interrupt` only — stop the agent, keep the session |
| `POST /message` | send a `user.message` to the active session |
| `GET /session/{id}/events` | SSE relay of the session event stream |

Model choice is a per-session override, never an agent mutation:

```python
agent = {"type": "agent_with_overrides", "id": ALLOC_AGENT_ID, "model": model}
```

The active session id lives in one module-level slot. `sessions.list()` is the session list — no local database.

`/message` is not in the spec's route table; that is an oversight in the spec, since a chat UI cannot work without it.

- [ ] **Step 2: Verify it boots**

Run: `uv run uvicorn web:app --port 8000` and `curl -s localhost:8000/models`
Expected: JSON list of models.

- [ ] **Step 3: Commit**

```bash
git add web.py
git commit -m "Add the session web server"
```

---

## Task 5: The UI

**Files:**
- Create: `static/index.html`

- [ ] **Step 1: Write it** — one self-contained page: model `<select>`, "New session" (stops the old), "Stop agent", session list in a sidebar, transcript pane fed by `EventSource`. No build step, no CDN.

- [ ] **Step 2: Verify** — load `localhost:8000`, confirm the model list populates and the session list renders.

- [ ] **Step 3: Commit**

```bash
git add static/index.html
git commit -m "Add the allocation chat UI"
```

---

## Task 6: Config

**Files:**
- Modify: `.env.example`, `pyproject.toml`, `.gitignore`

- [ ] **Step 1: `.env.example`** — allocation block becomes `ALLOC_AGENT_ID`, `ALLOC_ENV_ID`, `ALLOC_SKILL_ID`, `ANTHROPIC_ENVIRONMENT_KEY`, `SANDBOX_ROOT`, `SESSIONS_ROOT`. Drop `ALLOC_VAULT_ID`, `XAS_HOST`, `XAS_DATA_TOKEN`.

- [ ] **Step 2: `pyproject.toml`** — add `fastapi`, `uvicorn[standard]`, `sse-starlette`, and a `[dependency-groups] dev = ["pytest", "pytest-asyncio", "ruff"]`.

- [ ] **Step 3: `.gitignore`** — add `sandbox/`, `sessions/`, `snapshot.json`.

- [ ] **Step 4: Verify**

Run: `uv sync && uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add .env.example pyproject.toml .gitignore uv.lock
git commit -m "Wire up config for the self-hosted allocation agent"
```

---

## Task 7: Docs, and retire the old driver

**Files:**
- Modify: `README.md`, `CLAUDE.md`
- Delete: `allocation_agent.py`

- [ ] **Step 1: Delete `allocation_agent.py`.** Its job was pasting nine files into a prompt for the model to retype; the worker imports the solver directly.

- [ ] **Step 2: `README.md`** — replace the allocation agent's "Run it as a Managed Agent" section with the three-process run order (setup once, then `worker.py` + `web.py`), and correct the claim that the sandbox is self-hosted so it is finally true of the code.

- [ ] **Step 3: `CLAUDE.md`** — add the allocation agent's invariants: one worker serves sessions sequentially; the tool contract has one definition in `alloc_tools.py`; `agents.update()` preserves omitted arrays; no egress tool exists but `bash` inherits the host network.

- [ ] **Step 4: Verify**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check .`
Expected: tests pass, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Document the self-hosted allocation agent; retire the prompt-materialization driver"
```

---

## Open questions carried from the spec

1. **Synthetic-data shape** — implemented as file-plus-summary (see above). Revisit when specified.
2. **Ledger scope across sessions** — flat `sandbox/`, archived on new-session. Resuming a past ledger is not designed.
3. **Model list** — `web.py` ships Opus 5 / Sonnet 5 / Haiku 4.5; adjust to taste.
