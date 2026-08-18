# One agent, two skills — Implementation Plan

Merge the allocation agent and the reports (Q&A) agent into a single Managed
Agent whose specialisation comes from **skills**, not from separate agent
objects. One agent ID, one environment, one run plane (`web.py`).

Confirmed decisions (2026-08-18):

- **Baseline** — merge `claude/vpo-jobcard-data-sopb7f` into the migration
  branch first, then unify. Verified clean: the two branches touch **zero**
  files in common.
- **Session shape** — one session carries **both** capabilities. A planner can
  repair an allocation and then ask for a chart in the same conversation.
- **Run plane** — reports fold into `web.py`. `run_qa.py` and
  `qa/setup_xas_qa.py` are deleted.
- **Credentials** — keep the allocation agent's. The QA agent, environment and
  skill were **never created in Anthropic** (`qa/setup_xas_qa.py` was written but
  never run, and there is no `qa/.env`), so there is nothing to migrate, orphan
  or clean up. The merged agent **is** the existing allocation agent, updated in
  place to carry a second skill.
- **Taxonomy** — one committed dictionary for now; the **frontend** chooses which
  one to load later, so the seam is a session-create parameter, not host config.
- **Fake data** — the allocation and reports datasets stay separate. They are
  different mechanisms and are not being unified.

## The change, in one line

`agents.create(skills=[xas-allocation, xas-qa])` — one agent, both skills, and a
system prompt thin enough that the skills do the specialising.

## The principle it respects

> `plan = pure_function(data_snapshot, skill, override)`

Merging the agents does not merge the invariant. The allocation lane keeps its
guarantee only if **every allocation claim still comes from the solver**. The
chosen session shape (both capabilities, one session) is the shape where that is
hardest to hold, because the agent can now see `jobcards.json` and could answer
"which orders are late?" by grepping records instead of running
`session.discrepancy_report`. That answer would look right and be
non-deterministic. **This is the central risk of the merge**, and §Fix 3 and
§Tests exist to hold it.

Reporting has no such invariant — it is a read-only query over mounted data — so
the merge is asymmetric: the allocation lane constrains the reports lane, never
the other way round.

## Fix 0 — merge the branches (no code change)

```bash
git merge claude/vpo-jobcard-data-sopb7f
```

`git merge-tree` reports a clean merge and the changed-file sets are disjoint
(`qa/`, `skills/xas-qa/`, `docs/xas-*` vs `xas_allocation/`, `alloc_tools.py`,
`data/pull.json`). After this the tree has the real-XAS vocabulary (VSO
job-items, `vehicle_classification`, `break_cost`) **and** the reports lane.

→ verify: `uv run pytest` (60 tests) and `PYTHONPATH=. uv run python
tests/test_invariant.py` (4/4) both pass on the merged tree.

## Fix 1 — one agent, one setup script

`setup_allocation_agent.py` + `qa/setup_xas_qa.py` → **`setup_agent.py`**.

- `skills=[{custom, ALLOC_SKILL_ID}, {custom, QA_SKILL_ID}]` — 2 of the 20 the
  API allows.
- `tools=[agent_toolset_20260401 (web_search/web_fetch off), PULL_TOOL]`.
- `model="claude-opus-5"` on the agent; `web.py` keeps its per-session
  `agent_with_overrides` model picker.
- `skill_files()` becomes `skill_files(skill_dir, extra_package=None)` so one
  helper builds both bundles: `xas-allocation/` + the `xas_allocation` package,
  and `xas-qa/` (SKILL.md + `phrasebook.py`, no package).
- **Env keys are unchanged.** `ALLOC_AGENT_ID`, `ALLOC_ENV_ID`,
  `ALLOC_SKILL_ID` and `ALLOC_DOWNLOAD_DIR` keep their names and their values —
  they already point at the agent this plan keeps. The only addition is
  **`QA_SKILL_ID`**, printed on first run. Existing `.env` files keep working, so
  there is no migration and no stale-key check to write.

Because the agent already exists, the first run takes the `update` path: it
pushes a new version of the allocation skill, creates the QA skill, and updates
the agent with both. Nothing new is created except the second skill.

→ verify: `uv run python setup_agent.py` prints `QA_SKILL_ID` and reports the
agent updated; re-running updates in place and prints new skill versions.

## Fix 2 — the two skills stop competing for the same turns

Skills are selected at runtime by their **description** — it sits in context and
the model reads the body when the description matches. Two skills that both
claim "job cards" will mis-fire.

`skills/xas-qa/SKILL.md` frontmatter currently ends with *"Use on every turn that
names a document type, a status, a lifecycle word, or an entity."* That is an
always-fire trigger written when this skill was alone on its agent, and on a
merged agent it fires on allocation turns too — a VSO **is** a document type and
"late" reads as a lifecycle word. Narrow it:

- **xas-qa** — *"…answer reporting questions and draw charts over the mounted
  job-card records. Use for questions ABOUT the data (counts, breakdowns,
  statuses, charts). Do NOT use for allocation repair — vehicle-to-order
  assignment, lateness, or bumping belong to xas-allocation."*
- **xas-allocation** — unchanged, but add the mirror sentence: *"Do not use for
  general reporting over job-card records."*

→ verify: `tests/test_agent_contract.py::test_skill_descriptions_are_disjoint`.

## Fix 3 — the system prompt becomes a router with one hard rule

Today's allocation prompt is ~35 lines carrying procedure that belongs in the
skill. The merged prompt keeps identity, the routing sentence, and the HARD
RULES — everything procedural stays in the two skills.

The load-bearing addition, stated as a hard rule:

> Every claim about allocation — which order is late, which vehicle an order
> gets, what a repair costs, who would be bumped — comes from running the solver
> through the xas-allocation skill's helpers. Never from reading
> `/workspace/reports/jobcards.json` or any other file. Those records answer
> reporting questions only. If you cannot answer an allocation question by
> running the solver, say so; do not substitute a number you read.

Plus the mount map, which is now three paths (§Fix 4) and is what makes the rule
checkable.

## Fix 4 — `web.py` mounts three resources and absorbs the reports lane

Session creation currently mounts one file. It becomes three, namespaced so the
prompt rule can key on paths:

| Mount | Source (host-side) | Lane |
| --- | --- | --- |
| `/workspace/pull.json` | `datasource.get_source().pull()` | allocation |
| `/workspace/reports/index.md` | `datasource.get_taxonomy()` | reports |
| `/workspace/reports/jobcards.json` | `datasource.get_records()` | reports |

`datasource.py` gains `get_taxonomy(name=None)` / `get_records()` alongside
`pull()`, keeping DECIDE-7's shape: a callable source, fake by default
(`data/xioma-DMSDEV2023-flat-index.md`, `qa/data/sample_jobcards.json` — moved to
`data/`), real XAS by config. The taxonomy is **mounted, never bundled** — it is
per-tenant and drifts with the DMS config, so regenerating it must not need a
skill redeploy.

**The frontend picks the taxonomy.** `get_taxonomy(name)` takes a parameter and
`POST /session` grows an optional field on `NewSession` (which today carries only
`model` and `title`). Omitted, it falls back to the single committed dictionary —
the behaviour for the whole prototype. This is deliberately *not* a host-side
config switch or an auto-detect: the caller knows which dealership it is asking
about, and putting the choice in the request body is what lets a second tenant
arrive without touching `web.py`.

The two fake datasets stay independent. `pull.json` is regenerated by
`scenario_engine` from a seed; `jobcards.json` is a fixed 24-record sample. They
describe different slices (sales orders vs service jobs) and are not being merged
into one fabricated world — see Gotcha 4 for what that means in a shared
session.

What `web.py` already has and does **not** need building: the per-session
`tool_runner`, SSE event stream, `/session/{id}/files`, and
`/session/{id}/files/download`. Charts are just session output files, so the
existing download route covers them. Remaining work is small:

- mount the two extra resources at create;
- filter the mounted inputs out of the download listing (`run_qa.py` did this via
  `INPUT_NAMES`; port it);
- surface written files in the UI — `_render` drops built-in tool results, so a
  chart the agent writes is currently invisible until the planner hits `/files`.
  Add a `files_changed` nudge on `session.status_idle`.

Delete `qa/run_qa.py`, `qa/setup_xas_qa.py`, `qa/.env.example`; fold `qa/README.md`
into the root `README.md`.

## Files touched

| File | Change |
| --- | --- |
| `setup_agent.py` | new — replaces both setup scripts |
| `setup_allocation_agent.py`, `qa/setup_xas_qa.py`, `qa/run_qa.py` | deleted |
| `web.py` | 3 mounts, input filter on download, files nudge, `XAS_*` env |
| `datasource.py` | `get_taxonomy()`, `get_records()` |
| `skills/xas-qa/SKILL.md` | narrowed description |
| `skills/xas-allocation/SKILL.md` | mirror exclusion sentence |
| `data/` | taxonomy + jobcards move here from `qa/data/` |
| `.env.example` | one new key: `QA_SKILL_ID` (existing `ALLOC_*` untouched) |
| `CLAUDE.md`, `README.md`, `COMMANDS.md` | one agent, one run plane |
| `tests/test_agent_contract.py` | new |

## Tests

`tests/test_agent_contract.py` (host-side, no API key, no network):

1. the agent's `skills` array carries **both** skill IDs, and `tools` carries
   `PULL_TOOL` — `agents.update()` preserves omitted array fields, so a setup
   that forgets one silently ships the old value;
2. skill descriptions are disjoint — neither contains the other's trigger nouns;
3. the system prompt contains the solver-authority rule verbatim;
4. the three mount paths are distinct and the reports pair is under
   `/workspace/reports/`;
5. `skill_files()` produces a bundle with `SKILL.md` at the root of each skill
   directory, and the allocation bundle contains `xas_allocation/solver.py`.

Extend `tests/test_tool_contract.py` for the two-skill agent. `test_invariant.py`
and `test_phrasebook.py` are unchanged.

**What tests cannot cover:** whether the model actually routes correctly. That
needs a live session, so add `docs/evals/routing.md` — a short checklist run by
hand after each prompt change: an allocation question, a reporting question, a
Hebrew reporting question, and the trap ("how many of my late orders are for
Colmobil?" — must run the solver, not grep).

## Gotchas

1. **The custom-tool hang, now on every session.** `PULL_TOOL` is declared
   agent-wide, so *any* session can emit `agent.custom_tool_use`. An unanswered
   call parks the session on `requires_action` that **never times out** — the
   failure looks like a hang, not an error. `web.py` already starts
   `_answer_custom_tools` at session create, before the planner can send
   anything; deleting `run_qa.py` removes the one run plane that had no runner.
   Keep it that way: **no second entry point may create a session without
   attaching a runner.**
2. **`effort` cannot differ per lane.** An `effort` inside a per-session `model`
   override is silently **ignored** — it is agent-configuration only. Reporting
   and repair therefore run at the same effort. Accept it, or split the agent
   again. (`inference_geo` behaves the opposite way and *is* applied per session.)
3. **Never send `tools=[]`.** Clearing tools while `skills` is non-empty returns
   400 — skills need the `read` tool. Overrides replace in full and never merge.
4. **Two datasets, one world — accepted, not fixed.** `pull.json` holds VSOs;
   `jobcards.json` holds Service job cards, and they stay separate mechanisms by
   decision. They are disjoint *today*, which masks the collision — the taxonomy
   already describes VSO, so the moment reporting covers sales the two mounts
   describe the same rows from different snapshots, with no guarantee they agree.
   The path namespace and the Fix 3 rule are the only things keeping them apart;
   do not let the current fixtures stand in for that.
5. **Redeploy coupling doubles.** One agent means one `skills` array: changing
   **either** `SKILL.md`, `phrasebook.py`, or the `xas_allocation` package
   requires re-running `setup_agent.py`. Mounted data (pull, taxonomy, records)
   still does not.
6. **Nothing to clean up in Anthropic — and don't go looking.** The QA agent,
   environment and skill were never created, so the merge orphans no resources.
   The one live set is the allocation agent's, and this plan updates it in place.
   Should orphaned resources ever appear, note that **archive is terminal**:
   no unarchive, and new sessions can never reference an archived resource.
7. **`web_search` / `web_fetch` stay off.** The allocation invariant requires it
   (a web lookup adds state the snapshot doesn't hold). The reports lane loses
   nothing — the cloud environment has no egress anyway.
8. **The QA lane's model pin is stale.** `qa/.env.example` pins
   `claude-sonnet-4-5`. The merged agent runs `claude-opus-5` with `web.py`'s
   existing per-session picker (opus-5 / sonnet-5 / haiku-4.5) — a free upgrade,
   but re-baseline any latency expectation set against the old pin.
9. **The agent is updated, not replaced.** `ALLOC_AGENT_ID` keeps pointing at
   the same agent object, which gains a skill and a new version. Sessions already
   running stay pinned to the version they started on; only new sessions see both
   skills. If a merged session behaves like the old single-skill agent, check the
   version before debugging the prompt.

## Decisions (confirmed)

- **DECIDE-16 (new)** — the reports taxonomy is chosen **by the caller**. One
  committed dictionary ships now; `get_taxonomy(name)` and an optional field on
  `POST /session` are the seam the frontend will use to pick per dealership.
  Resolved 2026-08-18; supersedes IDX-3's leaning toward a host-side per-session
  fetch.
- **Fake data stays split** — `jobcards.json` does not become a `scenario_engine`
  product. The two lanes fabricate data by different mechanisms and that is
  intended. Resolved 2026-08-18.
- **Credentials stay as they are** — the merged agent is the existing allocation
  agent; only `QA_SKILL_ID` is added. Resolved 2026-08-18.

## Verify

```bash
git merge claude/vpo-jobcard-data-sopb7f     # clean; zero overlapping files
uv run python -m scenario_engine.generate
uv run pytest                                 # engine, flatten, contract, phrasebook, agent contract
PYTHONPATH=. uv run python tests/test_invariant.py
uv run ruff format . && uv run ruff check .
uv run python setup_agent.py                  # prints QA_SKILL_ID; updates the agent in place
uv run uvicorn web:app --port 8000            # one run plane, both lanes
```

Then the hand-run routing checklist in `docs/evals/routing.md`.
