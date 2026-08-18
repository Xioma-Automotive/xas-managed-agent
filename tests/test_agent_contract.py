"""One agent, two skills — the wiring that has no other guard.

Everything here is the agent's *contract*: what `setup_agent.py` declares and
what `web.py` mounts. It matters because `agents.update()` PRESERVES omitted
array fields — a `skills` or `tools` list that is not sent is a list that does
not change, so a setup that quietly drops one ships the previous value and the
failure surfaces as an agent that has "forgotten" a capability.

The load-bearing test is `test_prompt_forbids_answering_allocation_from_records`.
Merging the two lanes put the reporting records in the same sandbox as the
solver, so the agent can now answer "which orders are late?" by reading a file.
That answer would look right and not be reproducible, which is the exact leak
`plan = pure_function(data_snapshot, skill, override)` exists to prevent.

Runs host-side with no API key and no network, like the rest of the suite.
"""

from pathlib import Path

import pytest

import alloc_tools
import datasource
import setup_agent
import web

REPO_ROOT = Path(__file__).resolve().parent.parent


def _description(skill_md: Path) -> str:
    """The frontmatter `description:` block — what the platform routes on."""
    text = skill_md.read_text(encoding="utf-8")
    _, frontmatter, _ = text.split("---", 2)
    body = frontmatter.split("description:", 1)[1]
    return " ".join(body.split("name:")[0].split())


# --------------------------------------------------------------------------
# What the agent declares
# --------------------------------------------------------------------------


def test_agent_carries_both_skills():
    skills = setup_agent._skills("sk_alloc", "sk_qa")
    assert [s["skill_id"] for s in skills] == ["sk_alloc", "sk_qa"]
    assert all(s["type"] == "custom" for s in skills)


def test_agent_still_declares_the_pull_tool():
    """Dropping it makes the pull unanswerable; the session would hang, not error."""
    assert alloc_tools.PULL_TOOL in setup_agent.TOOLS


def test_web_search_and_fetch_stay_off():
    """A web lookup could only add state the snapshot doesn't hold."""
    toolset = next(t for t in setup_agent.TOOLS if t.get("type") == "agent_toolset_20260401")
    disabled = {c["name"] for c in toolset["configs"] if not c["enabled"]}
    assert {"web_search", "web_fetch"} <= disabled


# --------------------------------------------------------------------------
# The rule that keeps the two lanes from contaminating each other
# --------------------------------------------------------------------------


def test_prompt_forbids_answering_allocation_from_records():
    prompt = setup_agent.SYSTEM_PROMPT
    assert "The plan comes from the solver, not from you." in prompt
    assert web.RECORDS_MOUNT_PATH in prompt
    # The prohibition must name the records path, not just gesture at it.
    rule = prompt.split("The plan comes from the solver, not from you.")[1][:700]
    assert "NEVER" in rule and web.RECORDS_MOUNT_PATH in rule


def test_prompt_names_every_mount():
    prompt = setup_agent.SYSTEM_PROMPT
    for path in (alloc_tools.MOUNT_PATH, web.TAXONOMY_MOUNT_PATH, web.RECORDS_MOUNT_PATH):
        assert path in prompt, f"{path} is mounted but never explained to the agent"


def test_prompt_answers_in_the_users_language():
    """The dealership works in Hebrew and English; a Hebrew question gets Hebrew back."""
    assert "language the person wrote in" in setup_agent.SYSTEM_PROMPT


# --------------------------------------------------------------------------
# Skill routing — the descriptions are what the platform selects on
# --------------------------------------------------------------------------


def test_skill_descriptions_are_disjoint():
    qa = _description(setup_agent.QA_SKILL_DIR / "SKILL.md")
    alloc = _description(setup_agent.ALLOC_SKILL_DIR / "SKILL.md")
    assert "Do NOT use for allocation repair" in qa
    assert "Do NOT use for general reporting" in alloc


def test_qa_skill_does_not_claim_every_turn():
    """It once said 'use on every turn that names a document type' — alone on its
    own agent that was fine; beside xas-allocation it fires on allocation turns,
    because a VSO *is* a document type."""
    assert "every turn" not in _description(setup_agent.QA_SKILL_DIR / "SKILL.md")


# --------------------------------------------------------------------------
# Bundles: code ships, data does not
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bundle,root",
    [(setup_agent.alloc_bundle(), "xas-allocation"), (setup_agent.qa_bundle(), "xas-qa")],
)
def test_bundle_has_skill_md_at_its_root(bundle, root):
    assert any(name == f"{root}/SKILL.md" for name, _ in bundle)


def test_alloc_bundle_ships_the_solver():
    names = [n for n, _ in setup_agent.alloc_bundle()]
    assert "xas-allocation/xas_allocation/solver.py" in names


def test_qa_bundle_ships_the_phrasebook_builder():
    assert [n for n, _ in setup_agent.qa_bundle()] == [
        "xas-qa/SKILL.md",
        "xas-qa/phrasebook.py",
    ]


@pytest.mark.parametrize("bundle", [setup_agent.alloc_bundle(), setup_agent.qa_bundle()])
def test_no_dataset_is_bundled(bundle):
    """Data is mounted per session, so regenerating it needs no redeploy."""
    for name, _ in bundle:
        assert "pull.json" not in name
        assert "flat-index" not in name
        assert "jobcards" not in name


# --------------------------------------------------------------------------
# Mounts
# --------------------------------------------------------------------------


def test_mount_paths_are_distinct_and_reports_are_namespaced():
    paths = [alloc_tools.MOUNT_PATH, web.TAXONOMY_MOUNT_PATH, web.RECORDS_MOUNT_PATH]
    assert len(set(paths)) == 3
    assert web.TAXONOMY_MOUNT_PATH.startswith(web.REPORTS_MOUNT_DIR + "/")
    assert web.RECORDS_MOUNT_PATH.startswith(web.REPORTS_MOUNT_DIR + "/")
    assert not alloc_tools.MOUNT_PATH.startswith(web.REPORTS_MOUNT_DIR)


def test_every_mounted_input_is_filtered_from_outputs():
    """files.list(scope_id=...) returns the inputs too; handing a planner their
    own pull back as an 'output' is noise, and downloading it is worse."""
    mounted = {
        Path(alloc_tools.MOUNT_PATH).name,
        Path(web.TAXONOMY_MOUNT_PATH).name,
        Path(web.RECORDS_MOUNT_PATH).name,
    }
    assert mounted == set(web.MOUNTED_INPUT_FILENAMES)


# --------------------------------------------------------------------------
# The taxonomy the caller picks (DECIDE-16)
# --------------------------------------------------------------------------


def test_default_taxonomy_resolves():
    name, blob = datasource.get_taxonomy()
    assert name == datasource.DEFAULT_TAXONOMY
    assert blob.startswith(b"# Taxonomy")


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", "nope", "xioma-DMSDEV2023/.."])
def test_taxonomy_name_is_looked_up_never_joined(bad):
    """The name arrives from the frontend, so it must never reach the filesystem."""
    with pytest.raises(RuntimeError, match="Unknown taxonomy"):
        datasource.get_taxonomy(bad)


# --------------------------------------------------------------------------
# Where things actually live in the sandbox
#
# Observed 2026-08-18: a resource requested at /workspace/pull.json appeared at
# /mnt/session/uploads/workspace/pull.json instead, and the flatten command --
# which read the requested path directly -- failed. The agent improvised by
# copying files around, which is exactly the "state leaked out of the snapshot"
# failure the invariant exists to prevent. These pin the resolution.
# --------------------------------------------------------------------------


def test_pull_is_resolved_not_assumed():
    candidates = alloc_tools.mount_candidates()
    assert alloc_tools.MOUNT_PATH in candidates
    assert f"{alloc_tools.UPLOAD_PREFIX}{alloc_tools.MOUNT_PATH}" in candidates


def test_flatten_command_tries_every_candidate():
    command = alloc_tools.flatten_command()
    for candidate in alloc_tools.mount_candidates():
        assert candidate in command, f"{candidate} unreachable by the flatten command"


def test_flatten_command_never_searches_from_root():
    """An unbounded rglob from / once swept the container and killed the shell."""
    command = alloc_tools.flatten_command()
    assert "rglob" in command
    assert "p != root" in command


def test_qa_skill_points_at_the_paths_web_mounts():
    """Namespacing the mounts under /workspace/reports/ silently orphaned the
    skill's instructions -- it still said /workspace/index.md."""
    skill = (setup_agent.QA_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "/workspace/index.md" not in skill
    assert "/workspace/jobcards.json" not in skill
    assert web.REPORTS_MOUNT_DIR in skill
    assert alloc_tools.UPLOAD_PREFIX in skill, "skill must mention the upload fallback"


def test_phrasebook_resolves_the_taxonomy_mount():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "phrasebook", setup_agent.QA_SKILL_DIR / "phrasebook.py"
    )
    phrasebook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(phrasebook)
    paths = [str(c) for c in phrasebook.INDEX_CANDIDATES]
    assert f"{web.TAXONOMY_MOUNT_PATH}" in paths
    assert f"{alloc_tools.UPLOAD_PREFIX}{web.TAXONOMY_MOUNT_PATH}" in paths


# --------------------------------------------------------------------------
# Charts reaching the planner's screen
# --------------------------------------------------------------------------

OUTPUTS_DIR = "/mnt/session/outputs"


def test_agent_is_told_where_charts_must_go():
    """Only /mnt/session/outputs is captured by the Files API. A chart written
    anywhere else runs successfully and is seen by nobody."""
    assert OUTPUTS_DIR in setup_agent.SYSTEM_PROMPT
    assert OUTPUTS_DIR in (setup_agent.QA_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_agent_is_told_not_to_read_the_chart_back():
    """Reading a PNG back returns base64 -- ~100KB of context for no new information."""
    prompt = setup_agent.SYSTEM_PROMPT.lower()
    assert "do not read the image back" in prompt


def test_web_serves_file_content_for_the_browser():
    paths = {r.path for r in web.app.routes}
    assert "/session/{session_id}/files/{file_id}/content" in paths


@pytest.mark.parametrize(
    "filename,expected,inline",
    [
        ("chart.png", "image/png", True),
        ("plot.svg", "image/svg+xml", True),
        ("report.html", "text/html", False),
        ("notes", "application/octet-stream", False),
    ],
)
def test_media_type_drives_inline_rendering(filename, expected, inline):
    assert web._media_type(filename) == expected
    assert web._media_type(filename).startswith("image/") is inline
