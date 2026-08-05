# Commands

Every runnable command in this repo, grouped by what you're doing. All need
`uv sync` once first. Nothing here needs an API key **except** running the web app
against a real session or deploying (those two read `.env`).

```bash
uv sync          # install dependencies (run once, and after pulling changes)
```

---

## Generate / vary the data

The pull is fabricated by the scenario engine and written to `data/pull.json`
(plus `data/baseline.json`, the pre-disruption "good world"). This is the file
the web app reads and mounts into the agent's sandbox.

```bash
uv run python -m scenario_engine.generate          # regenerate with defaults
```

### Parameters (this is how you "play with the starting conditions")

| Flag | Default | What it changes |
| --- | --- | --- |
| `--seed` | `20` | Random seed. Same seed → byte-identical data. Change it for a different-but-reproducible world. |
| `--customers` | `30` | How many dealers exist. |
| `--orders` | `40` | How many vehicle order rows (the demand) — the allocatable units. |
| `--spare-ratio` | `0.4` | Extra supply beyond demand. Higher = more spare cars the solver can shuffle to; lower = tighter, more orders stay stuck. |
| `--delay-days` | `21` | How many days the disrupted PO slips. Bigger = a worse disruption, more orders pushed late. |
| `--out` | `data/` | Output directory. |

Examples:

```bash
# A bigger book with a milder disruption and more spare cars to play with
uv run python -m scenario_engine.generate --customers 60 --orders 120 --spare-ratio 0.6 --delay-days 10

# A harsh disruption on a tight supply — more orders end up stuck
uv run python -m scenario_engine.generate --orders 80 --spare-ratio 0.2 --delay-days 45

# A different reproducible scenario
uv run python -m scenario_engine.generate --seed 7
```

> You can also just hand-edit `data/pull.json` directly — whatever is in that file
> is exactly what the next session sees.

---

## Run the solver locally (no API key, no sandbox)

Exercises the same code the agent runs, against `data/pull.json` on your machine.

```bash
uv run python -m xas_allocation.session      # full per-turn loop: discrepancy map,
                                             # planner report, 3 demo steering turns
uv run python -m xas_allocation.flatten      # rich pull -> snapshot (sanity check)
uv run python -m xas_allocation.decisions    # print every open DECIDE-n + its default
```

---

## Tests & checks (the gate)

```bash
uv run pytest                                        # full suite (engine, flatten,
                                                     # solver, contract, report, datasource)
PYTHONPATH=. uv run python tests/test_invariant.py   # determinism proof, standalone (4/4)
uv run ruff format .                                 # format
uv run ruff check .                                  # lint
```

All of the above run offline — no network, no credentials.

---

## Web app (the planner UI + host-side pull)

The single process. It fetches the pull host-side, mounts it into the sandbox,
answers the custom tool, and serves the chat UI. Needs `.env` with the agent IDs
(from `setup_allocation_agent.py`).

```bash
uv run uvicorn web:app --port 8000            # then open http://localhost:8000
uv run uvicorn web:app --reload --port 8000   # same, auto-reload on code changes (dev)
```

### Choosing where the pull comes from (`.env` or shell env)

| Variable | Values | Meaning |
| --- | --- | --- |
| `XAS_DATA_SOURCE` | `scenario` (default) / `xas` | `scenario` = the fabricated `data/pull.json` (offline, no credentials). `xas` = the real endpoint (stubbed until XAS exists). |
| `XAS_API_BASE` | URL | Real XAS base URL — only used when `XAS_DATA_SOURCE=xas`. |
| `XAS_API_TOKEN` | token | Host-side credential for XAS — never shipped to the sandbox. |

```bash
# Run the web app against the fabricated data (the normal local mode)
XAS_DATA_SOURCE=scenario uv run uvicorn web:app --port 8000
```

---

## Deploy to the Managed Agent (control-plane, needs `.env`)

Creates/updates the cloud environment, uploads the skill (solver + `SKILL.md`),
and creates/updates the agent. Re-runnable — updates in place. Re-run it whenever
you change the **solver package** or **`SKILL.md`** (regenerating `data/pull.json`
no longer needs a redeploy — it's fetched live).

```bash
uv run python setup_allocation_agent.py
```

---

## Typical loops

```bash
# Change the data, then see the solver's behaviour locally
uv run python -m scenario_engine.generate --delay-days 40
uv run python -m xas_allocation.session

# Change solver/skill code, then verify and redeploy
uv run ruff format . && uv run ruff check . && uv run pytest
uv run python setup_allocation_agent.py
```
