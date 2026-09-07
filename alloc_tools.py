"""The allocation agent's data tool: pull a snapshot to repair.

ONE contract, ONE definition. ``PULL_TOOL`` is the ``custom`` tool
``setup_agent.py`` declares on the agent; ``pull_allocation_snapshot``
is the implementation ``web.py`` registers to answer it. Both are derived from
the constants below, so the declaration and the implementation cannot drift into
the failure this arrangement invites: a custom tool call whose name nothing
answers parks the session on a ``requires_action`` idle, which never times out.

**Cloud sandbox.** The tool runs on *our* host; the agent runs in Anthropic's.
Everything this returns crosses into the agent's context.

**Pull ships a file, not rows.** The pull is a scenario directory's two CSVs
(``datasource.get_source()``), translated HOST-SIDE into the ONE document the
"Allocation solve contract" specifies. ``web.py`` mounts it and this tool answers
with the contract's five header fields — ``pull_id``, ``captured_at``, ``source``,
``counts`` and ``file`` — plus a self-locating ``flatten`` command that reads that
file and writes ``snapshot.json``. The rows travel as the mounted file, never
through the context window.

**``file`` is the path the platform gave back, never a constant.** ``web.py``
reads it off the resource the API returns and hands it to `summarize`; guessing it
here is how the sandbox ends up solving against a file that is not there.

The contract fixes those five fields and says nothing about the rest, so the drop
funnel (``excluded`` / ``conflicts``), the late summary and the ``flatten``
command stay beside them. Dropping ``excluded`` would take away the one thing the
turn-1 reply is REQUIRED to say — a plan over 1 of 25 orders that does not mention
the other 24 reads as the whole book — and dropping ``flatten`` would leave the
agent no command to run.

DECIDE-7: the source of the rows (bundled file → app MCP → the export's CSVs) is
the only thing that has ever changed here; the summary + flatten contract is not.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

from anthropic.lib.tools import beta_async_tool


def parse_date(value: str) -> date:
    """'2026-09-20' -> date. Local to this module so the summary needs no import
    from the solver package, which lives on the other side of the mount."""
    return date.fromisoformat(str(value)[:10])


REPO_ROOT = Path(__file__).resolve().parent

SNAPSHOT_FILENAME = "snapshot.json"

# Where web.py ASKS for the pull to be mounted — one file, because the contract is
# one document carrying both row streams. This is the request; the path the
# platform actually reports back is what reaches the agent, and web.py passes that
# through rather than re-deriving it from this constant.
PULL_MOUNT_PATH = "/workspace/dms_allocation.json"

# Where the platform ACTUALLY materializes a mounted resource. Observed
# 2026-08-18: a resource requested at /workspace/pull.json appeared at
# /mnt/session/uploads/workspace/pull.json, and /workspace held only `skills`.
# The docs say mount_path is absolute, so treat this as a location we resolve
# rather than one we assume — a wrong guess fails the pull, and the agent then
# either improvises (copying files around, as it did) or gives up. Both break
# "same snapshot every turn".
UPLOAD_PREFIX = "/mnt/session/uploads"


def mount_candidates(mount_path: str) -> list[str]:
    """Every place a resource mounted at ``mount_path`` might really be, in order.

    Still needed even though the API now reports a ``mount_path``: what it reports
    is the path that was REQUESTED, and the observation below is that the platform
    may materialize it under a prefix. Two explicit candidates, no searching.
    """
    return [mount_path, f"{UPLOAD_PREFIX}{mount_path}"]


TOOL_NAME = "pull_allocation_snapshot"

TOOL_DESCRIPTION = (
    "Pull the current allocation snapshot: sales orders, the pool of planned "
    "vehicles, the complete current allocation, and the disruption to repair. "
    "Returns a summary plus the exact `flatten` command to materialize the full "
    "snapshot as snapshot.json in your sandbox — run that command before solving, "
    "then read the file from your solver code, never into this conversation. Call "
    "once at the start of a repair cycle; the same pull backs every turn, so "
    "re-applying the same combined override is what makes a turn reproducible. "
    "Prototype (DECIDE-7): the real XAS pull does not exist yet, so the rows come "
    "from the scenario-engine fake by default; either way the host mounts them as "
    "a file the flatten command reads."
)

# The pull takes no parameters: the scenario is pre-fabricated and bundled, so
# there is nothing for the agent to tune. (A future multi-scenario build would
# add a 'scenario' selector here.)
PULL_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

# What the agent declares. Kept next to the implementation on purpose.
PULL_TOOL: dict[str, Any] = {
    "type": "custom",
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "input_schema": PULL_TOOL_INPUT_SCHEMA,
}


def flatten_command(pull_path: str = PULL_MOUNT_PATH) -> str:
    """The one-liner that flattens the mounted document inside the sandbox.

    Two kinds of location, both handled explicitly:
      * the **solver package** ships in the skill bundle, landing wherever the
        platform materializes skills — so we self-locate ``xas_allocation/
        flatten.py``. Search bases are **explicit and never** ``/`` (an unbounded
        ``rglob`` from ``/`` once swept the whole container and killed the shell).
      * the **document** is mounted by the host at a path the platform reports
        back, which is what ``pull_path`` carries — but the platform may
        materialize it under ``/mnt/session/uploads``, so it is resolved against
        ``mount_candidates`` rather than assumed. Still no searching: the list is
        short, explicit and bounded.

    The command fails fast, naming the file it could not find — a silent miss
    would let the sandbox solve against no data, which is worse than not solving.
    ``snapshot.json`` is written in the working directory; single line, single
    quoting for a clean paste.
    """
    return (
        'python -c "'
        "import sys, json, pathlib; "
        "root = pathlib.Path('/'); "
        "bases = [p for p in (pathlib.Path.cwd(), pathlib.Path('/workspace')) "
        "if p.is_dir() and p != root]; "
        "hit = next((h for b in bases for h in b.rglob('xas_allocation/flatten.py')), None); "
        "sys.exit('xas_allocation not found under ' + str(bases)) if hit is None else None; "
        "sys.path.insert(0, str(hit.parent.parent)); "
        "from xas_allocation.flatten import flatten_path; "
        # `next(gen, sys.exit(...))` would NOT work: a default argument is
        # evaluated eagerly, so the exit fires before the lookup runs. Pick, then
        # check.
        f"cands = {mount_candidates(pull_path)!r}; "
        "pull = next((n for n in cands if pathlib.Path(n).is_file()), ''); "
        "sys.exit('pull data not mounted at ' + str(cands)) if not pull else None; "
        f"out = pathlib.Path.cwd() / '{SNAPSHOT_FILENAME}'; "
        "json.dump(flatten_path(pull).as_dict(), open(out,'w'), "
        "indent=2, sort_keys=True); "
        "print('wrote ' + str(out))"
        '"'
    )


def summarize(pull: dict, file_path: str = PULL_MOUNT_PATH) -> dict[str, Any]:
    """The part of the pull that crosses into the agent's context.

    The contract's five header fields first — ``pull_id``, ``captured_at``,
    ``source``, ``counts``, ``file`` — then counts, provenance and the flatten
    command. Never rows. What the source filtered OUT is in here on purpose: the
    turn-1 reply has to account for the orders that are not in the plan, and a
    plan over 1 of 25 that doesn't say so reads as the whole book.

    ``file_path`` is the path the platform reported for the mounted resource. It
    is a parameter and not a constant because the contract says so, and because
    the two would drift the day the platform starts resolving mounts differently.
    """
    meta = pull.get("meta", {})
    orders = pull.get("orders", [])
    vehicles = pull.get("vehicles", [])
    late = list((pull.get("disruption") or {}).get("disrupted_orders") or [])

    held = {o["VehicleCode"] for o in orders if o.get("VehicleCode")}
    eta_of = {v["VehicleCode"]: v["AvailableBy"] for v in vehicles}
    by_key = {f"{o['DMSJCNum']}-{o['LineNum']}": o for o in orders}
    # The spread of how late things are. This REPLACED the fake's delay manifest
    # ("30 days on 25 vehicles"): the export records no such thing, so the only
    # honest summary of a disruption is what the dates now say.
    gaps = sorted(
        (parse_date(eta_of[o["VehicleCode"]]) - parse_date(o["DeliveryDate"])).days
        for key in late
        if (o := by_key.get(key)) and o.get("VehicleCode") in eta_of
    )

    return {
        # --- the contract's five ---------------------------------------------
        "pull_id": pull.get("pull_id"),
        "captured_at": pull.get("captured_at"),
        "source": pull.get("source"),
        "counts": {"orders": len(orders), "vehicles": len(vehicles)},
        "file": file_path,
        # --- and what the agent cannot do the turn without --------------------
        "flatten": flatten_command(file_path),
        "snapshot_path": SNAPSHOT_FILENAME,
        "excluded": meta.get("excluded", {}),
        "conflicts": meta.get("conflicts", []),
        "job_cards": len({o["DMSJCNum"] for o in orders}),
        "orders_holding_a_car": len(held),
        "orders_holding_no_car": sum(1 for o in orders if not o.get("VehicleCode")),
        "free_supply": sum(1 for v in vehicles if v["VehicleCode"] not in held),
        "sales_models": meta.get("sales_models", []),
        "late_orders": len(late),
        "late_order_keys": late,
        "days_late": (
            {"min": gaps[0], "median": gaps[len(gaps) // 2], "max": gaps[-1]} if gaps else {}
        ),
    }


# The provider hands back BOTH halves of one session's pull: the rows, and the
# path the platform reported for the file they were mounted as. One callable, so
# the two can never be fetched from different sessions.
RichProvider = Callable[[], tuple[dict, str] | Awaitable[tuple[dict, str]]]


def make_pull_tool(get_rich: RichProvider):
    """Build the pull tool over a per-session data provider.

    ``get_rich`` returns ``(pull, mounted_path)`` for this session (sync or async)
    — web.py closes it over the session's fetched-and-mounted data. The tool
    answers with only the summary + flatten command; the rows never cross the
    transcript.

    The name / description / schema come from the module constants, so the
    declared ``PULL_TOOL`` and this implementation stay ONE contract — a drift
    there is a custom tool nothing answers, which parks the session forever.
    """

    async def pull_allocation_snapshot() -> str:
        rich = get_rich()
        if inspect.isawaitable(rich):
            rich = await rich
        pull, mounted_path = rich
        return json.dumps(summarize(pull, mounted_path), indent=2)

    return beta_async_tool(
        pull_allocation_snapshot,
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        input_schema=PULL_TOOL_INPUT_SCHEMA,
    )


def _default_rich() -> tuple[dict, str]:
    """Host-side default provider: the default scenario directory, mounted where
    we ask for it. Used by the module-level tool for tests and local runs; web.py
    builds per-session tools over the scenario the planner picked and the path the
    platform reported, via ``make_pull_tool``."""
    import datasource

    return datasource.get_source().pull(), PULL_MOUNT_PATH


# A ready-to-use instance over the default source, for host-side tests/local runs.
pull_allocation_snapshot = make_pull_tool(_default_rich)
