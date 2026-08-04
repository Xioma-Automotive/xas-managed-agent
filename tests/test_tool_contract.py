"""The agent's tool declaration and the worker's implementation are one contract.

A mismatch between the two does not raise: the agent emits an
``agent.custom_tool_use`` nothing answers, and the session parks on a
``requires_action`` idle that never times out. So the wiring gets a test.

Async tools are driven through ``asyncio.run`` rather than a pytest plugin —
one less dev dependency for four tests.
"""

import asyncio
import json

import alloc_tools


def call(tool, **kwargs):
    return json.loads(asyncio.run(tool.call(kwargs)))


def test_declaration_matches_implementation(tmp_path):
    tool = alloc_tools.make_pull_tool(tmp_path)
    assert tool.name == alloc_tools.PULL_TOOL["name"]
    assert tool.input_schema == alloc_tools.PULL_TOOL["input_schema"]
    assert tool.description == alloc_tools.PULL_TOOL["description"]


def test_declared_as_a_custom_tool():
    assert alloc_tools.PULL_TOOL["type"] == "custom"
    # The API bounds both: name 1-128 chars, description 1-4096.
    assert 1 <= len(alloc_tools.PULL_TOOL["name"]) <= 128
    assert 1 <= len(alloc_tools.PULL_TOOL["description"]) <= 4096


def test_schema_defaults_match_the_implementation_signature(tmp_path):
    """A default that disagrees with the signature is a silent behaviour change:
    the model reads the schema, the function applies the signature."""
    import inspect

    tool = alloc_tools.make_pull_tool(tmp_path)
    signature = inspect.signature(tool.func)
    for name, prop in alloc_tools.PULL_TOOL_INPUT_SCHEMA["properties"].items():
        assert signature.parameters[name].default == prop["default"], name


def test_pull_writes_the_snapshot_and_returns_a_summary(tmp_path):
    tool = alloc_tools.make_pull_tool(tmp_path)
    summary = call(tool, seed=20, n_orders=12, spare_ratio=0.5, delay_weeks=2)

    written = tmp_path / alloc_tools.SNAPSHOT_FILENAME
    assert written.exists()
    snapshot = json.loads(written.read_text())

    assert len(snapshot["orders"]) == 12
    assert summary["snapshot_path"] == alloc_tools.SNAPSHOT_FILENAME
    assert summary["orders"] == 12
    assert summary["units"] == len(snapshot["units"])
    assert summary["disruption"]["delay_weeks"] == 2
    assert summary["disrupted_orders"] == len(snapshot["disruption"]["disrupted_orders"])


def test_summary_carries_the_customer_map(tmp_path):
    """§6: the agent resolves a dealer name to a customer_id when compiling an
    override. Without this it would have to guess or read the whole snapshot."""
    summary = call(alloc_tools.make_pull_tool(tmp_path), seed=1, n_orders=10)
    assert summary["customers"]["Colmobil"]["customer_id"] == "CUST-001"


def test_summary_excludes_the_rows(tmp_path):
    """The point of the file-plus-summary shape: the pull stays small however
    many orders it covers."""
    summary = call(alloc_tools.make_pull_tool(tmp_path), seed=3, n_orders=500)
    assert "orders" in summary and isinstance(summary["orders"], int)
    assert len(json.dumps(summary)) < 4000


def test_same_seed_same_bytes(tmp_path):
    """The data_snapshot half of plan = pure_function(data_snapshot, skill, ledger)."""
    a, b = tmp_path / "a", tmp_path / "b"
    call(alloc_tools.make_pull_tool(a), seed=7, n_orders=30)
    call(alloc_tools.make_pull_tool(b), seed=7, n_orders=30)
    assert (a / alloc_tools.SNAPSHOT_FILENAME).read_bytes() == (
        b / alloc_tools.SNAPSHOT_FILENAME
    ).read_bytes()


def test_different_seed_different_snapshot(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    call(alloc_tools.make_pull_tool(a), seed=7, n_orders=30)
    call(alloc_tools.make_pull_tool(b), seed=8, n_orders=30)
    assert (a / alloc_tools.SNAPSHOT_FILENAME).read_bytes() != (
        b / alloc_tools.SNAPSHOT_FILENAME
    ).read_bytes()
