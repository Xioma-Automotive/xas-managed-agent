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


def call(**kwargs):
    return json.loads(asyncio.run(alloc_tools.pull_allocation_snapshot.call(kwargs)))


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


def test_schema_defaults_match_the_implementation_signature():
    """A default that disagrees with the signature is a silent behaviour change:
    the model reads the schema, the function applies the signature."""
    import inspect

    signature = inspect.signature(alloc_tools.pull_allocation_snapshot.func)
    for name, prop in alloc_tools.PULL_TOOL_INPUT_SCHEMA["properties"].items():
        assert signature.parameters[name].default == prop["default"], name


def test_summary_carries_the_disruption():
    summary = call(seed=20, n_orders=12, spare_ratio=0.5, delay_weeks=2)
    assert summary["seed"] == 20
    assert summary["orders"] == 12
    assert summary["disruption"]["delay_weeks"] == 2
    assert summary["disrupted_orders"] == len(summary["disrupted_order_ids"])


def test_summary_carries_the_customer_map():
    """§6: the agent resolves a dealer name to a customer_id when compiling an
    override. Without this it would have to guess or read the whole snapshot."""
    summary = call(seed=1, n_orders=10)
    assert summary["customers"]["Colmobil"]["customer_id"] == "CUST-001"


def test_summary_stays_small_regardless_of_size():
    """The tool result crosses into the agent's context. Returning the rows would
    put ~100 KB there on every pull; the seed reproduces them in the sandbox."""
    summary = call(seed=3, n_orders=500)
    assert summary["orders"] == 500
    assert len(json.dumps(summary)) < 4000


def test_materialize_command_reproduces_the_snapshot(tmp_path):
    """The command handed to the agent must actually rebuild the same snapshot.

    It is the transport: if it drifts from the tool's own parameters, the sandbox
    solves against different data than the summary describes, silently.

    Run against a copy of the skill layout — the package under
    ``skills/xas-allocation/`` rather than in the working directory — because
    that is where the bundle puts it, and a command that only works from the repo
    root would pass here and fail in every real session.
    """
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    skill_dir = tmp_path / "skills" / "xas-allocation"
    skill_dir.mkdir(parents=True)
    shutil.copytree(
        Path(__file__).resolve().parent.parent / "xas_allocation",
        skill_dir / "xas_allocation",
        ignore=shutil.ignore_patterns("__pycache__"),
    )

    params = {"seed": 20, "n_orders": 12, "spare_ratio": 0.5, "delay_weeks": 2}
    summary = call(**params)

    command = summary["materialize"].replace("python ", f"{sys.executable} ", 1)
    subprocess.run(command, shell=True, cwd=tmp_path, check=True)

    written = tmp_path / alloc_tools.SNAPSHOT_FILENAME
    assert written.exists()
    snapshot = json.loads(written.read_text())
    assert len(snapshot["orders"]) == summary["orders"]
    assert len(snapshot["units"]) == summary["units"]
    assert snapshot["seed"] == summary["seed"]
    assert snapshot["disruption"]["disrupted_orders"] == summary["disrupted_order_ids"]


def test_materialize_command_never_sweeps_the_filesystem_root():
    """Regression: the agent ran this from `/` and swept the whole container.

    The search bases must exclude `/` outright rather than trusting the caller's
    working directory. Run from `/` with no package under /workspace, the command
    must fail fast with a message — not walk the filesystem.
    """
    import subprocess
    import sys
    import time

    command = call(seed=1, n_orders=5)["materialize"].replace("python ", f"{sys.executable} ", 1)

    started = time.monotonic()
    done = subprocess.run(
        command, shell=True, cwd="/", capture_output=True, text=True, timeout=60, check=False
    )
    elapsed = time.monotonic() - started

    assert done.returncode != 0, "should not have found a solver under /"
    assert "not found" in (done.stdout + done.stderr)
    # A root sweep on any real machine takes far longer than this.
    assert elapsed < 30, f"took {elapsed:.1f}s — looks like it searched too widely"


def test_same_seed_same_summary():
    """The data_snapshot half of plan = pure_function(data_snapshot, skill, ledger)."""
    assert call(seed=7, n_orders=30) == call(seed=7, n_orders=30)


def test_different_seed_different_snapshot():
    assert call(seed=7, n_orders=30) != call(seed=8, n_orders=30)
