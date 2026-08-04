# Xioma Automotive — Managed Agents

This repo hosts Claude **Managed Agents** for Xioma Automotive. Each agent uses
the Managed Agents REST surface via the Python `anthropic` SDK, model
`claude-opus-5`. The agent loop runs against a **self-hosted** per-session
sandbox where the agent's tools (bash, file ops, web fetch, etc.) execute; your
code just drives the session and collects the results.

| Agent | Control-plane setup | Data-plane driver | What it does |
| ----- | ------------------- | ----------------- | ------------ |
| **XAS Allocation Agent** | `setup_allocation_agent.py` | `allocation_agent.py` | Repairs a vehicle-to-order allocation after a disruption via a deterministic OR-Tools min-cost-flow solver. Prototype on synthetic data. |
| **Billing Dashboard Agent** | `setup_agent.py` | `billing_agent.py` | Reads billing data from an internal HTTP API and builds self-contained HTML dashboards. |

Every agent splits into two planes: **control** (create the agent, environment,
and vault once — persistent, versioned resources referenced by ID forever after)
and **data** (open a fresh session per conversation and drive it). Never call
`agents/environments/vaults create()` in the per-conversation path.

---

# XAS Allocation Agent (prototype)

A Managed Agent that helps a planner **repair** a vehicle-to-order allocation
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

## Run it as a Managed Agent

```bash
uv sync
cp .env.example .env            # fill in ANTHROPIC_API_KEY (XAS_HOST stays blank in the prototype)
uv run python setup_allocation_agent.py
#   paste the printed ALLOC_AGENT_ID / ALLOC_ENV_ID / ALLOC_VAULT_ID into .env
uv run python allocation_agent.py
```

`allocation_agent.py` opens a session, materializes the **exact** reference
solver into the sandbox and runs the invariant test as a smoke test (determinism
is the reference solver's, never the model re-deriving it), then drives one
steering turn and downloads the ledger + proposed plan to `./out/`. It never
writes back to XAS — the plan is a proposal the planner approves.

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

---

# Billing Dashboard Agent

A conversational Claude **Managed Agent** that reads billing data from an
internal HTTP API, analyzes it (trends, anomalies, top movers), and produces
self-contained HTML dashboards. You chat with it to describe the report you want.

It uses the Managed Agents REST surface via the Python `anthropic` SDK, model
`claude-opus-5`. The agent loop runs against a **self-hosted** per-session
sandbox where the agent's tools (bash, file ops, web fetch, etc.) execute; your
code just drives the session and collects the results.

## Setup-once vs. runtime-per-conversation

Managed Agents split cleanly into two planes, and so does this repo:

- **Control plane — run once.** The agent, environment, and vault are
  persistent, versioned resources. You create them a single time with
  `setup_agent.py`, then reference them by ID forever after. Re-creating them on
  every run accumulates orphaned resources and pays create latency for nothing.
- **Data plane — run every conversation.** `billing_agent.py` opens a fresh
  session against the pre-created agent, streams the conversation, and downloads
  outputs. It never calls `agents.create()` / `environments.create()` /
  `vaults.create()` — it only references the IDs from `.env`.

## Files

| File               | Plane        | What it does |
| ------------------ | ------------ | ------------ |
| `.gitignore`       | —            | Ignores `.env`, generated `*.html`, and Python cruft. |
| `.env.example`     | —            | Committed template for the values below. Copy to `.env`. |
| `pyproject.toml`   | —            | Project + dependencies (`anthropic`, `python-dotenv`). Managed by `uv`. |
| `uv.lock`          | —            | Locked dependency versions (committed for reproducible installs). |
| `requirements.txt` | —            | Same deps, for a plain `pip` fallback. |
| `setup_agent.py`   | Control (once) | Creates a **cloud** environment (limited networking, scoped to the billing host), the **agent** (`claude-opus-5`, prebuilt toolset, billing-analyst system prompt), and a **vault**. Attaches the billing token as an `environment_variable` credential if it's set — otherwise skips it so you can add it later. Prints `AGENT_ID` / `ENV_ID` / `VAULT_ID`. |
| `billing_agent.py` | Data (per run) | Loads the three IDs, opens a session, runs a smoke-test turn then your real request, and downloads the HTML dashboards the agent produced. |
| `README.md`        | —            | This file. |

## Run order

```bash
uv sync
cp .env.example .env
#   fill in ANTHROPIC_API_KEY and BILLING_HOST
#   (BILLING_DATA_TOKEN is optional now — you can add it later)
uv run python setup_agent.py
#   paste the printed AGENT_ID / ENV_ID / VAULT_ID into .env
uv run python billing_agent.py
```

> Using plain `pip` instead of `uv`? `pip install -r requirements.txt`, then
> drop the `uv run` prefix from the commands above.

### Don't have the billing token yet?

You can stand everything up without it. Leave `BILLING_DATA_TOKEN` blank and run
`setup_agent.py` — it creates the environment, agent, and (empty) vault, and
skips the credential. The agent exists and Claude works; it just can't reach the
billing API yet, so the smoke-test turn in `billing_agent.py` will report an auth
failure (expected).

When you get the token from Xioma, set `BILLING_DATA_TOKEN` in `.env` and **re-run
`uv run python setup_agent.py`** — it detects the existing IDs and attaches the
credential to the existing vault, without re-creating anything.

`billing_agent.py` prints a Console trace URL for each session so you can watch
tool calls and messages stream live. Dashboards the agent writes are downloaded
into the working directory as `*.html` (gitignored).

## How auth works

The billing token (`BILLING_DATA_TOKEN`) is stored in an Anthropic-managed
**vault** as an `environment_variable` credential, scoped to the billing host. The sandbox only
ever sees an opaque placeholder; the real token is substituted into the outbound
`Authorization` header **at egress**, so agent code can't read or exfiltrate it.
The smoke-test turn exists because credential/network failures surface on first
use, not when the session is created — it does one authenticated GET and reports
the status before any real work.

## Things to confirm

- **`BILLING_HOST` in `.env.example` is a placeholder** (`api.xiomautomotive.com`).
  Set it to the real internal billing API host before running setup — it scopes
  both the environment's `allowed_hosts` and the credential's egress allow-list,
  so a wrong value means the agent can't reach the API.
- **The token is injected as `Authorization: Bearer <token>`** (header injection).
  If the real billing API expects a different auth scheme — a custom header like
  `X-API-Key`, a query param, or a non-Bearer scheme — the header-injected Bearer
  token won't authenticate. Adjust the credential's `injection_location` and the
  system prompt's auth guidance accordingly (note: secrets can only be injected
  into request headers or bodies, never the URL path).
