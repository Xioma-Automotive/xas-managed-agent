# Callable pull endpoint — Implementation Plan

> Replace the skill-bundled `data/pull.json` with a **callable data source** the
> host resolves at session start, so the rows come from an endpoint (real XAS, or
> the scenario engine as a fake) instead of shipping frozen inside the agent's
> skill. NO code until approved.

**Why:** today the fabricated dataset is baked into the skill bundle
(`setup_allocation_agent.skill_files()` appends `data/pull.json`) and
`flatten_default()` reads it from the sandbox. That means the data is static per
deploy, lives with the agent, and can't come from a live system. DECIDE-7 already
names this the seam: *"when the real XAS pull exists this reads it instead of a
bundled file; the summary + flatten contract stays, only the source of the rows
changes."* This plan builds that seam.

---

## The one principle

**The endpoint is called host-side (`web.py`), never in the sandbox.** The
sandbox keeps `networking: limited`, no egress, no credentials — the whole reason
this branch exists. Only `web.py` (which already holds the org key and answers the
custom tool out of the sandbox's sight) talks to XAS. The sandbox receives an
already-fetched, already-scoped file. **No change to the environment egress
policy** — the security boundary is preserved by construction.

## The transport (SDK-confirmed)

`sessions.create(resources=[...])` accepts a file resource:

```python
{"type": "file", "file_id": "<id>", "mount_path": "/workspace/pull.json"}
```

`files.upload(file=(name, bytes, "application/json"))` → `file_id`; the session
mounts it into the sandbox filesystem. This is the reverse of what `web.py`
already does with `files.list/download(scope_id=session_id)` — same session file
store, host writing instead of reading. **The rows travel as a mounted file, not a
tool result, so nothing hits the transcript** (the property the current
bundle-in-skill trick preserves).

## The endpoint contract (the rich pull shape)

The DataSource returns exactly what `flatten()` and `alloc_tools.summarize()`
already consume — the shape `scenario_engine.generate()["pull"]` emits:

```
{ meta: {now, sales_models, seed?, …},
  pos:  [...],                       # PO → PDN header rows
  sos:  [{customer, customer_id, rows:[{…, current_supply_id}], …}],
  supply: [{vehicle_id|slot ref, kind, sales_model, planned_delivery_date,
            location_state, po_ref, pdn}],
  disruption: {po, delay_days, delayed_supply[], disrupted_orders[]} }
```

This contract is the deliverable of DECIDE-7: prod fills it from XAS, tests fill
it from the scenario engine, `flatten` never knows the difference.

---

## Fix 1 — the `DataSource` seam (host-side only)

New **host-side** module `datasource.py` (NOT inside `xas_allocation/` — HTTP and
credentials must stay out of the package that ships to the sandbox, same rule that
keeps `scenario_engine/`'s code out):

- `class DataSource(Protocol): def pull(self, scope: dict | None) -> dict` —
  returns the rich pull dict above.
- `ScenarioEngineSource` — wraps the fabricator. Default reads the committed
  `data/pull.json` (stable, offline, byte-for-byte with the tests); an optional
  `regenerate=True` calls `scenario_engine.generate(**cfg)["pull"]`. This is the
  **fake**, and the working default until XAS exists.
- `XASApiSource` — `httpx` client against `XAS_API_BASE` with `XAS_API_TOKEN`,
  mapping the XAS response into the contract. **Stubbed** (`raise
  NotImplementedError` with the field-mapping documented in the docstring) because
  the real endpoint does not exist yet (DECIDE-7). The mapping function is pure and
  unit-testable against a captured sample the day a sample exists.
- `get_source() -> DataSource` — picks by env `XAS_DATA_SOURCE` (`scenario` |
  `xas`, default `scenario`).

## Fix 2 — `web.py`: pull → upload → mount at session start

In `new_session` (only there — see gotcha 1):

1. `rich = get_source().pull(scope=None)` — host-side, credentialed.
2. `meta = await client.beta.files.upload(file=("pull.json",
   json.dumps(rich).encode(), "application/json"), betas=[MANAGED_AGENTS_BETA])`.
3. Pass `resources=[{"type": "file", "file_id": meta.id, "mount_path":
   MOUNT_PATH}]` to `sessions.create(...)`.
4. Stash `rich` in a per-session map (`_pull_by_session[sid] = rich`) so the tool
   answerer can `summarize()` it without re-reading — one fetch, one source of
   truth for both the mount and the summary.

`activate` (resuming an existing session) does **not** re-pull — that session's
file was mounted at its own create time and is part of its config; re-pulling would
swap the snapshot mid-cycle and break the invariant. On process restart the
`_pull_by_session` cache is cold; the tool answerer falls back to
`files.download` of the session's mounted pull to rebuild the summary (gotcha 2).

## Fix 3 — `alloc_tools.py`: tool reads the injected pull, not a bundled file

- Keep ONE contract (name / description / schema / `summarize` — the
  `test_tool_contract` guard). Replace the standalone `@beta_async_tool` default
  that calls `load_dataset()` with a **factory** `make_pull_tool(get_rich:
  Callable[[], dict])` that closes over the per-session data and still stamps
  `TOOL_NAME` / `PULL_TOOL_INPUT_SCHEMA`. `web.py`'s `_answer_custom_tools` builds
  the tool per session: `make_pull_tool(lambda: _pull_for(session_id))`.
- `flatten_command(pull_path=MOUNT_PATH)` now emits a one-liner that locates the
  `xas_allocation` package (unchanged self-locating search, still bounded to
  `cwd`/`/workspace`, never `/`) **and reads the rich data from the known
  `MOUNT_PATH`** — instead of `flatten_default()` finding a bundled copy.
- Drop `DATASET_PATH` / `load_dataset()` from the default path (keep a thin
  host-side loader only for the scenario fake if useful).

## Fix 4 — `flatten.py`: read the mounted path

- Add `flatten_path(src) -> Snapshot` (thin: `flatten(load_rich(src))`).
- `flatten_default()` stays for host-side tests/dev, still pointing at the repo
  `data/pull.json` (the fake's committed output) so the suite runs offline.
- The in-sandbox command calls `flatten_path(MOUNT_PATH)`.

## Fix 5 — `setup_allocation_agent.py`: stop bundling the data

- `skill_files()` drops the `data/pull.json` append; the skill carries only
  SKILL.md + the solver package. Re-run setup to redeploy the slimmer skill.
- `data/pull.json` **stays in the repo** as the fake's committed output (tests +
  offline dev). It just no longer ships in the skill.

## Config / env (`.env.example`)

```
XAS_DATA_SOURCE=scenario        # scenario | xas
XAS_API_BASE=                   # e.g. https://xas.internal/api  (xas source only)
XAS_API_TOKEN=                  # host-side credential; the sandbox never sees it
```

`MOUNT_PATH = "/workspace/pull.json"` as a shared constant (alloc_tools + web.py).

---

## Files touched

| File | Change |
| --- | --- |
| `datasource.py` | **new, host-side** — `DataSource` protocol, `ScenarioEngineSource` (fake, default), `XASApiSource` (stub + documented mapping), `get_source()` |
| `web.py` | `new_session`: pull → `files.upload` → `resources=[file mount]` on `sessions.create`; per-session `_pull_by_session` cache; `_answer_custom_tools` builds the tool via the factory; download-fallback after restart |
| `alloc_tools.py` | `make_pull_tool(get_rich)` factory; `flatten_command(pull_path)` reads `MOUNT_PATH`; retire `load_dataset` from the default path; shared `MOUNT_PATH` |
| `xas_allocation/flatten.py` | add `flatten_path(src)`; `flatten_default()` unchanged (host tests) |
| `setup_allocation_agent.py` | `skill_files()` stops appending `data/pull.json` |
| `.env.example` | `XAS_DATA_SOURCE`, `XAS_API_BASE`, `XAS_API_TOKEN` |
| `tests/test_datasource.py` | **new** — `ScenarioEngineSource.pull()` returns a flatten-able contract; `XASApiSource` mapping is pure (once a sample exists), stub raises cleanly |
| `tests/test_tool_contract.py` | tool built via factory with an in-memory pull; contract (name/schema) still matches `PULL_TOOL`; `flatten_command` targets `MOUNT_PATH` and stays bounded |
| `CLAUDE.md`, `README.md`, `SKILL.md` | data no longer bundled in the skill; DECIDE-7 now "callable source, host-side"; the pull-transport note updated |
| `xas_allocation/decisions.py` | DECIDE-7 default → "callable `DataSource`; scenario fake vs XAS by config; call is host-side" |

## Gotchas (surfaced from the code / SDK)

1. **Pull once per cycle, at create — not on activate.** The invariant is "the
   same snapshot backs every turn of a repair cycle." Re-pulling on `activate`
   (resume) would swap data mid-cycle. New session = new pull; resume = same file.
2. **Cold cache after a `web.py` restart.** `_pull_by_session` is in-process. If
   `web.py` restarts while a session lives, the tool answerer must rebuild the
   summary from the mounted file via `files.list(scope_id)/download` rather than a
   stale/empty cache. (The mounted file is the durable copy; the cache is a
   convenience.)
3. **Session-resource persistence across sandbox reclaim.** A cloud sandbox is
   ephemeral; confirm the platform **re-mounts** create-time file resources when a
   reclaimed session resumes. If it does not, a resumed session must re-mount
   (via `sessions.update` if supported, or by treating resume-after-reclaim as a
   fresh pull of the *same* data — deterministic because the source is stable).
   **Verify before relying on it.**
4. **`test_tool_contract` guards the single definition.** The factory must keep
   deriving name/description/schema from the same constants, or the "one contract,
   one definition" invariant (and its test) breaks — the failure mode is a custom
   tool nothing answers, which parks the session on a never-timing-out idle.
5. **Transcript hygiene must survive.** The whole point is that rows don't cross
   the context window. Do NOT let the tool return rows inline as a shortcut — the
   summary + `flatten` command shape is load-bearing at real dealer scale.
6. **Determinism unchanged.** `plan = f(data_snapshot, skill, override)` holds:
   the snapshot's *source* moved (bundled file → mounted file from a callable), its
   *role* did not. `flatten` stays pure; re-pull + same override → same plan. The
   fake source is stable (committed `data/pull.json`), so the invariant test needs
   no rework.
7. **Host reaches XAS, sandbox does not.** The `httpx` call is `web.py`'s own
   network. `create_environment()`'s `allowed_hosts: []` for the sandbox stays
   empty. Don't "fix" a connection error by opening sandbox egress.

## Non-goals / deferred

- **Pull parameters (scope).** The tool takes no input today. A real pull likely
  accepts a fetch scope (customers / month / POs) so it isn't fetching the whole
  book. Note the two distinct scopes — *fetch* scope (what XAS returns) vs the
  existing *override* scope (what's re-allocatable within it). Add a `scope` input
  to `PULL_TOOL` in a follow-up; out of scope here.
- **In-cycle re-pull** via `sessions.update` with a fresh file — deferred; new
  session is the re-pull path.
- **Durable override store (DECIDE-5).** The SDK's `memory_store` session resource
  is the natural host-side home for the combined override later — adjacent, not
  this plan.
- **Write-back to XAS** — still approval-gated and out of scope.

## Verify

`uv run pytest` (datasource + contract + invariant + report all green,
**offline**) · `PYTHONPATH=. uv run python tests/test_invariant.py` ·
`uv run ruff format . && uv run ruff check .` · with `XAS_DATA_SOURCE=scenario`,
run `web.py` locally and confirm a new session mounts `/workspace/pull.json`, the
pull tool returns the summary + flatten command, and the agent flattens the
mounted file (no rows in the transcript). Redeploy: re-run
`setup_allocation_agent.py` (slimmer skill, no bundled data).
```
