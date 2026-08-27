"""The one channel that reaches the planner's screen from inside the sandbox.

`web.py` drops every builtin tool result — sandbox chatter the planner has no use
for — which used to leave the agent retyping the solver's own report as the only
way to show it to anyone. Two copies of one table, both stored in the
conversation for the rest of the session, and every retype a chance to lose a row
or mistype a vehicle id.

So text printed between these two markers is FORWARDED: `web.py` lifts the span
out of the tool result and renders it as markdown beside the conversation. The
markers are emitted by code, never typed by the agent, so a planner-facing report
cannot arrive unmarked because the agent forgot to add them.

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
    """The planner-facing body inside ``printed``, or None if it has none.

    None is the normal case and means "drop this": pip output, a `wrote
    snapshot.json` line, a traceback. Only marked text is forwarded.

    Takes the FIRST open marker and the LAST close marker, so a report that itself
    mentions a marker cannot truncate the span, and tolerates stdout noise on
    either side — bash returns one blob per command, not one per print.
    """
    start = printed.find(MARK_OPEN)
    end = printed.rfind(MARK_CLOSE)
    if start == -1 or end == -1 or end < start:
        return None
    body = printed[start + len(MARK_OPEN) : end]
    return body.strip("\n") or None
