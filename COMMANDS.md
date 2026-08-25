# Commands

Every runnable command in this repo, grouped by what you're doing. All need
`uv sync` once first. Nothing here needs an API key **except** running the web app
against a real session or deploying (those two read `.env`).

```bash
uv sync          # install dependencies (run once, and after pulling changes)
```

---

## Generate / vary the data

The scenario engine fabricates the app MCP's two response payloads
(`data/mcp-jobcards.json`, `data/mcp-vehicles.json`) and then DERIVES
`data/pull.json` from them through `datasource.map_world` — the same mapping the
live pull uses (plus `data/baseline.json`, the pre-disruption "good world").
`data/pull.json` is the file the web app reads and mounts into the agent's sandbox.

```bash
uv run python -m scenario_engine.generate          # regenerate with defaults
```

### Parameters (this is how you "play with the starting conditions")

| Flag | Default | What it changes |
| --- | --- | --- |
| `--seed` | `20` | Random seed. Same seed → byte-identical data. Change it for a different-but-reproducible world. |
| `--customers` | `30` | How many dealers exist. |
| `--orders` | `40` | How many car lines, which is how many wanted cars — one car per line. Spread across VSOs of 1-3 lines each, so this yields fewer VSOs than orders. |
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
> is exactly what the next session sees. But it is a DERIVED file now: the next
> `scenario_engine.generate` overwrites it, and the default source maps the two
> `data/mcp-*.json` payloads rather than reading it. Edit those to make a change
> that survives a regenerate.

---

## Run the solver locally (no API key, no sandbox)

Exercises the same code the agent runs, against `data/pull.json` on your machine.

```bash
uv run python -m xas_allocation.session      # full per-turn loop: discrepancy map,
                                             # planner report, 3 demo steering turns
uv run python -m xas_allocation.flatten      # rich pull -> snapshot (sanity check)
uv run python -m xas_allocation.decisions    # every DECIDE-n, its default and its STATUS
```

---

## Inspect the pull itself

Runs against whichever source `XAS_DATA_SOURCE` selects, so it is also how you
check WHICH source you are on and what it actually returned. Read-only.

```bash
uv run python -m datasource --census   # the funnel: collected -> usable, and why the rest dropped
uv run python -m datasource --json     # the whole rich pull, pretty-printed
```

`--census` is the fastest way to see why a plan covers three orders out of
twenty-five, and the meter to watch while dev records are being filled in: fix
records in the app, re-run, the usable counts should climb.

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
(from `setup_agent.py`).

```bash
uv run uvicorn web:app --port 8000            # then open http://localhost:8000
uv run uvicorn web:app --reload --port 8000   # same, auto-reload on code changes (dev)
```

### Choosing where the pull comes from (`.env` or shell env)

| Variable | Values | Meaning |
| --- | --- | --- |
| `XAS_DATA_SOURCE` | `scenario` (default) / `xas` | `scenario` = the fabricated payloads, mapped offline with no credentials. `xas` = the LIVE dev system, read host-side through the app MCP's own `get_job_cards` + `get_vehicles`. |
| `APPMCP_URL` | URL | The app MCP to call. Defaults to `appmcp_auth.APPMCP_URL`. |

There is no `XAS_API_TOKEN`: the gateway authenticates with a session cookie from
its own login, so the credential IS the login. `XAS_DATA_SOURCE=xas` therefore
needs the same six host-side variables the reporting lane's bearer needs —
`MCP_TOKEN_ENC_KEY`, `APPMCP_VAULT_ID`, `APPMCP_CREDENTIAL_ID`,
`APPMCP_COMPANY_DB`, `APPMCP_LOGIN_EMAIL`, `APPMCP_LOGIN_PASSWORD` — and names
any that are missing rather than failing quietly. None of them ever reach the
sandbox.

**Live currently yields an EMPTY allocation pull.** The MCP's list projection
returns no `jobitems`, and one car line is one order, so all 25 dev VSOs drop for
`no_car_line`. That is a projection gap (`docs/mcp-field-spec.md`), not a bug
here — `datasource --census` names it.

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
uv run python setup_agent.py
```

---

## Typical loops

```bash
# Change the data, then see the solver's behaviour locally
uv run python -m scenario_engine.generate --delay-days 40
uv run python -m xas_allocation.session

# Change solver/skill code, then verify and redeploy
uv run ruff format . && uv run ruff check . && uv run pytest
uv run python setup_agent.py
```
