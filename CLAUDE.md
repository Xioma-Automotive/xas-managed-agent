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

Docs: `docs/mcp-response-schema.md` is the response shape the pull needs from the
app MCP, `docs/mcp-field-spec.md` the allowlist diff behind it,
`docs/real-source-investigation.md` what the live system actually holds, and
`docs/appmcp-connect.md` how to call the dev MCP by hand. The self-hosted plans,
specs and platform notes were retired on 2026-08-23.

## The invariant everything serves

> `plan = pure_function(data_snapshot, skill, override)`

If the mapping, graph, costs, or pins can't be regenerated from those three
inputs, state has leaked into model memory and determinism is gone. Concretely:

- **The pull is the data snapshot.** It comes from a callable source
  (`datasource.py`, host-side): the `scenario_engine/` fake fabricates
  `data/pull.json` (PO→PDN→Vehicle, Customer→SO with vehicle order rows) by
  default, or the real XAS endpoint by config (DECIDE-7). `web.py` fetches it and
  mounts it into the sandbox as a file; `flatten` explodes SOs into rows + unions
  the supply (vehicles ∪ PO-line slots) into the `orders/units/incumbent` snapshot.
  The *same* pull backs every turn of a repair cycle — re-applying the same
  override against a different pull is not the same turn.
- **The override is the session.** Steering is one combined override object
  (weights / pins / forbid / lambda / scope / bump) the agent edits in place and
  carries forward — no ledger, no replay. Same snapshot + same override →
  byte-identical plan; the sandbox is a performance convenience. Durable
  cross-session persistence of that override is deferred (DECIDE-5) — the real fix
  is a host-side store, not the ephemeral sandbox.
- **Flatten is pure code, not judgment.** Eligibility is a hard `sales_model`
  equality — there is no LLM spec-residual left to cache. If the rich→snapshot
  mapping were re-derived by the model each turn, that is the leak this guards
  against; `flatten.py` keeps it deterministic.

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
  all.** `web.py`'s `SESSION_BUDGET` caps ONE session's list-priced spend at $5
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
- **The skill bundles carry code, and one dataset.** `skill_files(skill_dir,
  package)` builds both: `xas-allocation/` + the `xas_allocation` package, and
  `xas-reporting/` (SKILL.md + `phrasebook.py` + `index.md`). The one SESSION dataset is
  still mounted per session by `web.py` — the pull from
  `datasource.get_source()` — so regenerating it needs no re-deploy. The tenant
  taxonomy is the exception (DECIDE-16): it rides in the `xas-reporting` bundle
  because there is one tenant, `phrasebook.py`
  finds it beside itself, and the price is that the caller can no longer pick a
  dealership per session. **Change either skill, the solver package, or the
  taxonomy and you must re-run `setup_agent.py`.**
- **One mount, and reporting has no file at all.** `/workspace/pull.json` is the
  allocation snapshot, and it is the only thing `web.py` mounts. The reporting
  lane had a second mount — a fabricated `jobcards.json` under
  `/workspace/reports/`, whose namespace existed so the prompt could forbid a
  **path** — removed 2026-08-20 because the records were only ever mock data.
  Reporting reads the live system through `xas-app-mcp` instead, so the fence is
  toolset-shaped: every allocation claim comes from the solver, never from an MCP
  tool, never from a file the agent read itself. With both lanes in one sandbox
  that rule is the only thing standing between a planner and a plausible,
  irreproducible answer — `tests/test_agent_contract.py` pins the RULE's
  presence, and nothing pins that the agent follows it. Verify by hand: ask for a
  count over "late orders" and check the answer came from the solver.
- **The disruption is derived, and the incumbent may be invalid.** XAS records no
  "this shipment slipped 21 days" manifest, but `solver.partition` builds the free
  set from `disruption.disrupted_orders`, so `map_response` derives it: an
  allocated order whose car now lands past its promise. (An order with no car
  needs no help — `partition` already frees anything unassigned.) And the live
  incumbent is not always a matching: vehicle `10831` is allocated to three VSOs
  at once, so a contested vehicle yields **no** incumbent for anyone and the
  conflict rides in `meta.conflicts` — otherwise the solver's own self-check
  fires on its input.
- **The two views are not one world.** `pull.json` holds VSOs fabricated by
  `scenario_engine`; the MCP serves whatever the dev DMS holds right now, Service
  job cards included. They describe overlapping business objects with no
  guarantee they agree, and the live side changes between turns. Do not let a
  quiet fixture stand in for the rule above.
- **The real pull goes through the app MCP, host-side, and it is still ONE frozen
  snapshot.** `datasource.AppMcpSource` calls the MCP's own `get_job_cards` +
  `get_vehicles` — one data seam for both lanes — and `map_response` filters and
  maps the rows, pure and tested against a captured response
  (`tests/fixtures/xas_sample.json`). The AGENT still does not make these calls:
  `web.py` fetches before the session exists and mounts the result as a file,
  because a live mid-turn read makes the same override meet different rows on
  turn 3 than on turn 1. The MCP tools the agent holds stay the reporting lane's.
- **The MCP projects, so a missing field has two very different causes.** Its
  tools return an allowlisted subset of each record, and the fields the solver
  needs are not all on it yet — `docs/mcp-field-spec.md` is the list, and the
  worst of them is a NAME BUG: it asks for `DueDate` where XAS stores
  `DueDateTime`, so 0 of 25 VSOs return a promised date while 13 have one.
  `missing_projection()` tells the two cases apart: a field absent from EVERY row
  is the allowlist omitting it (widen the MCP), a field absent from SOME rows is
  the tenant not having filled it in (data entry). Both produce an identical
  empty funnel and need opposite fixes, so the gap is named in
  `meta.projection_gaps` and reaches the planner as its own sentence.
- **Two mapping rules that fail silently if reversed.** Future-vs-real comes from
  the status **name**, never the code: `02` is `On The Way` on 218 vehicles and
  `'Available For Sale '` (trailing space) on 106 more, so bucketing by code
  merges a car still shipping with a car on the lot. And eligibility joins the
  order's `SalesModelCode` to the vehicle's **`SalesModel`** — `ModelId.Code`
  holds the model above it and matches nothing, which would backorder every
  order.
- **Everything the filter drops is counted, and the count must be reported.**
  `meta.excluded` carries the funnel by reason and `session.exclusion_note` turns
  it into the first thing the planner reads: on dev data 24 of 25 sales orders
  have no model or no promised date, and a plan over the 25th presented as the
  whole book is the worst thing this pipeline can do. The fabricated source
  filters nothing, so the note is empty there.
- **The pull mounts a file, not a seed, and not the rows in-band.** The source
  runs here; the agent runs there; everything the *tool* returns crosses into its
  context. So the tool returns only a summary + a `flatten` command; the rows
  travel as the mounted file (fetched host-side, out of the sandbox's sight) and
  `flatten` reads them there — nothing dumps ~KBs of JSON into the transcript. The
  scenario engine's *code* (and any XAS credential) stays out of the sandbox; only
  the fetched *output* travels in. The summary still carries the customer-name →
  `customer_id` map, because §6 steering needs it to compile "prefer Colmobil" into
  an override.
- **`flatten_command` searches from `.`/`/workspace`, never from `/`.** The solver
  lands wherever the platform puts skills, so the command self-locates
  `xas_allocation/flatten.py` — but bounded to the sandbox tree. (The pull is *not*
  searched for: it's read from the known `MOUNT_PATH` the host mounted it at.) An
  unbounded `find /` exceeds the 120s bash timeout and kills the agent's shell;
  that is not hypothetical, it happened on the self-hosted build.
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

`DECIDE-1..16` are stubbed defaults, not settled answers. Run
`uv run python -m xas_allocation.decisions` for the live list. The big ones for
anyone touching this: DECIDE-14 (`time_scale` knob — the solver reasons at
days/weeks/months, rounding gaps UP; changes the plan, fence stays in days),
DECIDE-15 (earliness is priced — linear + small so lateness dominates; a car
months-early is not a win), DECIDE-7 (the real pull reads the live system through
the app MCP — `datasource.AppMcpSource`, `XAS_DATA_SOURCE=xas`; the
`scenario_engine/` fake stays the offline default. What is still open is the
DATA: 1 of 25 dev VSOs carries both a model and a promised date, and `EtaDealer`
is set on 3 vehicles fleet-wide), DECIDE-3 (which
`location_state` counts as committed), DECIDE-9 (the solver lives in-repo; it
moves to a version-pinned repo before real dealer data), DECIDE-5 (no durable
session persistence assumed — steering is one combined override carried in the
conversation; a host-side store is the deferred real fix), DECIDE-10
(reserved_for_customer eligibility, deferred), DECIDE-16 (the taxonomy ships in
the `xas-reporting` skill instead of being mounted — one tenant only; a second one moves
it back to a host-side mount).

Not in the prototype, per spec: the CP-SAT + LNS escape hatch for *coupled*
orders, and any new hard constraint. **The prompt moves weights and pins; a human
moves the model** — a new constraint is a reviewed PR with tests, never a
live-session mutation.

## Verifying a change

```bash
uv run python -m scenario_engine.generate           # (re)fabricate data/pull.json
uv run python -m datasource --census                # what the configured source kept vs dropped
uv run pytest                                       # engine, flatten, contracts, phrasebook, determinism
PYTHONPATH=. uv run python tests/test_invariant.py  # the invariant, standalone (4/4)
uv run ruff format . && uv run ruff check .
```

Tests need no credentials and no network — the tool and flatten are exercised
in-process, and `data/pull.json` is committed so the suite runs without the
engine. Regenerating the dataset means re-running `setup_agent.py`.

The sandbox being Anthropic's is worth confirming once by hand: ask the agent to
run `whoami; ls ~; cat /proc/1/environ | tr "\0" "\n"` and check that what comes
back is a container, not your laptop.
