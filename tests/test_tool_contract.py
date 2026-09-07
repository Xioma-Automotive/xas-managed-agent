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
    """The pull takes no input yet — a fetch scope is a documented follow-up."""
    assert alloc_tools.PULL_TOOL_INPUT_SCHEMA["properties"] == {}
    assert alloc_tools.PULL_TOOL_INPUT_SCHEMA["required"] == []


def test_the_answer_carries_the_contracts_five_header_fields():
    """The "Allocation solve contract" fixes these five and the frontend is read
    against them. `file` is the mounted path, not rows — the whole point of the
    shape."""
    summary = call()
    assert set(summary) >= {"pull_id", "captured_at", "source", "counts", "file"}
    assert summary["pull_id"] and summary["captured_at"] == "2026-08-25"
    assert summary["source"].startswith("scenario-")
    assert set(summary["counts"]) == {"orders", "vehicles"}
    assert summary["file"] == alloc_tools.PULL_MOUNT_PATH
    assert summary["file"].endswith(".json")


def test_the_answer_names_the_file_the_platform_gave_back_not_a_constant():
    """`file` must be the path `resources.add` actually returned, so it is a
    parameter here. Guessing it is how the sandbox solves against nothing."""
    pull, _ = alloc_tools._default_rich()
    elsewhere = "/mnt/session/uploads/workspace/dms_allocation.json"
    summary = alloc_tools.summarize(pull, elsewhere)
    assert summary["file"] == elsewhere
    assert elsewhere in summary["flatten"]


def test_summary_carries_the_shape_of_the_problem():
    summary = call()
    counts = summary["counts"]
    assert counts["orders"] > 0
    assert summary["orders_holding_a_car"] + summary["orders_holding_no_car"] == counts["orders"]
    assert counts["vehicles"] > 0
    assert 0 < summary["free_supply"] <= counts["vehicles"]
    assert summary["late_orders"] == len(summary["late_order_keys"])
    # keys are the contract's card+line, never a bare card
    assert all("-" in key for key in summary["late_order_keys"])
    assert 0 < summary["job_cards"] <= counts["orders"]


def test_the_summary_reports_how_late_things_are_not_a_manifest():
    """The fake's delay manifest ("30 days on 25 vehicles") went with the fake:
    the export records no such thing, so the only honest summary of a disruption
    is the spread the dates now imply."""
    summary = call()
    late = summary["days_late"]
    assert set(late) == {"min", "median", "max"}
    assert 0 < late["min"] <= late["median"] <= late["max"]
    assert "delay_days" not in summary and "delayed_vehicles" not in summary


def test_summary_stays_small():
    """The tool result crosses into the agent's context. It carries a summary and
    a command, never the rows — so it stays small regardless of dataset size."""
    assert len(json.dumps(call())) < 6000


def test_flatten_command_reproduces_the_snapshot(tmp_path):
    """The command handed to the agent must actually flatten the MOUNTED pull.

    It is the transport: if it drifts, the sandbox solves against different data
    than the summary describes, silently. Stage the two things a real session has
    — the solver package under a skill layout (self-located) and the document
    mounted at a path the platform reported (read directly) — and point the
    command at them.
    """
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import datasource

    repo = Path(__file__).resolve().parent.parent
    skill_dir = tmp_path / "skills" / "xas-allocation"
    skill_dir.mkdir(parents=True)
    shutil.copytree(
        repo / "xas_allocation",
        skill_dir / "xas_allocation",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    # The one mount, exactly as web.py mounts it.
    pull = datasource.get_source().pull()
    pull_path = tmp_path / "dms_allocation.json"
    pull_path.write_text(json.dumps(datasource.document(pull)))

    summary = alloc_tools.summarize(pull, str(pull_path))
    command = alloc_tools.flatten_command(str(pull_path))
    command = command.replace("python ", f"{sys.executable} ", 1)
    subprocess.run(command, shell=True, cwd=tmp_path, check=True)

    written = tmp_path / alloc_tools.SNAPSHOT_FILENAME
    assert written.exists()
    snapshot = json.loads(written.read_text())
    assert len(snapshot["orders"]) == summary["counts"]["orders"]
    assert len(snapshot["vehicles"]) == summary["counts"]["vehicles"]
    assert snapshot["disruption"]["disrupted_orders"] == summary["late_order_keys"]


def test_the_command_fails_loudly_when_the_pull_is_not_mounted(tmp_path):
    """No pull is worse than a slow one: without this the solver would run over
    nothing and report every order unplaceable. The command must name the path it
    looked at instead."""
    import subprocess
    import sys
    from pathlib import Path

    command = alloc_tools.flatten_command(str(tmp_path / "missing.json"))
    command = command.replace("python ", f"{sys.executable} ", 1)
    # From the repo root, so the package IS found and the mount check is what
    # fails — otherwise this would pass for the wrong reason.
    done = subprocess.run(
        command,
        shell=True,
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "not mounted" in done.stdout + done.stderr
    assert "missing.json" in done.stdout + done.stderr


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
    """The data_snapshot half of plan = pure_function(data_snapshot, skill, override).

    `pull_id` is excluded: it names THIS read of the data, so two reads must
    differ in it and in nothing else. Nothing is priced with it."""
    a, b = call(), call()
    assert a.pop("pull_id") != b.pop("pull_id")
    assert a == b
