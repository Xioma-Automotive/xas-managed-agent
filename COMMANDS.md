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

## Cut a scenario from the REAL export

`data/vehicles.csv` + `data/orders.csv` are a real XAS export — 3523 cars, 1641
orders. There is nothing to allocate in it as it stands: every order already holds
exactly one car, and a car's `status.name` *is* its allocation state (every
`Dealer Order Confirmation` and `Dealer Reservation` car is claimed by one order,
every `Available For Sale` car by none). Three scripts manufacture a decision out
of it, all running the same `carve` in `scenario_engine/real_export.py` and all
emitting the export's own CSV shape:

```bash
uv run python -m scenario_engine.real_unallocated   # orders that lost their car
uv run python -m scenario_engine.real_delayed       # orders whose car turned late
uv run python -m scenario_engine.real_mixed         # both at once
```

Each asks for its knobs, or takes them as flags. The first two are the mixed one
with a count pinned to zero — `real_mixed --late 0` produces a byte-identical file
to `real_unallocated`.

| Flag | Scripts | Default | What it changes |
| --- | --- | --- | --- |
| `--empty` | unallocated, mixed | `100` / `50` | Orders stripped of their car. They keep `OrderId`, model, colour and `etaDealer` — the demand that needs a plan. Their cars are freed, so an emptied order can always at least get its own car back; the interesting part is whether a better one exists. |
| `--late` | delayed, mixed | `100` / `50` | Orders whose car is delayed past its promise. Only `availableBy` moves — the allocation stands and the order row is untouched. |
| `--days-late` | delayed, mixed | `1-20` | How far past the promise the car lands, drawn per order. A span or one number. `1-20` is what the export's own 114 real late orders show, median 8. |
| `--extra-free` | all | `50` | Cars freed by deleting an allocation, their ORDERS LEAVING THE BOOK — otherwise every freed car arrives with its own claimant attached and the pool never has slack. |
| `--subset` | all | `400` | How many cars the scenario holds in total. |
| `--available-pct` | all | `40` | Available share of that subset. Cars the scenario frees are the FIRST counted toward it; the rest is padded with cars the export already had available, and the remainder stays allocated with its order intact. |
| `--models` | all | `0` (all 66) | Narrow the whole subset to the N most-demanded sales models. **This, not the percentage, is what gives a small subset any choice** — see below. Flag only; it is not prompted. |
| `--seed` | all | `1` | Same knobs + same seed → byte-identical output. |
| `--out` | all | `data/scenario-{unallocated,delayed,mixed}` | Output directory. |
| `--orders-in` / `--vehicles-in` | all | `data/orders.csv`, `data/vehicles.csv` | Source export. Refused if any order lacks `etaDealer` — an order with no promise can be neither late nor met. |

**Only an inbound car can be delayed.** A car whose `availableBy` has passed is
already on the dealer's hands (1727 of 3523), so slipping it would rewrite history.
Candidates are the **694** allocated orders whose car is both inbound and currently
on time; `real_delayed` and `real_mixed` print that count before asking.

**Choice per order comes from `--models`, not from `--available-pct`.** Eligibility
is exact sales-model equality, and the export spans 66 models. A 400-car subset
averages 6 cars per model; a 60-car subset averages **0.9**, so most orders see
nothing but their own car back however high the free share is. Measured on 20
unallocated orders in 60 cars at 50% available:

| `--models` | eligible free cars per order (min/median/max) |
| --- | --- |
| `0` (all 66) | 1 / **1** / 2 |
| `12` | 2 / **3** / 6 |
| `8` | 1 / **7** / 8 |
| `4` | 4 / **8** / 10 |

Concentrating makes the subset less representative of the whole book. That is the
trade; at 400+ cars it is usually unnecessary.

Examples:

```bash
# The three defaults, non-interactively
uv run python -m scenario_engine.real_unallocated --empty 100 --extra-free 50 --subset 400 --available-pct 40
uv run python -m scenario_engine.real_delayed --late 100 --days-late 1-20 --extra-free 50 --subset 400 --available-pct 40
uv run python -m scenario_engine.real_mixed --empty 50 --late 50 --days-late 1-20 --extra-free 50 --subset 400 --available-pct 40

# Small and workable: 20 orders needing a car, 60 cars, half of them free
uv run python -m scenario_engine.real_unallocated --empty 20 --extra-free 5 --subset 60 --available-pct 50 --models 8

# A hard, uniform slip with no spare cars freed for it — most late orders stay stuck
uv run python -m scenario_engine.real_delayed --late 120 --days-late 30 --extra-free 0 --subset 400 --available-pct 15

# The competitive case: unallocated demand and late orders chasing the same pool
uv run python -m scenario_engine.real_mixed --empty 60 --late 60 --days-late 1-20 --extra-free 40 --subset 500 --available-pct 45
```

Each run prints the resulting mix, the days-late spread, and a feasibility line per
disturbance — how many eligible free cars each order has and how many have one that
lands by the promise. Re-roll `--seed` if a draw comes out dull.

> **The late count is measured on the output, not on the knob.** The export ships
> 256 already-late orders, so any subset inherits some — `--late 100` typically
> yields ~124, and every run breaks out "delayed here / already late in the export
> / on time", including the scenarios that delay nothing.

> All three emit CSVs, not the app MCP's response payloads, so they do **not** reach
> `data/pull.json` and the solver cannot read them yet — a CSV → MCP-shape
> translation does not exist. Three things none of them touches: `inv status label`
> (the physical stage — freeing or delaying a car does not move it), the order row
> in the delay case, and the order's colour (the export copies it from the assigned
> car, so re-matching on colour is circular).

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

# Cut a fresh scenario out of the real export (no car / late car / both)
uv run python -m scenario_engine.real_mixed --empty 50 --late 50 --days-late 1-20 --extra-free 50 --subset 400 --available-pct 40

# Change solver/skill code, then verify and redeploy
uv run ruff format . && uv run ruff check . && uv run pytest
uv run python setup_agent.py
```
