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

Docs: `docs/appmcp-connect.md` is how to call the dev MCP by hand (reporting
lane), `docs/evals/routing.md` the hand-run check of which skill fires, and
`docs/appmcp-requests.md` the three things the reporting lane needs from
`xas-app-mcp` that no rule here can fix — the last is an open request against
another repo and goes away when it is answered. That is all three, and each
describes the code as it stands — `docs/` is not an archive. Deleted 2026-08-30: the app-MCP allocation-pull change request
(`mcp-field-spec.md`, `mcp-response-schema.md`), the three implementation plans,
the sandbox file-probe note, and `real-source-investigation.md` — the last was
the 2026-08-20 study of reading the pull through the app MCP, and that whole path
is gone. Git history keeps every one of them. The self-hosted plans, specs and
platform notes were retired on 2026-08-23.

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
- **The app MCP is the reporting lane's, and NOTHING fences it any more.**
  `xas-app-mcp` (added 2026-08-19) gives the agent six read tools against the LIVE
  dev system, and since 2026-08-20 it is the reporting lane's ONLY source — the
  fabricated `jobcards.json` mount is gone. A tool has no path to forbid, so the
  fence was a prompt sentence naming the toolset outright — "NEVER from an
  `xas-app-mcp` tool" — and the prompt used to make the agent SAY a reporting
  number came from the live system, dropped 2026-09-01 as noise in the reply — so
  nothing on screen marks a number as live any more. An allocation claim sourced
  from live data is worse than one read from a file: it changes under you, so the
  turn is not even reproducible in principle. **That sentence was cut from the
  prompt on 2026-09-01 in the size pass, with the two tests that pinned it, and
  the string `xas-app-mcp` appears nowhere in `skills/xas-allocation/SKILL.md` —
  so the rule now exists in no file at all.** Both lanes still share one sandbox.
  Restoring it is one line in `SYSTEM_PROMPT` or a section in the allocation
  skill; until then, verify by hand. What is still pinned is only the two-place
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
  sends `{"id": MODEL, "effort": EFFORT}` — `claude-opus-4-8` back at **`low`** on
  2026-09-03, after a few minutes at `medium` the same day. It is sent
  EXPLICITLY rather than omitted, because `agents.update()` preserves an omitted
  `effort` only while the model id is unchanged. An `effort` inside a per-session
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
  `xas-reporting/` (SKILL.md + `charts.md` + `resolve.py` + `dates.py` +
  `phrasebook.tsv`). `charts.md` is the one file the agent reads ON DEMAND
  (2026-09-01): charts fire on a minority of reporting turns, so the recipe is not
  paid for on the first turn of every session — SKILL.md and the prompt both name
  the path, because a rule you have to fetch is a rule you can skip.
  The DATA is mounted per session by `web.py` — the pull from
  `datasource.get_source(scenario)` — so re-carving a scenario needs no
  re-deploy. The tenant taxonomy is the exception (DECIDE-16): it rides in the
  `xas-reporting` bundle because there is one tenant, and the price is that the
  caller can no longer pick a dealership per session. It ships **BUILT**
  (2026-08-30): `reporting_bundle()` renders `index.md` through
  `phrasebook.render` into `phrasebook.tsv` and ships THAT, while `index.md`
  stays in the repo as the source and never reaches the sandbox. Deriving it
  there cost a model turn every session to rebuild a file that is byte-identical
  every run and that the agent cannot change — and put a second copy of the
  taxonomy in front of a model told not to read it whole. **Change either skill,
  the solver package, or the taxonomy and you must re-run `setup_agent.py`.**
- **The sandbox already has matplotlib, and the chart recipe must not install
  it.** Probed live 2026-09-01 on the cloud environment: `matplotlib`, `numpy`,
  `pandas` and `PIL` are importable, `plotly` is NOT, Python is 3.11.15. So the
  reporting skill's chart recipe needs no pip line — the allocation lane's
  `pip install ortools pyyaml` is for the two the image lacks, not a general
  habit — and a chart written against plotly would fail in the sandbox.
- **The reporting SKILL.md is a procedure, not a changelog.** Condensed on
  2026-09-01 (534 -> ~460 lines): every rule kept, the incident narratives behind
  them removed. The agent re-reads this file on the first reporting turn of every
  session, so a paragraph explaining WHY a rule exists is paid for on every
  session and answers a question the agent never asks. The why lives in the
  docstrings of `tests/test_agent_contract.py` and in this file. Those tests pin
  the rules by exact phrase: re-wrap a pinned sentence and the test fails, which
  is the point — use `_flat` there for anything that is prose rather than a
  literal command.
  **Trimmed again 2026-09-02, 21,228 -> 20,054 characters**, after a live run
  measured that read at 23,038 characters of tool result on the first reporting
  turn. Five cuts, and the argument is the count of always-on rules, not the bytes:
  the read is cached INPUT and 1,174 characters is ~300 tokens. (1) The FILE half
  of the aggregation rule — the platform offloads a tool result past ~100,000
  characters to a file, but our own 200-row page cap keeps every reporting response
  below that, so six lines described a branch nothing can reach; the INLINE half
  stays, it is the one that gets forgotten. (2) The twenty-record cap was written
  THREE times (prompt, a bullet, and the named-column row); the bullet went and its
  "ceiling, not a target" clause moved into the row, so it now reads twice — the
  prompt, which survives a summary, and the row where the decision is taken. (3)
  "Never print a table the link already opens" restated the two table rows directly
  above it. (4) "grep it directly" — the third path `--list` exists to replace. (5)
  The block-reading paragraph compressed into the table under it. Two tests changed
  with them and say in their docstrings what was cut and when to put it back.
- **The system prompt is routing plus what has no skill, and it was HALVED on
  2026-09-01.** 3,045 -> 1,607 tokens (9,020 -> 4,727 chars, 64 -> 45 lines),
  measured with `messages.count_tokens` against `claude-opus-4-8`. The platform
  cap is 100K CHARACTERS, so size was never the constraint — the count of
  always-on rules was. WHAT WENT: everything procedural for the allocation lane
  (the determinism restatement, the bump and ask-first gates, the infeasible
  rule), the whole planner-voice section (channel, no-plumbing, lead-with-the-
  outcome), the mount paths, the app-MCP environment bullet, the write-back
  approval line, and the allocation-links clause. The allocation lane is now ONE
  line pointing at its skill. WHAT STAYED: the two-lane routing, the taxonomy
  lookup INVOCATION (which has to be readable before the skill arrives — see the
  phrasebook bullet), five hard rules, Links, and Reporting — the last
  restructured into an explicit FIRST (read the skill and resolve every term in
  ONE block) / THEN (the four rules that apply once both have landed), because a
  single dense paragraph did not say which step comes first.
  `tests/test_agent_contract.py` went 85 -> 79: six tests pinned nothing but cut
  sentences and were deleted whole, five more kept their live half and lost the
  dead assertions. THREE rules that were deliberately said TWICE now exist once or
  not at all — the ask-first gate (skill only), the app-MCP fence and the
  allocation-links clause (NEITHER, see their own bullets). The cut was the user's
  call, made deliberately and rule by rule; the reason for each one is in git
  history — read it before restoring anything.
- **A whole prompt+skill pair swaps on one env var, and only that pair.**
  `XAS_VARIANT=minimal uv run python setup_agent.py` deploys
  `variants/minimal/system-prompt.md` in place of `SYSTEM_PROMPT` and
  `variants/minimal/xas-reporting.SKILL.md` in place of the reporting SKILL.md
  (see `variants/README.md`); unset deploys the full pair, so the default run and
  the whole suite are untouched. Everything else — `resolve.py`, `dates.py`,
  `charts.md`, the rendered `phrasebook.tsv`, the allocation bundle —
  ships identically either way, which is what makes the two comparable. Both
  files must exist or the run exits naming the missing one, and `main()` prints
  which pair went out. `minimal` (2026-09-03) is the experiment in saying what
  each component is FOR and leaving the reasoning to the model: it keeps the two
  link kinds and the never-show-the-kitchen rule, drops every procedure, and its
  prompt says NOTHING about allocation — the `xas-allocation` skill's own
  description is the only thing routing to it. `tests/test_agent_contract.py`
  pins the FULL pair by phrase, so it passes under either and proves nothing
  about a variant: a variant is verified by hand.
- **The `minimal` prompt carries the tenant's TYPES inline, and they are
  GENERATED, never typed.** 2026-09-03: `{{CLASSIFICATIONS}}` in
  `variants/minimal/system-prompt.md` is substituted at deploy by
  `phrasebook.classification_block()`, from the same `index.md` the table is
  built from — a hand-written list in a prompt is a second copy of the taxonomy,
  free to drift. It holds the 33 classifications of the three entities a read
  tool can FILTER (job cards 23 with their link page, vehicles 5, accounts 3);
  the 19 under Item / Activity / Model / SalesModelObjects / VehicleModels stay
  out because no tool can filter them. The price is ~1,050 tokens on EVERY turn,
  the allocation lane's included (prompt 558 -> 1,617 tokens), against one saved
  round trip on the turns where a type was the only word needing translation —
  not the ones that also need a date range or a status list. Pinned by tests in
  `tests/test_phrasebook.py`, including that no `{{...}}` marker survives the
  substitution.
- **One taxonomy, one place: the shipped table drops what the prompt carries.**
  `build(include_classifications=...)`, keyed off `setup_agent.TYPES_IN_PROMPT`
  (true when the variant prompt held the marker). Under `minimal` the bundled
  `phrasebook.tsv` is 244 rows — statuses, branches, states, entities — and
  `--lookup`/`--list` answer for those only; under the full pair it keeps its 373
  and nothing changes, because that prompt has no type list and nowhere else to
  resolve a type from. Two copies of one taxonomy in front of one model is what
  the marker exists to avoid.
- **The taxonomy index is ACTIVE ONLY as of 2026-09-03, and that is a decision
  with a known cost.** `VGR` and `LeaseContract` were inactive in the tenant
  config, hand-maintained in `index.md`, and kept until this date because they
  hold **39 live job cards** (24 + 15, counted 2026-08-30). They are now removed
  along with their STATUS lines — 51 classifications, 109 statuses — at the
  user's call, twice stated. So a breakdown by type reports those 39 cards under
  NO type, which is exactly the failure the previous test pinned against; the
  test now pins the exclusion instead (absent from the index, the prompt list and
  the table), and git history holds the old one. All three status names those
  types carried (`Active`, `QUOTAION`, `Vehicle Ready`) still exist under active
  types, so no status became unresolvable. A regeneration from the config drops
  them anyway, which is why nothing has to be re-excluded by hand later.
- **A follow-up over rows already in the transcript is a formatting job.** Added
  to the prompt's Reporting list on 2026-09-02: a live turn 3 spent 20s
  re-reasoning over records a previous turn had already returned, same filter,
  same records. The rule is in the PROMPT, not the reporting skill, because the
  turn it fires on is the one furthest from the skill read — and it is scoped
  under Reporting, so the allocation lane pays for one line and no procedure.
  `tests/test_agent_contract.py` pins it by phrase.
- **The phrasebook is TWO modules, split by where they run, and they share one
  `normalize`.** `phrasebook.py` at the repo root parses `index.md` and renders
  the table (host-side, never shipped — the same hop `flatten.py` is for the
  pull); `skills/xas-reporting/resolve.py` is the query side the agent runs,
  ONE verb `--lookup`, and it is the ONLY one in the bundle. The
  builder IMPORTS `normalize` and `COLUMNS` from the skill file, never the
  reverse: the skill file has to stand alone in a sandbox that cannot see this
  repo, so it owns anything both sides need. That direction plus "render at
  bundle time, never commit the table" is what makes normalizer drift
  impossible — a skill version physically cannot hold a table built by a
  different `normalize`, and if it ever did, the `normalized` column would stop
  equalling `normalize(surface)` and the exact rung would miss in silence.
  `tests/test_phrasebook.py` pins that equality over the BUNDLED bytes, and that
  the parser is absent from every shipped file.
  **`--lookup` answers EVERY wording, and that is a 2026-09-02 fix.** It used to
  work rung-before-term and RETURN at the first rung any wording reached, so the
  other wordings were discarded in silence — which made the prompt's own
  instruction (send every wording you would have tried, in one call) actively
  harmful: a hedge that hit a higher rung hid the wording that meant what was
  asked. Live, "inventory vehicles by status" hedged with "in stock" and got back
  one status row, with both `Inventory Vehicles` classifications hidden. Now each
  wording gets its own best rung, the rungs ORDER the blocks (exact first) instead
  of suppressing them, rows already printed are not repeated, and a whole-call
  ceiling (`TOTAL_LIMIT`) sits on top of the per-term `LOOSE_LIMIT`. The
  nearest-spelling rung stays a WHOLE-CALL fallback deliberately: per wording it
  fires on every hedge word the agent invented — `status` is nobody's term here
  and its nearest neighbour is `Task` — and the skill instructs the agent to act on
  a `CONFIRM` line. Two things this does NOT fix and that were considered
  separately: an exact hit still says nothing about near-siblings that merely
  CONTAIN the term (a live run on 2026-09-02 saw both `Inventory Vehicles`
  classifications in one block and still answered about one of them without
  asking), and enumerating a bucket list is not a lookup — see the next bullet.
- **`--list` is the SECOND verb, and it exists because a bucket list is not a
  lookup.** `--lookup` answers what a word MEANS; nothing answered what the values
  ARE, so a session that needed every vehicle status invented plausible names
  (`Sold`, `Delivered`, `In Transit`), looked each guess up, then guessed codes
  `12`/`13`/`14` — three round trips to arrive at a list still missing `99
  Disabled` (2026-09-01 spent a 15-iteration shell loop on the same hole; the awk
  recipe that used to answer it was cut in `96ab2a4`). `resolve.py --list
  kind=status entity=Vehicle` takes any `<column>=<value>` and prints ONE row per
  RECORD in code order: distinctness is (code, name, id), so the eleven JobCard
  classifications sharing `01 New` collapse to one bucket while vehicle `02` stays
  TWO — the collision rule again, this time enforced by the output rather than by
  prose. Aliases collapse into the printable name row (`1212` -> `Inventory
  Vehicles (Truck)`), and `BUCKET_LIMIT` caps a tenant whose classifications run
  to hundreds. It lives in the SKILL, not the prompt: unlike the lookup it is
  never wanted before the skill has landed, and the allocation lane pays for every
  prompt line. `tests/test_agent_contract.py` pins the invocation INSIDE the
  bucket-loop row — a command one paragraph away from the decision is the shape
  that got cut last time.
- **A filter guessed from the MCP's own `fields` enum returns 0, not an error.**
  On 2026-08-31 a session read SKILL.md and fired `{"inventoryStatus": "InStock"}`
  in the SAME block — so it filtered before it had the procedure it was fetching —
  then answered the 0 with an unfiltered 40-row pull instead of the one `count: 1`
  control the skill allows. Three calls for a one-call count, and 40 padded rows
  left in the conversation to be re-read every later turn. The enum advertises
  names the server does not honour (`InventoryStatus` really holds `"1"`–`"5"`),
  and the phrasebook had the answer outright: `In Stock` is a Vehicle STATUS,
  code `03`. Two rules now carry it — the prompt orders the skill read BEFORE the
  first MCP call and bans filters taken from a field list, and the skill's opening
  **Three sources** table scopes that list to `fields` alone. Neither is structural;
  the prose is the whole mechanism. The ban is on the CALL, not on the block: a
  taxonomy lookup rides WITH the skill read, because its words come from the
  planner's question rather than from the procedure being fetched, and a lookup
  cannot come back wrong where a filter can. Reworded 2026-08-31 — the read is a
  round trip of its own (~9s and 17k tokens on the first reporting turn of every
  session) and the block after it was always the same grep. **Permission was not
  enough** (2026-09-01): the prompt said the lookup MAY ride along and named
  `resolve.py --lookup`, but the RUNNABLE command — its path and its
  many-wordings-at-once form — lived only in SKILL.md, so the agent could not fire
  it until it had read the skill. Eight live sessions measured that day all spent
  two serial round trips (~5–11s) before touching data and not one rode along. The
  INVOCATION now lives in the prompt and only there; the skill keeps how to READ the
  result, which is not wanted until the result is in hand — by which point both have
  landed. **Moved again 2026-09-02, out of its own `Environment` section and into
  the Reporting step that fires it**, with the ban on reading the table any other
  way; `Environment` held nothing else and is gone, the allocation lane's "read
  your skill" riding on its routing bullet instead. Same fix as `--list` beside the
  bucket row: a command one section from the decision is a command that is not run.
  The both-directions clause was said in both places and now reads once, in the
  THEN bullet where each direction is acted on. Splitting it that way is also why the prompt does not grow for the
  allocation lane, which pays for the prompt and never reads this skill. There are THREE sources
  and each supplies exactly one thing: the tool says what you may SEE, the taxonomy
  supplies filter VALUES, the recipes supply filter KEYS. Both sides used to get that
  wrong in opposite directions — the prompt sent the agent to the taxonomy for a key
  it does not hold, and the skill's own rule said the same in its heading while its
  last sentence said the opposite. Whichever half was believed, one of them was a dead end, which is the
  shape of the guess above.
- **A tally is ONE page, and the page is 200 — the server's own maximum, not
  ours.** The skill said 50, so a 51-card tally came back one short and the agent
  spent a whole round trip on page 2 to collect a customer already in its list: 17s
  of a 45s turn, and the general rule that forbade it ("never walk pages to compute
  an aggregate") sat in a later paragraph than the bullet where the decision is
  taken. Raised 2026-08-31, with the never-page rule moved next to the decision. Any
  cap has an off-by-one case; 200 makes it rare, and a shortfall now means the page is
  a SAMPLE that holds no tally at all (2026-09-01) rather than "fetch the rest" — the
  earlier wording, "too big to tally, loop the buckets", read as a routing hint and a
  session printed a 200-of-1,334 sample anyway. 200 is not free, so what
  bounds a page is stated as BYTES: 200 of one scalar is cheap, and the `Accounts.*`
  fields are whole owner objects (~175 tokens a row, phone and e-mail included) — see
  `docs/appmcp-requests.md`, which asks for a sub-field so a customer name costs a
  customer name.
- **Bucket-looping has NO cap, and what decides the path is the PHRASEBOOK, not the
  bucket count.** The table split a breakdown at five buckets — loop below, hand-tally
  above — and on 2026-09-01 "what inventoy vehcles we have by status?"
  (`sesn_01Ar2oFNgj7nskxibNPLNuTS`) did both: twelve parallel `count: 1` calls
  answered the whole 1,334-car fleet EXACTLY in one round trip for ~9k characters, and
  the same turn then pulled 200 rows (34,173 characters) that contributed nothing to
  the answer and could not have. Nothing about a `count: 1` call gets dearer at the
  sixth bucket, so the cap bought nothing and cost a call. The rows path survives for
  the case that actually needs it: a grouping key whose VALUES cannot be enumerated in
  advance — customer, model — where there is no bucket list to loop. `distinct` /
  `group by` in `docs/appmcp-requests.md` is now the ask for exactly that residue, and
  it is the only remaining tally case. Re-read in full on 2026-09-01: that same turn
  spent EIGHT model round trips on a three-trip answer (173s, 11,559 output tokens,
  $0.81), and two of the five wasted ones broke rules the agent had read that turn —
  the size probe before the buckets and the set link left to the end. Prose did
  not hold them, so two rules were made CONCRETE in the skill instead: the residual is
  worked through with its arithmetic (twelve buckets summing to 723 against 1,334
  leaves 611, confirmed once with `{"status.code": {"$in": [null]}}`), and **two
  phrasebook rows sharing one code are TWO buckets** — vehicle `02` is both
  `On The Way` and `Available For Sale `, so a `status.code` call returns their sum and
  hides the split; count each by `status.name` with `$like`. That collision was the ONE
  finding in that trace specific to a single entity, which is why the reporting skill is
  not split per entity: the other four were generic and a per-entity split would have
  copied them three times. The examples for hand-rolling a phrasebook enumeration and
  for printing a tenant's misspelled status name verbatim were considered and declined.
- **A tool result past ~100,000 CHARACTERS becomes a file, and only then is
  aggregation code work.** The platform offloads any oversized tool output — MCP tools
  included — to a file in the sandbox and returns a truncated preview plus the path
  (`shared/managed-agents-tools.md`, "Large tool outputs"); there is no knob and no way
  to lower the threshold. So the skill's rule has TWO halves and the second is the one
  that gets forgotten: a result on disk is tallied with python in `bash`, and a result
  INLINE is counted in the model, because re-emitting it into a bash command pays for
  the payload a second time in OUTPUT tokens. Measured against the vehicles turn:
  ~90s to retype 34,173 characters against ~15s to read them, plus a second copy of
  the payload left in the transcript to be re-read on every later turn. Nothing we
  send controls which side of the cliff a response lands on — the 200-row page cap
  keeps every reporting response deliberately below it, so in practice the file half
  fires on no reporting call we make today. Programmatic tool calling, which WOULD put
  a result in code without the size cliff, is a Messages API feature and is not
  compatible with MCP tools; it is not available on this surface.
- **The app MCP returns the links now, and every path we built is DELETED
  (2026-09-03).** Probed live against the dev tenant that day: all six read tools
  return a `Url` per record (`/job_cards/61`, `/vehicles/11330`,
  `/accounts/<Id>` — nested `include:` sections too), and the three list tools a
  top-level `ListUrl` over exactly the filter just run. So `skills/xas-reporting/link.py`
  and `tests/test_link.py` are GONE, with the skill's link-building rules, the
  prompt's, and both `variants/minimal/` files'. Verified equivalent before
  deleting, against the app's own parser (`app/src/services/searchParams.ts`): a
  URL with only `filter` reads back as page 1 / count 20 / no sort / no kpi —
  identical to what `link.py` emitted with its `LINK_PAGE_SIZE = 20`. And the
  server does the three things that file existed for: it percent-encodes `$` (a
  raw one returned an EMPTY page, not an error), it normalizes a bare value into
  `{"$in": [...]}` for the vehicles/accounts dialect rather than letting the page's
  adapter re-wrap it as a `$like`, and it picks the classification's page itself
  (`VSO` -> `/vehicle_planning`, `Contract` -> `/contracts`). The rule is now one
  line in both prompts: **use what came back, build nothing.** Four things to know.
  (1) **`ListUrl` echoes `Branch: true` / `MyJobCards` verbatim** where `link.py`
  refused them. That was never really a link bug — the agent asks as the
  integration login, so the COUNT is already about the wrong person — so it
  survives as a FILTER ban in the skill, not a link rule. (2) **A card still has
  no link to its customer or its car.** `Accounts.Owner` carries `AccountUUID` +
  `AccountName` and no `Url`; a vehicle row's `Owner` carries `Code` and the
  account page routes on `Id`, so that one cannot even be composed. Composing
  `/accounts/<AccountUUID>` was right on 389 of one customer's 403 cards (13 ids
  no account answers to, one a DIFFERENT account's under the same name), so the
  skill now names customers in PLAIN TEXT rather than on a 3.5%-wrong path. That
  asymmetry is still why the customer FILTER is `Accounts.Owner.AccountDMSCode`
  (403) and never the UUID (389, and silent about the difference). (3) **TWENTY
  named records is still the cap** — "which vehicles does Hertz hold" printed 63
  linked rows on 2026-09-01, the table the set link already opens, re-read on every
  later turn. Past twenty the set link IS the list; twenty is a ceiling, not a
  target. The `minimal` pair says TEN, deliberately unreconciled. (4) **Allocation
  answers carry NO links** — those orders and cars come from the frozen pull, so an
  id that looks routable may open something else — and that rule is STILL written
  in no file: it was cut from the prompt on 2026-09-01 and `link` appears nowhere in
  `skills/xas-allocation/SKILL.md`. Both prompts' Links sections read as
  unconditional, and now every MCP row hands over a ready path, which makes the gap
  worse rather than better. Restoring it is one clause in either place.
  Every link is still RELATIVE (2026-08-31) and now unavoidably so — the server
  returns paths, not origins — so answers rendered outside the app, the `web.py`
  demo chat included, have dead links until that surface resolves them.
  What survives of the old machinery is the phrasebook's **`route` column, whose
  job was never really the link**: it groups a job card's types into the three
  business areas a planner asks for by NAME ("vehicle sales", "sales cards"), which
  `--list route=/vehicle_planning` enumerates and the `minimal` prompt renders as
  headings. Its four tests moved whole from `tests/test_link.py` into
  `tests/test_phrasebook.py`. Two things that column does NOT solve, both open: the
  FULL pair carries no type list and no GROUP rows in its table, so `--lookup
  "vehicle sales"` substring-matches 3 of the 10 vehicle-planning types and says
  nothing about the other 7 — putting GROUP rows in the table was considered on
  2026-09-03 and declined by the user; and a job-card filter naming no
  classification has no single area, where the server just picks `/job_cards`.
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
  that rule was the only thing standing between a planner and a plausible,
  irreproducible answer. `tests/test_agent_contract.py` used to pin its presence;
  the rule and those tests were cut on 2026-09-01 (see the app-MCP bullet above),
  so nothing pins it and nothing states it. Verify by hand, every time: ask for a
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
- **ONE ROW IS ONE ORDER, and there are no lines any more.** The key is the
  export row's own `OrderId` (`502377`) — ONE level. The two-level
  `{so_id}-{line}` key, the `Quantity` question with it, went out with the app-MCP
  job-card grain on 2026-08-27: this export has no lines and no `Quantity`
  column, so there is nothing to expand and nothing left uncounted. (Earlier
  still, on 2026-08-25, qty expansion itself was replaced — `qty_index`, the
  per-car report naming and the `allocation_qty_not_resolvable_to_cars` counter
  are all gone.) A line-grain pull would bring the whole question back; do not
  reintroduce one without deciding it. An order is NAMED by string in four places (`priority`,
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
- **No repair without asking the planner what matters first.** Added 2026-08-27:
  the allocation skill gates every solve behind one question — which orders or
  CLIENTS count for more, which must keep the car they hold, anything else that
  should hold —
  asked AFTER the discrepancy report and BEFORE any plan is offered. Solving
  first and asking after is the failure it prevents: a plan on the table is an
  anchor, and the planner corrects it instead of stating their own preferences.
  "Fix it" is a request for a repair, not an answer; "nothing special" IS an
  answer and may never be assumed. It used to be stated twice on purpose — the
  skill's own section and a prompt hard rule, because the skill body can be
  summarised — but the prompt half and its test were cut on 2026-09-01 in the size
  pass. It now lives in ONE summarisable place, `## Before you repair — ask what
  matters, every time` in the allocation skill. Nothing structural can force an
  ask, so that section is the whole mechanism.
- **The client's name is a LABEL, and it is the only thing the planner steers by
  that the solver cannot see.** `customer.name` reaches `Order.customer`, the
  three planner-facing tables, the bump list and `plan.json` (2026-08-27), so the
  agent can group orders by client and answer "which are Delek's". Nothing reads
  it — not eligibility, not cost, not a filter: the `customers` filter dimension
  was removed the same day and stays gone. The consequence is the agent's job:
  every lever names ORDER IDS, so a client instruction must be resolved to ALL of
  that client's orders and the ids confirmed back. Resolve two of three and the
  client is half-prioritised with nothing to catch it. The names are also
  ASSIGNED, not the vendor's — see the carve notes below.
- **`may_move` precedence is part of the contract: never beats only beats also.**
  `only` bounds the WHOLE turn (including anything `also` authorised) and NARROWS
  the default rather than replacing it — the `scope` key it replaced replaced the
  set, so a scope freed settled on-time orders nobody had authorised anyone to
  touch. `also` is the one place permission to displace is granted, and it is
  permission, not an instruction: the solver still declines a bump that buys
  nothing. `never` is absolute, and it is the only way to hold an order that is
  itself late. `also` takes a filter or the fleet-wide `true`, and it is the ONE
  key that EXPIRES: `session.carry_forward` drops it after the solve, because a
  permission that persisted would bump a settled order on the strength of a
  sentence said three turns ago. `{}` is not `true` — an empty filter widens
  nothing, so a half-built override cannot open the book.
  `tests/test_may_move.py` pins all three, and `tests/test_bump.py` the expiry.
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
  `planner_report` is the only renderer OF A PLAN. Reinstating per-car rows means
  reinstating the grouping rule with it: collapsing was only ever allowed when
  every displayed column agreed.
- **Three planner-facing reports, and the third exists so the agent stops writing
  its own.** `discrepancy_report` is what the delay broke, `planner_report` is
  what a solve did, and `current_state_report` (2026-08-30) is the whole book as
  it stands — every order, the car it holds, on time or not, worst first. It
  solves nothing and takes no override; it is a READ of the snapshot. It was
  added because "show me all the allocations" had no helper, so a session
  answered it by scripting over `snapshot.json`, and then explained the result
  with free-car counts it had worked out itself — one of which its own printed
  data contradicted. A question the helpers cannot answer is a question the agent
  will answer by hand, which is the leak the whole design is built to stop.
  `discrepancy_report` and `current_state_report` both open with
  `exclusion_note`, so the agent prints ONE of them, never both.
- **The closing caveat knows whether a bump was already authorised.** `_caveat`
  takes the override and whether anything was displaced, because "authorising a
  bump might help" said to a planner who authorised one in the same breath reads
  as the solver ignoring them. Granted-and-unused is an ANSWER — the solver
  declines a displacement that buys nothing — and the report now says that
  instead. `also: {}` is an empty filter that frees nobody, so it is correctly
  NOT an authorisation here either.
- **The scenarios are the data of record, and they are BUILD OUTPUT.**
  `data/scenario-{unallocated,delayed,mixed}/` each hold `orders.csv` +
  `vehicles.csv` + a `scenario.json` sidecar, carved out of the real export by
  `scenario_engine/real_*.py`. Editing one by hand is editing build output — the
  next carve overwrites it. Change a scenario by re-running the script or its
  knobs, never the CSVs. As of 2026-08-27 all three are **10-order books** and a
  bare run of each script reproduces the committed files exactly — the prompt
  defaults ARE the carve (8/0/2, 0/8/2, 4/4/2 by no-car / late / on-time). The sidecar exists because the pull date has no column
  and cannot be the clock: `datasource.scenario_now` reads it, `XAS_PULL_NOW`
  overrides it. The fabricated `scenario_engine/generate.py` world and the two
  `data/mcp-*.json` payloads it authored were deleted on 2026-08-27 — with the
  export as the only source there is nothing for an invented vocabulary to be
  substitutable for, and `data/pull.json` / `data/baseline.json` went with them.
- **A book is THREE classes, and the on-time ones are a SHARE, not a remainder.**
  Unallocated, late, and allocated-and-on-time — `--on-time-pct` (20% by default)
  sets the third as a share of the whole book, so the book SIZE follows from the
  disturbance counts (`--empty 4 --late 4` at 20% is 10 orders) and the car subset
  follows from the book. `--subset` is gone with that (2026-08-27): asking for
  both a car count and an order mix let the two disagree. The on-time orders are
  the control group — with every order in the book needing something, a plan that
  moves everything cannot be told from one that moves only what it should. Two
  traps. The draw takes only orders that are ACTUALLY on time: the export ships
  256 already-late orders, and drawing the remainder at random folded some in, so
  `--late 100` reported ~124 and the share was a lie; `carve` now re-measures the
  finished files and RAISES if one got in. And a mostly-unallocated book forces
  `--available-pct` HIGH — every emptied order's car is free by construction, so 8
  emptied of a 10-order book cannot sit under 80% — which is why the unallocated
  scenario carves at 85% and the error names the floor.
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
  status keeps its REAL trailing space, `'Available For Sale '`. `customer.name`
  is the one column that is NOT the vendor's export (added 2026-08-27): 30
  customers assigned at random, seeded, so the book has someone to name. It rides
  through the carve because the header is copied, and since 2026-08-27 it also
  rides through `datasource.translate` (as `Customer`) into `Order.customer`, the
  discrepancy and change tables, the bump list and `plan.json`. So every client
  name a planner sees is ASSIGNED, not the dealer's own — real names arrive with
  real dealer data, and nothing about the plan changes when they do. Two traps: only an
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
  the whole book is the worst thing this pipeline can do. On the three 10-order
  scenarios nothing drops at all — the carve already prunes to the models in play
  — which is what makes the OTHER half of that note load-bearing: it also names
  the orders **holding no car** — 4 of 10 mixed, 8 of 10 in the unallocated carve,
  where "no orders are late" on its own would read as "nothing to do".
- **The pull mounts files, not a seed, and not the rows in-band.** The source
  runs here; the agent runs there; everything the *tool* returns crosses into its
  context. So the tool returns only a summary + a `flatten` command; the rows
  travel as the two mounted files (read host-side, out of the sandbox's sight) and
  `flatten` reads them there — nothing dumps ~100KB of JSON into the transcript.
  The scenario scripts' *code* stays out of the sandbox; only the translated
  *output* travels in. The summary carries counts, the scenario name, the drop
  funnel and the min/median/max days late — no rows, and no customer map: the
  client's name rides on the order rows in the mounted file, so there is nothing
  for a separate map to key.
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
- **Two are recorded but deliberately not built.** DECIDE-10 — a reserved-for-X
  vehicle is out of the pool entirely, so an earmarked car is supply for NO ONE,
  not "eligible for anyone" as the register used to claim; modelling it as
  earmarked supply is the deferred upgrade. This export has no such row —
  its pool is three statuses and none of them earmarks — so the decision waits on
  a source that carries one. DECIDE-6 — there is no liveness
  check and will not be one: the pull happens host-side before the session
  exists, so a session-start call from the agent proves nothing about it.
- **DECIDE-7 is settled and unblocked.** The source is the export's two CSVs, so
  nothing waits on a widened MCP projection any more (the blocker was: no
  `jobitems`, so every dev job card dropped and the live allocation pull came back
  EMPTY). The change request that went with it was deleted on 2026-08-30 — it
  was answered by dropping the MCP source, not by widening the projection.
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
