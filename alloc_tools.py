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


# --- The write-back: stage a plan for the planner to review -------------------
#
# Same one-definition rule as the pull: ``PUSH_TOOL`` is what the agent declares,
# ``make_push_tool`` is what answers it, both built from the constants below.
#
# STAGE IS A HOLD, and that is why this can be answered honestly here. The
# contract has no ``apply`` value, so nothing this tool does reaches the DMS: it
# checks the plan against the snapshot the session actually holds and records it
# for a human to approve. The approval is the planner's, in the app.
#
# THE PLAN TRAVELS IN THE CALL, NOT AS A PATH — and that is a platform fact, not
# a preference. The contract asked for ``plan_file``, but a file the agent writes
# in the sandbox is indexed into the session's file store only ~1-3s AFTER the
# session goes idle, and a custom tool is answered MID-TURN: `web.py` even sets
# `check_files: False` for a `requires_action` idle for exactly this reason. So
# the host's listing cannot yet see the plan the agent just wrote — tried live on
# 2026-09-09, `no file named plan.json`, and the agent then went hunting the
# filesystem for it. Only the CHANGED rows are sent: an unchanged line is
# `skipped` anyway, so the payload is the part staging acts on and is far smaller
# than the file (4 rows of 10 in the live trace).

PUSH_TOOL_NAME = "push_allocation_plan"

PUSH_MODE = "stage"

# Contract only. The procedure — copy the rows and counts out of plan.json, ask
# the planner first, what to do with a refusal — is in the allocation skill,
# which is read once a session; a tool description is paid on every turn of every
# session, reporting turns included.
PUSH_TOOL_DESCRIPTION = (
    "Hold the plan you just saved for the planner to approve in the app. Send the "
    "changed rows of plan.json and its counts block. Nothing is applied from "
    "here, and there is no mode that does. See the allocation skill first."
)

PUSH_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "description": "Every row of plan.json whose status is not 'unchanged'.",
            "items": {
                "type": "object",
                "properties": {
                    "order": {"type": "string"},
                    "was_car": {"type": ["string", "null"]},
                    "now_car": {"type": ["string", "null"]},
                    "status": {"type": "string", "enum": ["moved", "no_car"]},
                    "bumped": {"type": "boolean"},
                },
                "required": ["order", "was_car", "now_car", "status"],
                "additionalProperties": False,
            },
        },
        "pull_id": {
            "type": "string",
            "description": "pull_id of the snapshot this plan was built on.",
        },
        "source": {"type": "string", "description": "source of that same snapshot."},
        "mode": {
            "type": "string",
            "enum": [PUSH_MODE],
            "description": "Only 'stage' exists. Nothing here applies a plan.",
        },
        "summary": {
            "type": "object",
            "description": "The counts block from plan.json, copied.",
            "properties": {
                k: {"type": "integer"} for k in ("orders", "moved", "bumped", "unchanged", "no_car")
            },
            "required": ["orders", "moved", "bumped", "unchanged", "no_car"],
            "additionalProperties": False,
        },
        "note": {
            "type": "string",
            "description": "One line for the planner's pending bar: why this plan.",
        },
    },
    "required": ["changes", "pull_id", "source", "mode", "summary", "note"],
    "additionalProperties": False,
}

PUSH_TOOL: dict[str, Any] = {
    "type": "custom",
    "name": PUSH_TOOL_NAME,
    "description": PUSH_TOOL_DESCRIPTION,
    "input_schema": PUSH_TOOL_INPUT_SCHEMA,
}

# The three row statuses that partition the book. `bumped` is a subset of
# `moved`, so it is not one of them.
PARTITION_KEYS = ("moved", "unchanged", "no_car")


class PlanRefused(Exception):
    """A staged plan that does not match the session's snapshot. Refusing is the
    whole job: every check here exists because the alternative is staging a plan
    built on a pull that is not this one."""


def check_plan(
    changes: list[dict], pull: dict, mode: str, pull_id: str, source: str, summary: dict
) -> dict:
    """Validate one plan against the snapshot the session holds, and say what
    would stage. Pure: no I/O, so the rules are testable without a session.

    Four refusals, in the order they cost least to check, and every one of them
    is "this is not the plan for this snapshot":

    * ``mode`` is anything but ``stage`` — the enum is the guardrail, so a value
      outside it is a caller that thinks it can apply.
    * ``pull_id`` / ``source`` differ from the pull — the plan was solved against
      another snapshot or another tenant, and re-applying an override to a
      different pull is not the same turn.
    * ``summary`` disagrees with the rows sent — ``moved`` and ``bumped`` are
      counted here from ``changes`` and must match what the file said, and
      ``moved`` + ``unchanged`` + ``no_car`` must add up to the book. A summary
      copied from a different plan than the rows is the wrong plan.

    Then per row: a row whose ``was_car`` is not the car the snapshot has that
    line holding is STALE and does not stage. Within one session that can only
    happen if the rows were not built from this pull; against a live workspace it
    is the ordinary race, which is why the contract has the field.
    """
    if mode != PUSH_MODE:
        raise PlanRefused(f"mode must be {PUSH_MODE!r}, not {mode!r}: nothing here applies a plan")
    for field, theirs, ours in (
        ("pull_id", pull_id, pull.get("pull_id")),
        ("source", source, pull.get("source")),
    ):
        if theirs != ours:
            raise PlanRefused(
                f"{field} {theirs!r} is not this session's snapshot ({ours!r}) — "
                "the plan was built on a different pull"
            )

    # What the rows themselves say, against what the file's counts said. Only
    # the changed rows travel, so `unchanged` is the one count nothing here can
    # recount — which is why the partition below has to hold instead.
    from_rows = {
        "moved": sum(1 for r in changes if r.get("status") == "moved"),
        "no_car": sum(1 for r in changes if r.get("status") == "no_car"),
        "bumped": sum(1 for r in changes if r.get("bumped")),
    }
    mismatched = {k: (summary.get(k), v) for k, v in from_rows.items() if summary.get(k) != v}
    if mismatched:
        raise PlanRefused(
            "the summary does not match the rows sent: "
            + ", ".join(
                f"{k}: summary says {sent!r}, the rows are {found!r}"
                for k, (sent, found) in mismatched.items()
            )
        )
    total = sum(summary.get(k, 0) for k in PARTITION_KEYS)
    if total != summary.get("orders"):
        raise PlanRefused(
            f"the counts do not partition the book: "
            f"{ {k: summary.get(k) for k in PARTITION_KEYS} } is {total}, "
            f"not the {summary.get('orders')} orders in it"
        )

    # Imported here, like `_default_rich` does: both are host-side-only paths,
    # and nothing in the mounted skill bundle may depend on this module pulling
    # `datasource` in at import time.
    import datasource

    wanted = {row.get("order") for row in changes}
    held = {
        key: o.get("VehicleCode") or None
        for o in pull.get("orders", [])
        if (key := datasource.order_key(o)) in wanted
    }
    stale, staged = [], 0
    for row in changes:
        key = row.get("order")
        if key in held and row.get("was_car") != held[key]:
            card, _, line = str(key).rpartition("-")
            stale.append(
                {
                    "DMSJCNum": card,
                    "LineNum": line,
                    "expected": row.get("was_car"),
                    "actual": held[key],
                }
            )
        else:
            staged += 1

    return {
        "staged": staged,
        "skipped": summary.get("unchanged", 0),
        "rejected": len(stale),
        "stale": stale,
        "awaiting": "planner_review",
    }


def make_push_tool(get_rich: RichProvider):
    """Build the push tool over one session's pull.

    Takes the SAME provider as `make_pull_tool` — `web.py` passes one closure to
    both — so the mount path comes along and is ignored here rather than the two
    tools disagreeing about the provider's shape.

    Nothing is read from disk: the rows arrive in the call, because a file the
    agent wrote is not indexed into the session's file store until the turn ends
    and this runs mid-turn. See the note at the top of this section.
    """

    async def push_allocation_plan(
        changes: list[dict], pull_id: str, source: str, mode: str, summary: dict, note: str
    ) -> str:
        rich = get_rich()
        if inspect.isawaitable(rich):
            rich = await rich
        pull, _ = rich
        try:
            result = check_plan(changes, pull, mode, pull_id, source, summary)
        except PlanRefused as refused:
            return json.dumps({"refused": str(refused)}, indent=2)
        return json.dumps({**result, "note": note}, indent=2)

    return beta_async_tool(
        push_allocation_plan,
        name=PUSH_TOOL_NAME,
        description=PUSH_TOOL_DESCRIPTION,
        input_schema=PUSH_TOOL_INPUT_SCHEMA,
    )
