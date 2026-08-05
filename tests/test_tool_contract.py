"""The agent's tool declaration and the implementation are one contract.

A mismatch between the two does not raise: the agent emits an
``agent.custom_tool_use`` nothing answers, and the session parks on a
``requires_action`` idle that never times out.

Async tools are driven through ``asyncio.run`` rather than a pytest plugin —
one less dev dependency.
"""

import asyncio
import json

import alloc_tools


def call():
    return json.loads(asyncio.run(alloc_tools.pull_allocation_snapshot.call({})))


def test_declaration_matches_implementation():
    tool = alloc_tools.pull_allocation_snapshot
    assert tool.name == alloc_tools.PULL_TOOL["name"]
    assert tool.input_schema == alloc_tools.PULL_TOOL["input_schema"]
    assert tool.description == alloc_tools.PULL_TOOL["description"]


def test_declared_as_a_custom_tool():
    assert alloc_tools.PULL_TOOL["type"] == "custom"
    # The API bounds both: name 1-128 chars, description 1-4096.
    assert 1 <= len(alloc_tools.PULL_TOOL["name"]) <= 128
    assert 1 <= len(alloc_tools.PULL_TOOL["description"]) <= 4096


def test_pull_takes_no_parameters():
    """The scenario is pre-fabricated and bundled — nothing for the agent to tune."""
    assert alloc_tools.PULL_TOOL_INPUT_SCHEMA["properties"] == {}
    assert alloc_tools.PULL_TOOL_INPUT_SCHEMA["required"] == []


def test_summary_carries_the_disruption():
    summary = call()
    assert summary["now"]
    assert summary["orders"] > 0  # vehicle order rows
    assert summary["supply"] > 0  # vehicles ∪ PO-line slots
    assert summary["disruption"]["po"]
    assert summary["disruption"]["delay_days"] > 0
    assert summary["disrupted_orders"] == len(summary["disrupted_order_ids"])


def test_summary_carries_a_wellformed_customer_map():
    """§6: the agent resolves a dealer name to a customer_id when compiling an
    override. The map covers exactly the customers with orders in play."""
    summary = call()
    assert summary["customers"], "no customers surfaced for §6 resolution"
    for name, info in summary["customers"].items():
        assert info["customer_id"].startswith("CUST-"), name
        assert info["priority"] in {"A", "B", "C"}, name


def test_summary_stays_small():
    """The tool result crosses into the agent's context. It carries a summary and
    a command, never the rows — so it stays small regardless of dataset size."""
    assert len(json.dumps(call())) < 6000


def test_flatten_command_reproduces_the_snapshot(tmp_path):
    """The command handed to the agent must actually flatten the bundled dataset.

    It is the transport: if it drifts, the sandbox solves against different data
    than the summary describes, silently. Run against a copy of the skill layout
    (package + data under skills/xas-allocation/) because that is where the bundle
    puts it, and a command that only works from the repo root would pass here and
    fail in every real session.
    """
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    skill_dir = tmp_path / "skills" / "xas-allocation"
    skill_dir.mkdir(parents=True)
    shutil.copytree(
        repo / "xas_allocation",
        skill_dir / "xas_allocation",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (skill_dir / "data").mkdir()
    shutil.copy(repo / "data" / "pull.json", skill_dir / "data" / "pull.json")

    summary = call()
    command = summary["flatten"].replace("python ", f"{sys.executable} ", 1)
    subprocess.run(command, shell=True, cwd=tmp_path, check=True)

    written = tmp_path / alloc_tools.SNAPSHOT_FILENAME
    assert written.exists()
    snapshot = json.loads(written.read_text())
    assert len(snapshot["orders"]) == summary["orders"]
    assert len(snapshot["units"]) == summary["supply"]
    assert snapshot["disruption"]["disrupted_orders"] == summary["disrupted_order_ids"]


def test_flatten_command_never_sweeps_the_filesystem_root():
    """Regression: an earlier command ran from `/` and swept the whole container.

    The search bases must exclude `/` outright. Run from `/` with no package under
    /workspace, the command must fail fast with a message — not walk the tree.
    """
    import subprocess
    import sys
    import time

    command = call()["flatten"].replace("python ", f"{sys.executable} ", 1)

    started = time.monotonic()
    done = subprocess.run(
        command, shell=True, cwd="/", capture_output=True, text=True, timeout=60, check=False
    )
    elapsed = time.monotonic() - started

    assert done.returncode != 0, "should not have found a solver under /"
    assert "not found" in (done.stdout + done.stderr)
    assert elapsed < 30, f"took {elapsed:.1f}s — looks like it searched too widely"


def test_same_dataset_same_summary():
    """The data_snapshot half of plan = pure_function(data_snapshot, skill, ledger)."""
    assert call() == call()
