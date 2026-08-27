# Commands

Every runnable command in this repo, grouped by what you're doing. All need
`uv sync` once first. Nothing here needs an API key **except** running the web app
against a real session or deploying (those two read `.env`).

```bash
uv sync          # install dependencies (run once, and after pulling changes)
```

---

## Pick / inspect the data

The pull is a **scenario directory** of the real export — `orders.csv` +
`vehicles.csv` + a `scenario.json` sidecar carrying the pull date — read
host-side and translated into the two files the sandbox gets. Three are
committed; the next section is how to cut another.

```bash
uv run python -m datasource --list                 # the scenarios the picker offers
uv run python -m datasource --census               # the read/filter funnel for the default one
uv run python -m datasource --scenario scenario-delayed --census
uv run python -m datasource --json | head -40      # the whole translated pull
```

| Variable | Default | What it changes |
| --- | --- | --- |
| `XAS_SCENARIO` | `scenario-mixed` | Which scenario the web form starts on. The picker overrides it per session. |
| `XAS_PULL_NOW` | the scenario's `scenario.json` (`2026-08-25`) | The pull date, for a what-if. Never the clock: static files plus `today()` mean the same rows mean something new tomorrow. |

> Do NOT hand-edit a `data/scenario-*/` CSV to change a scenario — it is build
> output, and the next carve overwrites it. Change the knobs below instead.
> The fabricated `scenario_engine.generate` world (and the `data/mcp-*.json`
> payloads + `data/pull.json` it derived) was deleted on 2026-08-27: the export is
> the only source now.

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

**A book is three classes: no car, a late car, a car that arrives on time.** The
first two are counts (`--empty`, `--late`); the third is a SHARE of the book
(`--on-time-pct`, 20% by default), because it is the control group — what a plan
leaves alone is only readable if some orders needed nothing. That fixes the size
of everything else: 8 disturbed orders at a 20% share is a 10-order book, and the
car subset follows from the book, so neither is a knob any more.

| Flag | Scripts | Default | What it changes |
| --- | --- | --- | --- |
| `--empty` | unallocated, mixed | `8` / `4` | Orders stripped of their car. They keep `OrderId`, model, colour and `etaDealer` — the demand that needs a plan. Their cars are freed, so an emptied order can always at least get its own car back; the interesting part is whether a better one exists. |
| `--late` | delayed, mixed | `8` / `4` | Orders whose car is delayed past its promise. Only `availableBy` moves — the allocation stands and the order row is untouched. |
| `--days-late` | delayed, mixed | `1-20` | How far past the promise the car lands, drawn per order. A span or one number. `1-20` is what the export's own 114 real late orders show, median 8. |
| `--extra-free` | all | `0` / `3` / `1` | Cars freed by deleting an allocation, their ORDERS LEAVING THE BOOK — otherwise every freed car arrives with its own claimant attached and the pool never has slack. |
| `--on-time-pct` | all | `20` | Share of the BOOK that rides in untouched: allocated, and holding a car that lands by the promise. Drawn only from orders that really are on time, so the share is exact and the reported late count is the one you asked for. This sets the book size — `--empty 4 --late 4` at 20% is 10 orders. |
| `--available-pct` | all | `85` / `40` / `50` | Available share of the CAR subset, whose size follows from the book. Cars the scenario frees are the FIRST counted toward it; the rest is padded with cars the export already had available. A mostly-unallocated book forces this high — every emptied order's car is free by construction — and the error names the floor. |
| `--models` | all | `2` (`0` = all 66) | Narrow the whole subset to the N most-demanded sales models. **This, not the percentage, is what gives a small subset any choice** — see below, and a ten-order book needs it. Flag only; it is not prompted. |
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
nothing but their own car back however high the free share is. Measured on
`--empty 20 --extra-free 5 --on-time-pct 60 --available-pct 50` (a 50-order book
in 60 cars, 30 of them free):

| `--models` | eligible free cars per order (min/median/max) | reachable by the promise |
| --- | --- | --- |
| `0` (all 66) | 1 / **2** / 4 | 16 of 20 |
| `12` | 1 / **3** / 6 | 19 of 20 |
| `8` | 1 / **5** / 6 | 19 of 20 |
| `4` | 6 / **8** / 10 | 20 of 20 |

Concentrating makes the subset less representative of the whole book. That is the
trade; at 400+ cars it is usually unnecessary, which is why `--models 0` belongs
on any big carve.

Examples:

```bash
# The three defaults, non-interactively — these ARE the committed 10-order books
uv run python -m scenario_engine.real_unallocated --empty 8 --extra-free 0 --on-time-pct 20 --available-pct 85
uv run python -m scenario_engine.real_delayed --late 8 --days-late 1-20 --extra-free 3 --on-time-pct 20 --available-pct 40
uv run python -m scenario_engine.real_mixed --empty 4 --late 4 --days-late 1-20 --extra-free 1 --on-time-pct 20 --available-pct 50

# A big book: 100 orders needing a car, 233 riding on time (70%), 388 cars
uv run python -m scenario_engine.real_unallocated --empty 100 --extra-free 50 --on-time-pct 70 --available-pct 40 --models 0

# Small and workable: 20 orders needing a car, 60 cars, half of them free
uv run python -m scenario_engine.real_unallocated --empty 20 --extra-free 5 --on-time-pct 60 --available-pct 50 --models 8

# A hard, uniform slip with no spare cars freed for it — most late orders stay stuck
uv run python -m scenario_engine.real_delayed --late 120 --days-late 30 --extra-free 0 --on-time-pct 66 --available-pct 15 --models 0

# The competitive case: unallocated demand and late orders chasing the same pool
uv run python -m scenario_engine.real_mixed --empty 60 --late 60 --days-late 1-20 --extra-free 40 --on-time-pct 50 --available-pct 45 --models 0
```

Each run prints the resulting mix, the days-late spread, and a feasibility line per
disturbance — how many eligible free cars each order has and how many have one that
lands by the promise. Re-roll `--seed` if a draw comes out dull.

> **The late count is measured on the output, and it must equal the knob.** The
> export ships 256 already-late orders, so a random draw used to fold some into
> the untouched remainder and `--late 100` came out at ~124. The on-time draw now
> takes only orders that are on time, and every run re-measures the finished
> files: if one late order got in that the scenario did not delay, the carve
> stops rather than print a share that is not true.

> All three write `data/scenario-<name>/{orders,vehicles}.csv` plus a
> `scenario.json` sidecar with the pull date, and that IS the pull — `datasource`
> reads them directly. Three things none of them touches: `inv status label`
> (the physical stage — freeing or delaying a car does not move it), the order row
> in the delay case, and the order's colour (the export copies it from the assigned
> car, so re-matching on colour is circular).

---

## Run the solver locally (no API key, no sandbox)

Exercises the same code the agent runs, against a scenario directory on your
machine.

```bash
uv run python -m xas_allocation.session      # full per-turn loop: discrepancy map,
                                             # planner report, 3 demo steering turns
uv run python -m xas_allocation.decisions    # every DECIDE-n, its default and its STATUS

# flatten the two MOUNTED payloads (what the agent's pull command runs):
uv run python -m xas_allocation.flatten --orders orders.json --vehicles vehicles.json
```

---

## Poke the app MCP by hand (needs `.env`)

The reporting lane's live source, called directly with no agent in the way. It
mints the bearer through `appmcp_auth`, so `.env` is the only setup, and it trims
the `states` block that rides on every job-card response and says nothing.

```bash
uv run python -m appmcp --list                      # every tool, params summarised
uv run python -m appmcp --list get_job_list         # one tool, full input schema
uv run python -m appmcp get_job_list '{"paging": {"count": 1}}'
uv run python -m appmcp get_job_list '{...}' --raw  # keep the states block too
```

This is the way to CHECK `docs/appmcp-connect.md` rather than trust it — the
surface has been renamed under us twice. Read-only, and it talks to the LIVE dev
system, so it is the one command here that is neither offline nor reproducible.

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
| `XAS_SCENARIO` | `scenario-mixed` | Which scenario directory the web form starts on; the per-session picker overrides it. |
| `XAS_PULL_NOW` | the scenario's sidecar | Override the pull date for a what-if. |
| `APPMCP_URL` | URL | The app MCP to call. Defaults to `appmcp_auth.APPMCP_URL`. |

There is no `XAS_API_TOKEN` and the allocation pull needs no credential at all —
it reads committed CSVs. The six host-side variables (`MCP_TOKEN_ENC_KEY`,
`APPMCP_VAULT_ID`, `APPMCP_CREDENTIAL_ID`, `APPMCP_COMPANY_DB`,
`APPMCP_LOGIN_EMAIL`, `APPMCP_LOGIN_PASSWORD`) are the REPORTING lane's bearer;
`web.py` names any that are missing rather than failing quietly, and none of them
ever reaches the sandbox.

```bash
# Run the web app on a specific order book (the picker overrides this per session)
XAS_SCENARIO=scenario-delayed uv run uvicorn web:app --port 8000
```

---

## Deploy to the Managed Agent (control-plane, needs `.env`)

Creates/updates the cloud environment, uploads the skill (solver + `SKILL.md`),
and creates/updates the agent. Re-runnable — updates in place. Re-run it whenever
you change the **solver package** or **`SKILL.md`** (re-carving a scenario needs
no redeploy — the data is mounted per session, not bundled).

```bash
uv run python setup_agent.py
```

---

## Typical loops

```bash
# Change the data, then see the solver's behaviour locally
uv run python -m scenario_engine.real_delayed --late 120 --days-late 30 --extra-free 0 --on-time-pct 66 --available-pct 15 --models 0
XAS_SCENARIO=scenario-delayed uv run python -m xas_allocation.session

# Cut a fresh scenario out of the real export (no car / late car / both)
uv run python -m scenario_engine.real_mixed --empty 4 --late 4 --days-late 1-20 --extra-free 1 --on-time-pct 20 --available-pct 50

# Change solver/skill code, then verify and redeploy
uv run ruff format . && uv run ruff check . && uv run pytest
uv run python setup_agent.py
```
