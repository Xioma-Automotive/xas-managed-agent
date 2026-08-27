# Planner Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the same allocation table being written into the conversation twice — once as sandbox output the planner cannot see, once retyped by the agent.

**Architecture:** The solver's reports are already written for the planner, but `web.py:_render` drops every built-in tool result, so the agent retypes them. Add a marked span: report text printed inside two sentinel lines is forwarded by `web.py` to the planner's screen and rendered as markdown. The agent then adds judgment in a sentence instead of re-typesetting data. A second, conditional change moves the table out of the agent's context entirely — but only if agent-written files can actually be retrieved, which Task 4 tests.

**Tech Stack:** Python 3.11, pytest, FastAPI (`web.py`), vanilla JS (`static/index.html`), Anthropic Managed Agents.

---

## Why the double copy exists

`web.py:314` `_render` docstring: *"Builtin tool results are dropped — they are sandbox chatter, and the agent's own reply already says what came of them."* Three things reach the planner: `agent.message`, `user.custom_tool_result` (the pull), and file bubbles. `repair_and_report` prints into a channel with no audience, so the agent's retype is the only version anyone reads.

Observed cost in session `sesn_019U2ezB9o9SYizbnDydSUgD`: turn 1 and each of three solves wrote the table twice. Two cache-cold pauses re-bought 105,261 tokens at the 1.25× write rate — $0.61 of a $2.66 session.

**The floor for Tasks 1-3 is one copy**, not zero: bash output enters the agent's context whatever we do. Task 5 reaches zero, and depends on Task 4.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `xas_allocation/planner_channel.py` | The two sentinel constants, `show()`, `planner_span()`. No solver imports. | Create |
| `xas_allocation/session.py` | Re-export `show` so the skill's three-call API stays one import. | Modify (near `PLAN_FILENAME`, ~line 517) |
| `web.py` | Forward marked spans from `agent.tool_result`; keep dropping everything else. | Modify `_render`, 314-340 |
| `static/index.html` | Render a `planner` event as markdown, visually distinct from the agent's own prose. | Modify `render()` 283-303, CSS ~47 |
| `skills/xas-allocation/SKILL.md` | Tell the agent to wrap planner-facing prints and to reply with judgment, not a retyped table. | Modify 177-223 |
| `tests/test_report.py` | Wrapping round-trips; the table survives inside the markers. | Modify |
| `tests/test_agent_contract.py` | `_render` forwards marked results and drops unmarked ones. | Modify |

**Why a separate module rather than putting the constants in `session.py`:** `web.py` needs `planner_span()`, and importing `session.py` would drag `solver.py` — and therefore ortools and the YAML config read — into the web process at import time. The house rule that a contract has exactly one definition (see `alloc_tools.PULL_TOOL`) applies here too, so the constants must not be duplicated. A dependency-free module satisfies both.

**Note:** `planner_channel.py` ships inside the skill bundle automatically (`setup_agent.skill_files` bundles the whole `xas_allocation` package), but adding it still requires re-running `setup_agent.py` — Task 3 Step 5.

---

### Task 1: The marked span

**Files:**
- Create: `xas_allocation/planner_channel.py`
- Test: `tests/test_report.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_report.py`:

```python
def test_show_wraps_text_and_planner_span_takes_it_back_out():
    from xas_allocation.planner_channel import planner_span, show

    wrapped = show("**Done** — 3 of 4 fixed.")
    assert planner_span(wrapped) == "**Done** — 3 of 4 fixed."


def test_planner_span_ignores_output_that_was_never_marked():
    from xas_allocation.planner_channel import planner_span

    assert planner_span("Successfully installed ortools-9.15.6755") is None
    assert planner_span("") is None


def test_planner_span_keeps_the_markdown_table_intact():
    from xas_allocation.planner_channel import planner_span, show

    body = planner_span(show(repair_and_report(_snapshot())))
    assert body is not None
    assert "| Order | Customer | Model |" in body
    assert "<<<" not in body


def test_show_survives_stdout_noise_around_the_span():
    from xas_allocation.planner_channel import planner_span, show

    printed = "WARNING: pip as root\n" + show("the table") + "\nwrote plan.json"
    assert planner_span(printed) == "the table"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_report.py -k "planner_span or show_wraps or show_survives" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xas_allocation.planner_channel'`

- [ ] **Step 3: Write the module**

Create `xas_allocation/planner_channel.py`:

```python
"""The one channel that reaches the planner's screen from inside the sandbox.

`web.py` drops every builtin tool result — sandbox chatter the planner has no
use for — which used to leave the agent retyping the solver's own report as the
only way to show it to anyone. Two copies of one table, both stored in the
conversation for the rest of the session, and every retype a chance to lose a
row or mistype a vehicle id.

So text printed between these two markers is FORWARDED: `web.py` lifts the span
out of the tool result and renders it as markdown beside the conversation. The
markers are emitted by code, never typed by the agent, so a planner-facing
report cannot arrive unmarked because the agent forgot.

NO SOLVER IMPORTS. `web.py` imports this module, and importing `session.py`
instead would pull `solver.py` — ortools and the YAML config read — into the web
process at startup. The constants live in exactly one place for the same reason
`alloc_tools` holds one definition of the tool contract: two spellings of a
marker is a silent channel failure.

Forgetting to wrap is a LOUD failure, not a silent one: the planner sees nothing
at all, rather than seeing something subtly wrong. That is why `show()` is the
agent's to call rather than being baked into each report function, which would
also change what every report test asserts on.
"""

from __future__ import annotations

# Deliberately ugly and deliberately short. Ugly so no report body could contain
# one by accident; short because both markers ride through the transcript on
# every turn.
MARK_OPEN = "<<<PLANNER"
MARK_CLOSE = "PLANNER>>>"


def show(text: str) -> str:
    """Wrap planner-facing prose so `web.py` puts it on the planner's screen.

    Print the result. Everything between the markers reaches the planner; the
    markers themselves are stripped before rendering.
    """
    return f"{MARK_OPEN}\n{text}\n{MARK_CLOSE}"


def planner_span(printed: str) -> str | None:
    """The planner-facing body inside `printed`, or None if it has none.

    None is the normal case and means "drop this": pip output, a `wrote
    snapshot.json` line, a traceback. Only marked text is forwarded.

    Takes the FIRST open marker and the LAST close marker, so a report that
    itself mentions a marker cannot truncate the span, and tolerates stdout noise
    on either side — bash returns one blob per command, not one per print.
    """
    start = printed.find(MARK_OPEN)
    end = printed.rfind(MARK_CLOSE)
    if start == -1 or end == -1 or end < start:
        return None
    body = printed[start + len(MARK_OPEN) : end]
    return body.strip("\n") or None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/ubuntu/xas-managed-agent && uv run pytest tests/test_report.py -k "planner_span or show_wraps or show_survives" -v`
Expected: 4 passed

- [ ] **Step 5: No re-export — the skill imports the channel directly**

**Deviation from the original design, decided during execution.** The plan called
for re-exporting `show` from `session.py` so the skill's snippet stayed one
import. Both ways of doing that lose to this repo's toolchain:

- a plain `from .planner_channel import show` is F401, unused import;
- the standard explicit-re-export idiom `import show as show` is **PLC0414**,
  which is in ruff 0.16.1's default rule set, so the project config cannot opt out
  of it without adding an ignore;
- `__all__ = ["show"]` would silence F401 but misrepresents `session.py` — it has
  ~25 public names — and would break `from session import *`.

So `session.py` is left untouched and the skill takes two imports instead of one:

```python
from xas_allocation import session as S
from xas_allocation.planner_channel import show
```

That is arguably the better shape anyway. `show` is *transport*, not part of the
solver API, and importing it separately says so at the call site.

- [ ] **Step 6: Verify the channel and the whole suite**

Run: `cd /home/ubuntu/xas-managed-agent && uv run python -c "
from xas_allocation import session as S
print(S.show('x'))
" && uv run pytest -q`
Expected: prints the wrapped `x` on three lines, then the full suite passes.

- [ ] **Step 7: Format, lint, commit**

```bash
cd /home/ubuntu/xas-managed-agent
uv run ruff format . && uv run ruff check .
git add xas_allocation/planner_channel.py tests/test_report.py
git commit -m "planner channel: text printed between two markers reaches the planner"
```

---

### Task 2: Forward marked spans to the browser

**Files:**
- Modify: `web.py:314-340` (`_render`)
- Modify: `static/index.html` (CSS ~47, `render()` 283-303)
- Test: `tests/test_agent_contract.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_agent_contract.py`:

```python
# --- The planner channel (web.py forwards marked tool output) -----------------


class _Block:
    """One text content block, shaped like the SDK's."""

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _ToolResult:
    """An `agent.tool_result` event, shaped like the SDK's."""

    def __init__(self, text: str, is_error: bool = False) -> None:
        self.type = "agent.tool_result"
        self.content = [_Block(text)]
        self.is_error = is_error


def test_render_forwards_a_marked_span_to_the_planner():
    from xas_allocation.planner_channel import show

    out = web._render(_ToolResult("noise\n" + show("| Order |\n|---|") + "\ndone"))
    assert out == {"type": "planner", "text": "| Order |\n|---|"}


def test_render_still_drops_unmarked_sandbox_chatter():
    assert web._render(_ToolResult("Successfully installed ortools-9.15.6755")) is None
    assert web._render(_ToolResult("wrote /workspace/snapshot.json")) is None


def test_render_drops_a_marked_span_that_failed():
    """A traceback is not a planner report, even if the span opened before it."""
    from xas_allocation.planner_channel import show

    assert web._render(_ToolResult(show("half a table"), is_error=True)) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ubuntu/xas-managed-agent && uv run pytest tests/test_agent_contract.py -k "planner or marked" -v`
Expected: FAIL — `_render` returns `None` for the first test (no branch for `agent.tool_result`)

- [ ] **Step 3: Add the branch in `web.py`**

At the top of `web.py`, beside the other in-repo imports (`import alloc_tools`, `import datasource`), add:

```python
from xas_allocation import planner_channel
```

Replace the `_render` docstring and add the new branch. The docstring currently reads *"Builtin tool results are dropped — they are sandbox chatter, and the agent's own reply already says what came of them."* — that assumption is what forced the second copy, so it goes:

```python
def _render(event) -> dict | None:
    """One session event -> what the browser shows, or None to drop it.

    Builtin tool results are dropped as sandbox chatter EXCEPT for a marked
    planner span (`xas_allocation.planner_channel`): the solver's reports are
    already written for the planner, and forwarding them is what stops the agent
    retyping every table into its own reply. A failed result is dropped whatever
    it contains — a traceback with an open marker in front of it is not a report.

    The *custom* tool's result is kept: it is answered by this process, so the
    transcript is the only place a planner can see what the pull returned.
    """
    kind = event.type
    if kind == "agent.message":
        text = "".join(b.text for b in event.content if b.type == "text")
        return {"type": "agent", "text": text}
    if kind == "agent.tool_result":
        if getattr(event, "is_error", False):
            return None
        body = "".join(b.text for b in event.content if getattr(b, "type", None) == "text")
        span = planner_channel.planner_span(body)
        return {"type": "planner", "text": span} if span else None
```

Leave every other branch in `_render` exactly as it is.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/ubuntu/xas-managed-agent && uv run pytest tests/test_agent_contract.py -k "planner or marked" -v`
Expected: 3 passed

- [ ] **Step 5: Render it in the browser**

In `static/index.html`, beside the `.msg.tool_result` rules (~line 47), add:

```css
  /* A report the SOLVER wrote, forwarded from the sandbox. Distinct from the
     agent's own prose: this text is generated, not composed. */
  .msg.planner { border-left: 3px solid var(--warn); padding-left: 12px; }
```

In `render()`, add a case beside `case "agent":`:

```js
    case "planner": return addAgent(event.text, "planner");
```

And widen `addAgent` to take the extra class (it currently hardcodes `"msg agent md"`):

```js
function addAgent(text, extra = "") {
  const div = document.createElement("div");
  div.className = "msg agent md" + (extra ? " " + extra : "");
  div.innerHTML = renderMarkdown(text);
  $("transcript").append(div);
  $("transcript").scrollTop = $("transcript").scrollHeight;
  runMermaid(div);
}
```

- [ ] **Step 6: Verify the whole suite and lint**

Run: `cd /home/ubuntu/xas-managed-agent && uv run pytest -q && uv run ruff format . && uv run ruff check .`
Expected: all pass, no lint findings.

- [ ] **Step 7: Commit**

```bash
cd /home/ubuntu/xas-managed-agent
git add web.py static/index.html tests/test_agent_contract.py
git commit -m "web: forward the solver's marked planner reports to the browser"
```

---

### Task 3: Tell the agent to explain, not retype

**Files:**
- Modify: `skills/xas-allocation/SKILL.md` (the `## Each turn` block, 177-223; the `## Talking to the planner` section, 224-252)
- Test: `tests/test_agent_contract.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_agent_contract.py`:

Locate the skill the way every other test in this file does — through
`setup_agent.ALLOC_SKILL_DIR` (see lines 250-251) — so the test does not depend
on pytest's working directory:

```python
def test_the_skill_tells_the_agent_to_wrap_planner_prints():
    body = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "show(S." in body, "the skill must show the agent how to reach the planner"


def test_the_skill_forbids_retyping_a_table_the_planner_has_seen():
    """The double-copy rule. Prose is the whole mechanism, so pin the prose."""
    lowered = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").lower()
    assert "already seen" in lowered
    assert "do not repeat the table" in lowered
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ubuntu/xas-managed-agent && uv run pytest tests/test_agent_contract.py -k "wrap_planner_prints or forbids_retyping" -v`
Expected: FAIL — both assertions

- [ ] **Step 3: Update the code block in `## Each turn`**

Replace the snippet at `skills/xas-allocation/SKILL.md:182-194` with:

```python
import json, sys, pathlib

sys.path.insert(
    0, str(next(pathlib.Path("/workspace").rglob("xas_allocation/session.py")).parent.parent)
)
from xas_allocation import session as S

snap = S.Snapshot.from_dict(json.load(open("snapshot.json")))
print(S.show(S.discrepancy_report(snap)))  # where things stand
print(S.show(S.repair_and_report(snap, override)))  # solve + write plan.json + the reply
S.bump_candidates(snap, S.solve(snap, override), override)  # who could be displaced
```

Then replace numbered points 2 and 3 (lines 198-204) with:

```markdown
2. Print `discrepancy_report` **inside `show(...)`** — what the data could not
   use, then the orders whose car now arrives past the promise. **Show this
   before solving anything.**
3. If they asked for a repair: **ask what matters first** — priorities,
   anything to leave alone, anything else that should hold — and wait for the
   answer. Then update the override and print `repair_and_report`, again inside
   `show(...)`. It solves, self-checks, **writes every allocation to
   `plan.json`**, and returns the finished reply.

**`show(...)` is the only thing the planner sees.** Anything else you print
stays in the sandbox. So wrap every report meant for them, and nothing else — a
`pip install` line or a stack trace inside `show` is noise on their screen.
```

- [ ] **Step 4: Add the no-retyping rule to `## Talking to the planner`**

Immediately after the `**The allocation changes.**` paragraph (~line 238-241), insert:

```markdown
**The planner has ALREADY SEEN what you printed with `show(...)`.** So **do not
repeat the table** in your own reply — not reformatted, not summarised row by
row, not "to confirm". Your reply is the part the report cannot write: which
customer this hurts, what you would do next, what you need from them, the one
thing worth noticing. One or two short paragraphs.

Two reasons, and the second is the important one. A retyped table is a table
that can lose a row or change a vehicle id by one character, and nothing checks
it. And the planner reading the same numbers twice, in two shapes, cannot tell
which is authoritative — the printed one always is.
```

- [ ] **Step 5: Run the tests, then re-deploy the skill**

Run: `cd /home/ubuntu/xas-managed-agent && uv run pytest -q`
Expected: full suite passes.

The bundle carries the new `planner_channel.py` and the edited `SKILL.md`, so the
agent runs neither until setup is re-run:

Run: `cd /home/ubuntu/xas-managed-agent && uv run python setup_agent.py`
Expected: reports the skill version created and the agent updated, no error.

- [ ] **Step 6: Commit**

```bash
cd /home/ubuntu/xas-managed-agent
git add skills/xas-allocation/SKILL.md tests/test_agent_contract.py
git commit -m "skill: print planner reports through S.show, and stop retyping them"
```

- [ ] **Step 7: Confirm it end to end by hand**

Start `web.py`, open a session on `scenario-mixed`, and ask *"what's the
situation"*. Check three things:

1. The late-orders table appears on screen with a left accent bar (the forwarded
   report), **once**.
2. The agent's own bubble underneath is prose, with no table in it.
3. `pip install` output and `wrote snapshot.json` do **not** appear.

Then ask for a repair and check the same three things for the plan tables.

---

### Task 4: Find out whether an agent-written file can be retrieved

**Files:**
- Create: `docs/planner-channel-file-probe.md` (the answer, whichever way it goes)

This task is a measurement, not a change. It decides whether Task 5 exists.
`static/index.html:showNewOutputs()` and `web.py:/session/{id}/files` are both
written on the assumption that files the agent writes appear in
`files.list(scope_id=session_id)`. In session
`sesn_019U2ezB9o9SYizbnDydSUgD` that listing returned only the two mounted
inputs — no `plan.json`, no `snapshot.json` — and the mounts themselves refused
to download (`file_not_downloadable`: *"Only files generated by a tool ... can be
downloaded"*). So either the agent must upload a file explicitly, or that whole
path is dead code today.

- [ ] **Step 1: Run a session that writes one file**

```bash
cd /home/ubuntu/xas-managed-agent
uv run uvicorn web:app --port 8000
```

Open the UI, create a session on `scenario-mixed`, and send:

> Write a file /workspace/probe.md containing the single line "hello", then tell me you did.

- [ ] **Step 2: List the session's files while it is still alive**

With the session id from the UI:

```bash
cd /home/ubuntu/xas-managed-agent
curl -s localhost:8000/session/<SESSION_ID>/files | python3 -m json.tool
```

Expected, if the channel works: a row whose `filename` ends `probe.md`.
Expected, if it does not: `{"files": []}`.

- [ ] **Step 3: If a row appears, confirm the content is actually readable**

```bash
curl -s localhost:8000/session/<SESSION_ID>/files/<FILE_ID>/content
```

Expected: `hello`. A `400 file_not_downloadable` here means the row exists but
the channel still does not work — that counts as a failure for Task 5.

- [ ] **Step 4: Write down the answer**

Create `docs/planner-channel-file-probe.md` recording: the date, the session id,
the exact listing output, whether the content downloaded, and the verdict in one
sentence. If it failed, add one line saying that `showNewOutputs()` and the
`/files` routes are dead code pending a fix, so the next person does not build on
them.

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/xas-managed-agent
git add docs/planner-channel-file-probe.md
git commit -m "docs: whether an agent-written file is retrievable host-side"
```

- [ ] **Step 6: Decide**

- **Row appears AND content downloads** → do Task 5.
- **Anything else** → **stop here.** Tasks 1-3 already removed the second copy.
  Task 5 without a working file channel would only move the table from stdout to
  a file nobody can read, and `plan_summary` earns nothing while the table is
  still in the agent's context: the agent would hold both.

---

### Task 5: Take the table out of the agent's context (CONDITIONAL on Task 4)

**Do not start this task unless Task 4 Step 6 said to.**

**Files:**
- Modify: `xas_allocation/session.py` (`repair_and_report`, 605-620; new `plan_summary` above it)
- Modify: `skills/xas-allocation/SKILL.md` (the `## Each turn` snippet)
- Test: `tests/test_report.py`

With a working file channel the table need not pass through the agent at all:
`repair_and_report` writes it to `plan.md`, the browser renders that file, and the
agent gets back only a summary. The table crosses the conversation zero times
instead of once.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_report.py`:

```python
def test_plan_summary_is_short_and_carries_what_a_reply_needs():
    from xas_allocation.session import plan_summary

    snap = _snapshot()
    cyc = run_cycle(snap, {})
    summary = plan_summary(snap, cyc.chosen, {})

    # Short enough that holding it costs nothing next to a table.
    assert len(summary.splitlines()) <= 8
    # Everything the agent needs to write a sentence of judgment.
    assert "moved " in summary
    assert "still late" in summary
    assert "checks passed" in summary
    # And none of the table.
    assert "| Order |" not in summary


def test_repair_and_report_writes_the_table_and_returns_only_the_summary(tmp_path):
    from xas_allocation.session import repair_and_report

    plan_json = tmp_path / "plan.json"
    plan_md = tmp_path / "plan.md"
    returned = repair_and_report(_snapshot(), {}, plan_path=plan_json, report_path=plan_md)

    assert "| Order | Customer | Model |" in plan_md.read_text(encoding="utf-8")
    assert "| Order |" not in returned
    assert "still late" in returned
    assert json.loads(plan_json.read_text(encoding="utf-8"))["allocations"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ubuntu/xas-managed-agent && uv run pytest tests/test_report.py -k "plan_summary or returns_only_the_summary" -v`
Expected: FAIL — `ImportError: cannot import name 'plan_summary'`

- [ ] **Step 3: Add `plan_summary`**

In `xas_allocation/session.py`, directly above `def repair_and_report(`:

```python
def plan_summary(snapshot: Snapshot, result: SolveResult, override: dict | None = None) -> str:
    """What the AGENT needs to know about a solve — a few lines, not the table.

    The planner's table goes to `report_path` and is rendered beside the
    conversation, so it never enters the agent's context. This is what replaces
    it: the counts, the exceptions and the self-check. Enough to write a sentence
    of judgment, and enough that a follow-up ("which are still late?") is
    answered without a script over `plan.json`.
    """
    rows = plan_rows(snapshot, result, override)
    late = [(r["order"], r["customer"], r["days_late"]) for r in rows if r["days_late"]]
    bumped = [r["order"] for r in rows if r["bumped"]]
    lines = [
        f"orders {len(rows)}; moved {sum(1 for r in rows if r['status'] == 'moved')}; "
        f"unchanged {sum(1 for r in rows if r['status'] == 'unchanged')}; "
        f"no car {len(result.unfilled)}",
        "still late: " + (", ".join(f"{o} ({c}) {d}d" for o, c, d in late) if late else "none"),
        "bumped: " + (", ".join(bumped) if bumped else "nobody"),
        f"churn price {result.churn_price}",
        "checks passed" if result.self_check["ok"] else f"CHECKS FAILED: {result.self_check}",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Rewrite `repair_and_report`**

Replace the whole of `repair_and_report` (605-620) with:

```python
def repair_and_report(
    snapshot: Snapshot,
    override: dict | None = None,
    plan_path: str | Path = PLAN_FILENAME,
    report_path: str | Path = REPORT_FILENAME,
) -> str:
    """Solve, write BOTH records, and return only what the agent needs.

    Three outputs on purpose. ``plan.json`` is the data record — every allocation
    this turn produced, and the answer to any follow-up. ``plan.md`` is the
    planner's finished reply, rendered beside the conversation from the file, so
    the table never enters the agent's context and cannot be retyped with a row
    missing. The RETURN VALUE is the short summary: the agent adds judgment to
    that, it does not re-present the numbers.
    """
    cyc = run_cycle(snapshot, override)
    save_plan(snapshot, cyc.chosen, override, plan_path)
    Path(report_path).write_text(planner_report(snapshot, cyc.chosen, override), encoding="utf-8")
    return plan_summary(snapshot, cyc.chosen, override)
```

And beside `PLAN_FILENAME` (~line 522) add:

```python
# The planner's finished reply, written rather than printed: the browser renders
# it from the session's files, so the table never crosses the conversation.
REPORT_FILENAME = "plan.md"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /home/ubuntu/xas-managed-agent && uv run pytest tests/test_report.py -k "plan_summary or returns_only_the_summary" -v`
Expected: 2 passed

- [ ] **Step 6: Fix the tests that assumed a report came back**

`tests/test_report.py` asserts on `repair_and_report`'s return value at lines
121, 157, 167, 172. Each must now read `plan.md`. Change the pattern from:

```python
    report = repair_and_report(_snapshot())
```

to:

```python
    out = tmp_path / "plan.md"
    repair_and_report(_snapshot(), plan_path=tmp_path / "plan.json", report_path=out)
    report = out.read_text(encoding="utf-8")
```

adding `tmp_path` to each test's signature. Do **not** relax any assertion — the
same text must still be in the file, including the JARGON checks at line 355.

Run: `cd /home/ubuntu/xas-managed-agent && uv run pytest tests/test_report.py -q`
Expected: all pass.

- [ ] **Step 7: Update the skill**

In `skills/xas-allocation/SKILL.md`, change the `repair_and_report` line of the
snippet to:

```python
print(S.repair_and_report(snap, override))  # writes plan.json + plan.md, returns the summary
```

Note there is deliberately **no** `show(...)` around it: it returns a summary
for you, not prose for the planner. Then replace the `**The allocations live in
plan.json**` paragraph's opening with:

```markdown
**`repair_and_report` writes the planner's reply to `plan.md` and shows it to
them itself.** What it returns is a short summary for YOU — counts, who is still
late, who was bumped, whether the checks passed. Read the plan from `plan.json`
if you need a specific row. Never re-present the table: the planner is already
looking at it.
```

Keep `show(...)` on `discrepancy_report` — that one has no file and still needs
the marked channel.

- [ ] **Step 8: Full verification, re-deploy, commit**

```bash
cd /home/ubuntu/xas-managed-agent
uv run pytest -q
PYTHONPATH=. uv run python tests/test_invariant.py
uv run ruff format . && uv run ruff check .
uv run python setup_agent.py
git add xas_allocation/session.py skills/xas-allocation/SKILL.md tests/test_report.py
git commit -m "repair_and_report: table to plan.md, summary to the agent"
```

Expected: suite passes, invariant 4/4, no lint findings, setup reports the agent updated.

- [ ] **Step 9: Confirm by hand**

Run a repair in the UI. The plan table must appear **once**, as a file bubble.
The agent's bubble must be prose only. Nothing in the conversation should contain
a second copy of the table.

---

## Self-review

**Spec coverage.** Fix 1 (show the write-up, stop the retype) = Tasks 1, 2, 3.
Fix 2 (agent gets a summary) = Task 5, gated by Task 4. Both covered.

**Deliberately out of scope,** because each is its own change and mixing them
would make this plan untestable:
- the ambiguous `Car pool: …` sentence in `exclusion_note` (`session.py:169`)
- `book_report()`, the missing before-and-after table
- reporting when the solver broke a tie arbitrarily
- `_render` currently labels the pull result "pull result (N chars)" in the UI;
  left alone.

**One risk worth stating.** Tasks 1-3 are prose-enforced on the agent's side:
nothing can force it to stop retyping a table, exactly as nothing forces it to
ask what matters first. The two new tests pin that the *instruction* is present,
not that it is followed — Task 3 Step 7 is the only check of the behaviour, and
it is manual. That matches how the rest of this skill's rules are held.

## Unresolved questions

1. **Can an agent-written file be retrieved at all?** Task 4 answers it. If not,
   Task 5 does not happen and the table keeps crossing the conversation once.
2. **Is `planner_report`'s prose good enough to stand alone?** It has only ever
   been read after an agent rewrote it. Task 3 Step 7 is the first time a planner
   sees it raw; if it reads as machine output, the fix is in the renderer's
   wording, not in this plumbing.
3. **Should `bump_candidates` go through `show` too?** It is planner-facing —
   the list they answer with — but it is also the agent's own working list. Left
   unwrapped for now; revisit once the channel has been used in anger.
4. **Does the summary want the tie flag?** When two orders could equally take a
   car, `plan_summary` is the natural place to say so. Not added: the tie finding
   is unresolved and guessing its shape here would be speculative.
