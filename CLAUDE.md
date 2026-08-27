# XAS Agent — working notes

A Claude **Managed Agent** on an **Anthropic-hosted (cloud)** sandbox. Nothing
here runs the agent's tools; `web.py` is the only process. Read `README.md`
first for architecture and run order; this file covers what isn't obvious from
the code.

**One agent, two skills.** Specialisation lives in the skills, not in separate
agent objects: `xas-allocation` drives the deterministic solver,
`xas-reporting` answers reporting questions over the live job-card records. Both are
declared on the single agent behind `ALLOC_AGENT_ID`; `setup_agent.py` sends both
every time. The reporting lane's own agent was never created — it exists only as
a skill.

The self-hosted variant lives on `claude/agent-spec-managed-i6tn8r`. It exists
because a self-hosted sandbox runs tools as your own uid — an agent there
enumerated every credential file on the host through `bash`, which is what
prompted this branch. Don't merge the two; they are alternatives.

Docs: `docs/real-source-investigation.md` is what the live system actually holds
and `docs/appmcp-connect.md` how to call the dev MCP by hand (reporting lane).
`docs/mcp-response-schema.md` and `docs/mcp-field-spec.md` are HISTORICAL as of
2026-08-27 — they were the change request for an app-MCP allocation pull that no
longer exists; the pull reads the export's CSVs instead. The self-hosted plans,
specs and platform notes were retired on 2026-08-23.

## The invariant everything serves

> `plan = pure_function(data_snapshot, skill, override)`

If the mapping, graph, costs, or pins can't be regenerated from those three
inputs, state has leaked into model memory and determinism is gone. Concretely:

- **The pull is the data snapshot.** It comes from a **scenario directory** of
  the real export — `data/scenario-*/orders.csv` + `vehicles.csv`, carved by
  `scenario_engine/real_*.py` — read host-side by `datasource.py` (DECIDE-7).
  `translate()` is the ONE mapping: it filters, counts every drop by reason and
  writes the two payloads `web.py` mounts, `orders.json` and `vehicles.json`.
  `flatten` reads those two files IN THE SANDBOX into the
  `orders/vehicles/allocations` snapshot, one order row = one order for one car.
  The *same* pull backs every turn of a repair cycle — re-applying the same
  override against a different pull is not the same turn, and the scenario is
  chosen once, at session create.
- **The override is the session.** Steering is one combined override object —
  since 2026-08-26 exactly THREE keys, `priority` / `may_move` / `churn_price` —
  which the agent edits in place and carries forward; no ledger, no replay. Same
  snapshot + same override → byte-identical plan; the sandbox is a performance
  convenience. Durable cross-session persistence of that override is deferred
  (DECIDE-5) — the real fix is a host-side store, not the ephemeral sandbox.
- **The config is part of the skill half of the invariant.** Every solver
  parameter lives in `xas_allocation/solver_config.yaml`, read once at import by
  `solver.py` and nowhere else; `decisions.py` holds decisions and no numbers.
  A plan is only reproducible against the config it was priced with, which is why
  `save_plan` stamps `solver_version`. Editing the YAML means re-running
  `setup_agent.py`, exactly like editing the code — and it needs `pyyaml` in the
  sandbox, so the skill's install line is `pip install ortools pyyaml`.
- **Flatten is pure code, not judgment.** Eligibility is a hard `sales_model`
  equality — there is no LLM spec-residual left to cache. If the pull→snapshot
  mapping were re-derived by the model each turn, that is the leak this guards
  against; `flatten.py` keeps it deterministic. And the pull date is a FIXED date
  (`datasource.DEFAULT_NOW`, overridable per scenario), never the clock: static
  files plus `today()` mean the same rows mean something new tomorrow.

`tests/test_invariant.py` proves this holds across a sandbox discard, and
`tests/test_agent_contract.py` pins the two-skill wiring and the mount namespace.
Both run host-side and need no API key — which is why `setup_agent.py` builds its
client lazily rather than at import.

### Trust levels

| Where | Holds | Runs |
| --- | --- | --- |
| `web.py` here | organization API key, `MCP_TOKEN_ENC_KEY`, the dev login (`.env`) | the one custom tool, the host-side fetches that become mounts, and the 20-min bearer rotation |
| Anthropic's vault | the app-MCP bearer only (write-only; never returned) | credential injection at egress |
| Anthropic's sandbox | nothing of ours | bash, file tools, the solver, the MCP tool calls |

The agent's shell is on Anthropic's side, so it has no path to this host's
filesystem, credentials, or network. The app MCP does not change that: the bearer
is stored in the vault and added by Anthropic's proxy *after* the request leaves
the sandbox, so code the agent writes cannot read or exfiltrate it even under
prompt injection. That is the whole reason this branch
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
  first input stops being a snapshot. (The environment's egress is MCP-only, so
  they could reach nothing anyway; this also keeps them out of the agent's context.)
- **The app MCP is the reporting lane's, and the prompt is the only fence.**
  `xas-app-mcp` (added 2026-08-19) gives the agent six read tools against the LIVE
  dev system, and since 2026-08-20 it is the reporting lane's ONLY source — the
  fabricated `jobcards.json` mount is gone. A tool has no path to forbid, so the
  hard rule names the toolset explicitly — "NEVER from an `xas-app-mcp` tool" —
  and the prompt makes the agent say a reporting number came from the live
  system. An allocation claim sourced from live data is worse than one read from a
  file: it changes under you, so the turn is not even reproducible in principle.
  That rule is now the WHOLE fence between the two lanes; nothing structural
  backs it up. `tests/test_agent_contract.py` pins the rule and the two-place
  declaration (`MCP_SERVERS` + the `mcp_toolset` that grants it — a server nothing
  references is a validation error, and so is the reverse).
- **Three places must agree on the MCP URL, and a mismatch is silent.** The agent
  declares it (`MCP_SERVERS`), the vault credential is keyed by it
  (`appmcp_auth.APPMCP_URL`), and the environment must allow the egress
  (`NETWORKING["allow_mcp_servers"]`). Vault matching normalizes host case, default
  ports and a trailing slash but compares the PATH byte-for-byte; no match means
  the call goes out **unauthenticated** and looks like a 401 from the MCP. Without
  `allow_mcp_servers` under `limited` networking, MCP tools fail with no error at
  all. `setup_agent.py` re-sends the networking config every run so the live
  environment cannot drift from the file.
- **The MCP bearer is two nested credentials, and only the outer one is ours.**
  `appmcp_auth.py` mints an AES-256-GCM JWE (7 days) around a `__DMS_app_token`
  the gateway issues at login (**30 minutes**), and rotates it into the vault every
  20 minutes — awaited once at session start, then on a session-owned task
  alongside the tool answerer. `mcp_oauth` auto-refresh cannot do this: its
  `token_endpoint` is a standard OAuth grant, and our outer token has to be
  encrypted here with `MCP_TOKEN_ENC_KEY`. The two expiries fail differently — a
  stale outer token is a flat `401`, a stale inner one is `200` + `isError` +
  "chat session has expired" — and `tools/list` never reaches the gateway, so it
  keeps working under both and proves nothing.
- **`appmcp_auth` reads its config per call, not at import.** `web.py` imports it
  *above* its own `load_dotenv()`, so module-level `os.environ.get` captures
  `None` for all six vars: `configured()` goes False, `vault_ids` is dropped from
  the session without comment, and the agent's first MCP call fails
  `initialize failed: no credential is stored for this server URL` — a message
  that points at the URL while the URL is fine. Fixed 2026-08-20; pinned by
  `test_credential_config_is_read_after_the_environment_loads`, and `web.py` now
  names the missing vars in a warning.
- **`effort` only works on the agent, and the failure is silence.** `setup_agent.model_config()`
  sends `{"id": MODEL, "effort": "medium"}`. An `effort` inside a per-session
  `model` override is IGNORED — not rejected — and `web.py` sends exactly such an
  override for the model picker, so a session always runs at the agent's level.
  Effort drives how many tool calls a turn spends, so this is a cost knob as much
  as a quality one, and no test covers what changing it does to a turn.
- **The session budget is create-only, so it is set on every create or not at
  all.** `web.py`'s `SESSION_BUDGET` caps ONE session's list-priced spend at $4
  (model tokens + web search + $0.08/hour of runtime). At the cap the session goes
  `idle` with `stop_reason: budget_reached`, keeps its container and history, and
  accepts only settle events — a new `user.message` is a 400, which `/message`
  turns into a 409 rather than an opaque 500. Only raising or removing the budget
  resumes it, removal is one-way, and it can never be ADDED to a running session.
  It rides in `extra_body` because anthropic 0.120.2 does not model the field yet.
  There is no step or iteration cap on the platform — this budget and the prompt's
  frugality clause are the only ceilings.
- **The tool answerer is owned by the session, not the browser.** `web.py` starts
  its `tool_runner` task when it creates the session and cancels it on stop. Tie
  it to the event-stream route instead and closing the tab hangs the next pull
  forever.
- **The skill bundles carry code, not data.** `skill_files(skill_dir,
  package)` builds both: `xas-allocation/` + the `xas_allocation` package, and
  `xas-reporting/` (SKILL.md + `phrasebook.py` + `index.md`). The DATA is mounted
  per session by `web.py` — the pull from `datasource.get_source(scenario)` — so
  re-carving a scenario needs no re-deploy. The tenant
  taxonomy is the exception (DECIDE-16): it rides in the `xas-reporting` bundle
  because there is one tenant, `phrasebook.py`
  finds it beside itself, and the price is that the caller can no longer pick a
  dealership per session. **Change either skill, the solver package, or the
  taxonomy and you must re-run `setup_agent.py`.**
- **Two mounts, and reporting has no file at all.** `/workspace/orders.json` and
  `/workspace/vehicles.json` are the pull — the export's two row streams, kept
  apart because folding them into one document would only make `flatten` take it
  apart again — and they are the only things `web.py` mounts. The reporting
  lane had a third mount — a fabricated `jobcards.json` under
  `/workspace/reports/`, whose namespace existed so the prompt could forbid a
  **path** — removed 2026-08-20 because the records were only ever mock data.
  Reporting reads the live system through `xas-app-mcp` instead, so the fence is
  toolset-shaped: every allocation claim comes from the solver, never from an MCP
  tool, never from a file the agent read itself. With both lanes in one sandbox
  that rule is the only thing standing between a planner and a plausible,
  irreproducible answer — `tests/test_agent_contract.py` pins the RULE's
  presence, and nothing pins that the agent follows it. Verify by hand: ask for a
  count over "late orders" and check the answer came from the solver.
- **The disruption is derived, and the allocations may not be a matching.** Nothing
  records "this shipment slipped 21 days" — the carve scripts bake the slip into
  `availableBy` — but `solver.partition` builds the free set from
  `disruption.disrupted_orders`, so BOTH `datasource.translate` (for the tool
  summary) and `flatten` (authoritatively, for the solver) derive it: an allocated
  order whose car now lands past its promise. The two must agree, and
  `tests/test_flatten.py` pins that they do. (An order with no car needs no
  manifest — `partition` already frees anything unassigned.) And a real book is
  not always a matching: a car claimed by two orders yields **no** allocation for
  anyone and the conflict rides in `meta.conflicts` — otherwise the solver's own
  self-check fires on its input. The delay MANIFEST is gone with the fake
  (`delay_days` / `delay_tiers` / `delayed_vehicles`, removed 2026-08-27): the
  summary reports the min/median/max days late instead, which real data can
  actually support.
- **ONE CAR PER LINE, and `Quantity` is not read at all.** One car line is one
  order, keyed `{so_id}-{line}` — two levels. This REPLACED qty expansion on
  2026-08-25 (the expansion, `qty_index`, the per-car report naming and the
  `allocation_qty_not_resolvable_to_cars` counter are all gone). The reason it is
  safe to ignore `Quantity`: a line resolves to at most ONE vehicle code, so a
  second car on it could never be linked to anything. The reason it is not free:
  **a line asking for 3 cars is planned as 1 and the other 2 are not represented
  anywhere, uncounted.** That was an explicit call, pending a response-shape
  decision (one allocation cap per line, or per-car fields) — `docs/mcp-response-schema.md`
  Q1, same VPO hop. Do not reinstate a counter or soften the report wording until
  that lands. An order is NAMED by string in four places (`priority`,
  `may_move.only/.also/.never`, the disruption manifest); all go through
  `solver.names_order` / `disrupted_order_keys`, which match the line or the
  whole VSO. `Snapshot.order_by_key` RAISES on a duplicate key rather than
  collapsing two orders into one.
- **Nothing is walled off, and the free set is the whole protection.** The time
  fence (frozen ≤14d / slushy 15–42d), the soft instruction pin with its
  `not_before`, and the three weight-escalation terms were all REMOVED on
  2026-08-26 (DECIDE-2, -4, -1/-11). The fence is the one that mattered: it fired
  BEFORE the authorisation check in `partition`, so it silently cancelled
  displacements a planner had explicitly authorised — three authorised bumps
  no-oped for exactly that reason. What everyone thought it protected, a settled
  on-time order, is already protected because such an order is never in the free
  set. `fence_of` / `is_locked_in` / `repairability` / `scale_units` /
  `time_scale_of` / `not_before_for` are gone with them; so are `Order.priority`
  and the three history fields. Do not reinstate a smaller version of any of it —
  the register (`decisions.py`) keeps each one RETIRED with what went wrong.
- **`may_move` precedence is part of the contract: never beats only beats also.**
  `only` bounds the WHOLE turn (including anything `also` authorised) and NARROWS
  the default rather than replacing it — the `scope` key it replaced replaced the
  set, so a scope freed settled on-time orders nobody had authorised anyone to
  touch. `also` is the one place permission to displace is granted, and it is
  permission, not an instruction: the solver still declines a bump that buys
  nothing. `never` is absolute, and it is the only way to hold an order that is
  itself late. `tests/test_may_move.py` pins all three.
- **The churn price charges for a CHANGED CAR, not a missed promise.** It used to
  be charged whenever the car's date differed from the promised date — true of
  98.9% of eligible pairings — and only inside the fence's 15–42d band, so the
  trade-off curve came back flat (193.0 weighted late-days at every price from 0
  to 100). It is now added once, in `_solve_one`, to every arc where the vehicle
  differs from the one the order held, beside the break cost. The fabricated book now
  sweeps 25 changes/0 weighted late-days at price 0 → 8 changes/233 at 100, and
  `run_cycle` presents the middle price, 25 (16 changes/63).
- **Priority is a planner LEVER, not a column.** Every order starts at the
  config's `default_priority_step` and only what the override names moves off it;
  named steps (`normal`/`high`/`urgent`) resolve to weights in the config. An
  unknown step RAISES in `_combined_priority` rather than falling back to normal,
  because a silent fallback makes a mistyped instruction look applied. The record's
  priority letter is not read anywhere: `JobPriority` left `REQUIRED_CARD_FIELDS`,
  the pull and `alloc_tools`' customer map with it.
- **The report and the solver speak the same grain: one row per order.** With one
  car per line there is nothing to group, so `planner_report` prints an order key
  (`VSO-4000-1`) per row and `line_sizes` / `car_range` / `line_label` /
  `group_by_line` are gone (2026-08-25) along with `build_change_list`.
  `planner_report` is the only renderer. Reinstating per-car rows means
  reinstating the grouping rule with it: collapsing was only ever allowed when
  every displayed column agreed.
- **The scenarios are the data of record, and they are BUILD OUTPUT.**
  `data/scenario-{unallocated,delayed,mixed}/` each hold `orders.csv` +
  `vehicles.csv` + a `scenario.json` sidecar, carved out of the real export by
  `scenario_engine/real_*.py`. Editing one by hand is editing build output — the
  next carve overwrites it. Change a scenario by re-running the script or its
  knobs, never the CSVs. The sidecar exists because the pull date has no column
  and cannot be the clock: `datasource.scenario_now` reads it, `XAS_PULL_NOW`
  overrides it. The fabricated `scenario_engine/generate.py` world and the two
  `data/mcp-*.json` payloads it authored were deleted on 2026-08-27 — with the
  export as the only source there is nothing for an invented vocabulary to be
  substitutable for, and `data/pull.json` / `data/baseline.json` went with them.
- **In the real export, a car's status IS its allocation state.** `data/vehicles.csv`
  + `data/orders.csv` (3523 cars, 1641 orders) hold a perfect 1:1 matching: every
  `Dealer Order Confirmation` (1380) and `Dealer Reservation` (261) car is claimed
  by exactly one order, every `Available For Sale` car (1302) by none, and there
  are no contested cars. So there is nothing to allocate in it, and three scripts
  manufacture the decision over ONE `carve` (`scenario_engine/real_export.py`):
  `real_unallocated` DELETES allocations (clearing the order's `vehicleCode` +
  `description`, freeing the car), `real_delayed` KEEPS them and slips
  `availableBy` past the order's `etaDealer` instead, and `real_mixed` does both —
  the first two are the mixed one with a count pinned to zero, which is why the
  carve lives in one place. Three things they deliberately leave alone: `inv status label` (the
  physical stage — freeing or delaying a car does not move it, and real available
  cars appear in every stage), the order row itself in the delay scenario, and the
  order's colour, which the export copies from the assigned car (so re-matching on
  colour is circular — 13 of 66 sales models exist in more than one). A freed car's
  status keeps its REAL trailing space, `'Available For Sale '`. Two traps: only an
  INBOUND car can be delayed (1727 of 3523 have already arrived; slipping one
  rewrites history, so candidates are the 694 inbound-and-on-time orders), and the
  export already ships 256 LATE orders, so a subset inherits some and the late
  count must be measured on the output rather than taken from the knob — every run
  reports the split. And `--models` exists because eligibility is exact sales-model
  equality across 66 models: a 60-car subset averages 0.9 cars per model, so most
  orders see nothing but their own car back and NO available percentage fixes it.
- **The two views are not one world.** The pull holds orders and cars carved out
  of a REAL export, frozen; the MCP serves whatever the dev DMS holds right now,
  Service job cards included. They describe overlapping business objects with no
  guarantee they agree, and the live side changes between turns. That is why the
  allocation lane never reads the MCP, and why an allocation number sourced from
  it would not be reproducible even in principle.
- **The pull is host-side, and it is ONE frozen snapshot.**
  `datasource.ScenarioSource` reads the two CSVs and `translate` filters and maps
  them, pure and tested row by row (`tests/test_datasource.py`). The AGENT does
  not read them: `web.py` reads before the session exists and mounts the two
  translated payloads, because a live mid-turn read makes the same override meet
  different rows on turn 3 than on turn 1. The MCP tools the agent holds stay the
  reporting lane's.
- **Two mapping rules that fail silently if reversed.** The PROMISE is the ORDER's
  `etaDealer`; the ARRIVAL is the CAR's `availableBy`. Confuse them and a date is
  compared with itself, so nothing is ever late and nothing downstream notices.
  And eligibility joins the order's `SalesModel` to the vehicle's **`SalesModel`**
  — `modelId.code` holds the model above it (`T71604NXXMH0031` against the order's
  `T71604NCLMH0031`) and matches nothing, which would leave every order with no
  car. There is deliberately NO fallback to it.
- **`'Available For Sale '` carries a real trailing space, and both spellings
  occur in one file** — 152 padded rows and 8 bare ones in the mixed scenario. So
  every status comparison strips first (`datasource._text`); compare unstripped
  and 8 free cars quietly leave the supply pool.
- **A missing column raises, and there is no projection gap any more.** A CSV
  header either has the column or it does not, so `datasource.read_rows` checks
  `REQUIRED_ORDER_COLUMNS` / `REQUIRED_VEHICLE_COLUMNS` at read time and raises
  naming what is absent, with the file still in hand. The app MCP's "absent from
  EVERY row vs absent from SOME rows" distinction — `missing_projection`,
  `meta.projection_gaps` and the planner-facing sentence about it — went with the
  MCP source on 2026-08-27.
- **Everything the filter drops is counted, and the count must be reported.**
  `meta.excluded` carries the funnel by reason and `session.exclusion_note` turns
  it into the first thing the planner reads. A plan over a survivor presented as
  the whole book is the worst thing this pipeline can do. On the three scenarios
  nothing drops but unwanted models (13 of 400 cars in the mixed one), which is
  what makes the OTHER half of that note load-bearing: it also names the orders
  **holding no car** — 50 of 290 mixed, 7 of 7 in the unallocated carve, where
  "no orders are late" on its own would read as "nothing to do".
- **The pull mounts files, not a seed, and not the rows in-band.** The source
  runs here; the agent runs there; everything the *tool* returns crosses into its
  context. So the tool returns only a summary + a `flatten` command; the rows
  travel as the two mounted files (read host-side, out of the sandbox's sight) and
  `flatten` reads them there — nothing dumps ~100KB of JSON into the transcript.
  The scenario scripts' *code* stays out of the sandbox; only the translated
  *output* travels in. The summary carries counts, the scenario name, the drop
  funnel and the min/median/max days late — no rows, and no customer map, because
  there are no customers.
- **`flatten_command` searches from `.`/`/workspace`, never from `/`.** The solver
  lands wherever the platform puts skills, so the command self-locates
  `xas_allocation/flatten.py` — but bounded to the sandbox tree. An unbounded
  `find /` exceeds the 120s bash timeout and kills the agent's shell; that is not
  hypothetical, it happened on the self-hosted build. The two payloads are *not*
  searched for: each is resolved against `mount_candidates` (the path we chose,
  then the `/mnt/session/uploads` prefix the platform was observed to use). The
  command names whichever is missing and exits non-zero — and note the trap that
  cost one debug cycle: `next(gen, sys.exit(...))` evaluates the default EAGERLY,
  so the exit fires before the lookup. Pick, then check.
- **A skill's `name` is immutable per `skill_id`, and `display_title` is unique
  per organization.** Renaming a skill is therefore a NEW skill object, not a new
  version: `versions.create` 400s with "must be consistent across all versions",
  and `skills.create` 400s again if the title is one an existing skill already
  holds. `xas-qa` became `xas-reporting` this way on 2026-08-23 — new
  `REPORTING_SKILL_ID`, new title, and the old object left behind. Clearing
  `REPORTING_SKILL_ID` puts `setup_agent.py` on its create-and-attach path, which
  is the migration. Retiring the old object frees its title, but `skills.delete`
  400s with "Cannot delete skill with existing versions" — every version goes
  first (`versions.delete(version, skill_id=...)`, in that argument order), and
  only then the skill.
- **`agents.update()` preserves omitted array fields.** `setup_agent.py`
  always sends `tools` and `skills` explicitly. Changing `PULL_TOOL` without
  re-running setup does nothing.
- **Cloud and self-hosted resources are not interchangeable.** `check_environment_type()`
  refuses to update a self-hosted environment from this branch. Cross-wire them
  and sessions queue forever for a worker that isn't coming.

## Open decisions

Reviewed 2026-08-25 — they are no longer all stubs. Run
`uv run python -m xas_allocation.decisions` for the live register; its header
line counts what is genuinely undecided. The shape of it:

- **One is OPEN.** DECIDE-5 — durable steering persistence. The override still
  lives only in the conversation; a host-side store in `web.py` (keyed by session
  id, mounted like the pull) is the candidate fix, not a decision. Check the
  Managed Agents persistence surface against current docs before wiring it.
- **Two are settled in SHAPE but carry a number nobody has validated** —
  DECIDE-3 (`break_cost=200`) and DECIDE-15 (`early_weight=0.15`), both in
  `solver_config.yaml`. Never checked against a planner's judgment. The mechanism
  is not up for debate; the value is, and it is reviewed at first real dealer
  data. DECIDE-3's own MECHANISM retired on 2026-08-27 — the hard/soft split read
  a real-vs-future binding the export does not carry — leaving one number.
- **Five are RETIRED** — DECIDE-1 (aging), DECIDE-2 (time fence), DECIDE-4 (pin
  mechanism), DECIDE-11 (reschedule fairness), DECIDE-14 (time scale). Built,
  reviewed and removed on 2026-08-26. They stay in the register with what went
  wrong, because a decision that reads as merely absent invites someone to make
  it again.
- **Two are recorded but deliberately not built.** DECIDE-10 — a `Reserved-*`
  vehicle is out of the pool entirely, so an earmarked car is supply for NO ONE,
  not "eligible for anyone" as the register used to claim; modelling it as
  earmarked supply is the deferred upgrade. DECIDE-6 — there is no liveness
  check and will not be one: the pull happens host-side before the session
  exists, so a session-start call from the agent proves nothing about it.
- **DECIDE-7 is settled and unblocked.** The source is the export's two CSVs, so
  nothing waits on a widened MCP projection any more (the blocker was: no
  `jobitems`, so every dev job card dropped and the live allocation pull came back
  EMPTY). `docs/mcp-field-spec.md` and `docs/mcp-response-schema.md` are the
  historical record of that request, not open work.
- **The rest are decided**, with their trigger named where they have one:
  DECIDE-9 (solver stays in-repo; extraction is triggered by the first NON-DEV
  tenant, not a date) and DECIDE-16 (taxonomy ships in the `xas-reporting`
  skill; a SECOND TENANT flips it back to a host-side mount). DECIDE-13 (no
  uninvited bumps) is the one that most changes what a turn does.

Not in the prototype, per spec: the CP-SAT + LNS escape hatch for *coupled*
orders, and any new hard constraint. **The prompt moves priority and who may
move; a human moves the model and the config** — a new constraint, or a new
number in `solver_config.yaml`, is a reviewed PR with tests, never a live-session
mutation.

## Verifying a change

```bash
uv run python -m datasource --list                  # the scenarios the picker offers
uv run python -m datasource --census                # what the scenario kept vs dropped
uv run python -m datasource --scenario scenario-unallocated --census
uv run python -m xas_allocation.session             # a full four-turn demo over the default scenario
uv run pytest                                       # mapping, flatten, contracts, phrasebook, determinism
PYTHONPATH=. uv run python tests/test_invariant.py  # the invariant, standalone (4/4)
uv run ruff format . && uv run ruff check .
```

Tests need no credentials and no network — the tool and flatten run in-process,
and the three `data/scenario-*/` directories are committed so the suite needs no
carve. Re-carving a scenario needs NO re-deploy (the data is mounted, not
bundled); changing the skill or the solver package does.

The sandbox being Anthropic's is worth confirming once by hand: ask the agent to
run `whoami; ls ~; cat /proc/1/environ | tr "\0" "\n"` and check that what comes
back is a container, not your laptop.
