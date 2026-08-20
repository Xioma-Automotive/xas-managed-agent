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
import appmcp_auth
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
# The app MCP — declared in two places that must agree, credentialed in a third
# --------------------------------------------------------------------------


def test_every_mcp_server_is_granted_by_a_toolset():
    """A declared server no toolset references is a validation error on create,
    and a toolset naming an undeclared server is too."""
    declared = {s["name"] for s in setup_agent.MCP_SERVERS}
    granted = {t["mcp_server_name"] for t in setup_agent.TOOLS if t.get("type") == "mcp_toolset"}
    assert declared == granted == {setup_agent.APPMCP_SERVER_NAME}


def test_mcp_tools_run_without_a_confirmation_nobody_sends():
    """Observed 2026-08-19: an mcp_toolset with no permission_policy resolves to
    `always_ask`, NOT the documented `always_allow`. The session then emits
    agent.mcp_tool_use and idles forever waiting for a user.tool_confirmation
    web.py never sends — indistinguishable from the MCP being down."""
    toolset = next(t for t in setup_agent.TOOLS if t.get("type") == "mcp_toolset")
    assert toolset["default_config"]["permission_policy"] == {"type": "always_allow"}


def test_mcp_url_matches_the_credential_the_host_mints_for():
    """Vault matching normalizes host case and default ports but compares the
    PATH byte-for-byte. A mismatch is not an error: the connection is attempted
    unauthenticated, so it surfaces as a 401 from the MCP instead."""
    assert [s["url"] for s in setup_agent.MCP_SERVERS] == [appmcp_auth.APPMCP_URL]


def test_credential_config_is_read_after_the_environment_loads(monkeypatch):
    """Regression (2026-08-20): appmcp_auth read its config at IMPORT time, and
    web.py imports it before calling load_dotenv(). Every value came back None,
    so configured() was False, `vault_ids` was silently omitted from the session,
    and the agent's first MCP call failed with "no credential is stored for this
    server URL" — a message that points at the URL, not at the real cause."""
    for name in appmcp_auth.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert appmcp_auth.configured() is False

    for name in appmcp_auth.REQUIRED_ENV:
        monkeypatch.setenv(name, "set-after-import")
    assert appmcp_auth.configured() is True, "config must be read per call, not at import"
    assert appmcp_auth.vault_id() == "set-after-import"


def test_environment_allows_mcp_egress():
    """Under `limited` networking without this, MCP tools fail SILENTLY."""
    assert setup_agent.NETWORKING["allow_mcp_servers"] is True
    assert setup_agent.NETWORKING["allowed_hosts"] == [], "the agent still reaches nothing else"


def test_setup_refreshes_the_environment_it_reuses():
    """The environment predates the MCP and was created with allow_mcp_servers
    off; updating the agent alone would leave every MCP call failing quietly."""
    source = (REPO_ROOT / "setup_agent.py").read_text(encoding="utf-8")
    assert source.count("update_environment(ALLOC_ENV_ID)") == 2


def test_prompt_fences_answers_to_the_two_data_sources():
    """Observed 2026-08-20: asked which car David Bowie drove, the agent answered
    from model memory (a Volvo 262C, a Mercedes 600) because a customer in the
    tenant happens to carry that name. Nothing sourced it, so nothing could
    contradict it — the same failure mode as an unresolved term, one step further
    out. The fence has to name the sources it does have and forbid the
    gap-filling. Two of them since the fabricated records went away."""
    prompt = setup_agent.SYSTEM_PROMPT
    rule = prompt.split("Hard rules (never violate)")[1][:1600]
    assert "Answer only from this dealership's data" in rule
    assert "two sources" in rule
    assert "ROW, not the thing it resembles" in rule, "a familiar name must stay a row"
    assert "do not spend a tool call on it" in rule, "an off-topic ask must not cost tokens"


def test_effort_is_set_on_the_agent_not_the_session_override():
    """`effort` inside a per-session `model` override is silently ignored — no
    error, no effect. web.py sends such an override for the model picker, so
    effort only lands if the AGENT carries it."""
    config = setup_agent.model_config()
    assert config["id"] == setup_agent.MODEL
    assert config["effort"] == setup_agent.EFFORT
    source = (REPO_ROOT / "web.py").read_text(encoding="utf-8")
    assert "effort" not in source, "an effort in web.py's override would do nothing"


def test_session_carries_a_spend_ceiling_from_the_start():
    """`budget` is create-only: a session started without one can never be given
    one, so this has to be on every create or the ceiling does not exist."""
    budget = web.SESSION_BUDGET
    assert budget["type"] == "limit"
    amount = budget["max_list_cost"]["amount"]
    assert amount.isdigit() and not amount.startswith("0"), "cents, integer string"
    assert budget["max_list_cost"]["currency"] == "USD", "the only supported currency"
    source = (REPO_ROOT / "web.py").read_text(encoding="utf-8")
    assert 'extra_body={"budget": SESSION_BUDGET}' in source, "sent at create, or never"


def test_prompt_caps_the_effort_an_off_topic_ask_may_spend():
    """Two failures, one clause. A vague ask first pulled 200 job cards and
    rendered a 17-row table; capped at a single lookup it then spent that lookup
    on vehicles, found nothing, and reported "nothing found" for a name that has
    six accounts. So: resolve the entity first, then ONE follow-up, then stop."""
    prompt = setup_agent.SYSTEM_PROMPT
    rule = prompt.split("Hard rules (never violate)")[1][:2600]
    assert "`get_accounts` first" in rule, "a name lives on an account"
    assert "ONE follow-up" in rule
    assert "No tables, no breakdowns" in rule


def test_prompt_forbids_sourcing_allocation_from_the_mcp():
    """The MCP is the easiest way to answer 'which orders are late' with a number
    that is real, plausible, and not reproducible. It is also now REPORTING's
    only source, so this rule is the whole fence between the two lanes."""
    prompt = setup_agent.SYSTEM_PROMPT
    rule = prompt.split("The plan comes from the solver, not from you.")[1][:900]
    assert "NEVER from an `xas-app-mcp` tool" in rule


def test_prompt_stops_claiming_there_is_no_network():
    """It said 'No network access — everything is local', which is now false and
    contradicts a tool the agent holds."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "No network access — everything is local." not in prompt
    assert 'one exception to "everything is local"' in prompt


def test_prompt_makes_the_agent_name_its_reporting_source():
    """Reporting reads the LIVE system, so a number is only true as of the moment
    it was asked. The planner cannot tell that from the number."""
    assert "from the live system" in setup_agent.SYSTEM_PROMPT


# --------------------------------------------------------------------------
# The rule that keeps the two lanes from contaminating each other
# --------------------------------------------------------------------------


def test_prompt_forbids_answering_allocation_from_a_file_read():
    """The fabricated job-card records are gone, so the rule can no longer forbid
    a PATH -- but a file read is still the other way to produce an allocation
    number without the solver (the pull is mounted, and the agent can cat it)."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "The plan comes from the solver, not from you." in prompt
    rule = prompt.split("The plan comes from the solver, not from you.")[1][:700]
    assert "NEVER" in rule and "NEVER from a file you read yourself" in rule


def test_prompt_names_no_records_mount():
    """The reporting lane reads the live MCP now. A path the host does not mount
    sends the agent looking for a file that is not there -- which is exactly how
    it silently substituted the live system for the records."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "/workspace/reports" not in prompt
    assert "jobcards.json" not in prompt


def test_prompt_names_every_mount():
    prompt = setup_agent.SYSTEM_PROMPT
    for path in (alloc_tools.MOUNT_PATH,):
        assert path in prompt, f"{path} is mounted but never explained to the agent"


def test_prompt_says_where_the_taxonomy_lives():
    """It is no longer a mount (DECIDE-16), so the prompt must send the agent to
    the skill directory instead of a path that does not exist."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "index.md` ships inside the `xas-qa` skill directory" in prompt
    assert "/workspace/reports/index.md" not in prompt


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


def test_qa_bundle_ships_the_phrasebook_builder_and_the_taxonomy():
    """The taxonomy is the ONE dataset that ships in a bundle (DECIDE-16) — it is
    static config for the single tenant, and phrasebook.py finds it beside
    itself instead of hunting for a mount."""
    assert [n for n, _ in setup_agent.qa_bundle()] == [
        "xas-qa/SKILL.md",
        "xas-qa/index.md",
        "xas-qa/phrasebook.py",
    ]


@pytest.mark.parametrize("bundle", [setup_agent.alloc_bundle(), setup_agent.qa_bundle()])
def test_no_session_dataset_is_bundled(bundle):
    """The pull is mounted per session, so regenerating it needs no redeploy.
    (The taxonomy is the deliberate exception — DECIDE-16.)"""
    for name, _ in bundle:
        assert "pull.json" not in name


# --------------------------------------------------------------------------
# Mounts
# --------------------------------------------------------------------------


def test_the_pull_is_the_only_mount():
    """Reporting used to get a second mount under /workspace/reports/. It reads
    the live MCP now, so a session that mounts anything else is a session whose
    reporting numbers came from somewhere this design does not control."""
    assert web.MOUNTED_INPUT_FILENAMES == frozenset({web.MOUNTED_PULL_FILENAME})
    source = (REPO_ROOT / "web.py").read_text(encoding="utf-8")
    assert source.count('"type": "file"') == 1, "one resource, or the fence moved"


def test_every_mounted_input_is_filtered_from_outputs():
    """files.list(scope_id=...) returns the inputs too; handing a planner their
    own pull back as an 'output' is noise, and downloading it is worse."""
    mounted = {Path(alloc_tools.MOUNT_PATH).name}
    assert mounted == set(web.MOUNTED_INPUT_FILENAMES)


# --------------------------------------------------------------------------
# The bundled taxonomy (DECIDE-16)
# --------------------------------------------------------------------------


def test_bundled_taxonomy_is_the_real_index():
    bundled = dict(setup_agent.qa_bundle())["xas-qa/index.md"]
    assert bundled.startswith(b"# Taxonomy")


def test_host_no_longer_serves_a_taxonomy():
    """It ships in the skill now, so the per-session upload is gone. Leaving a
    dead get_taxonomy behind is how the mount quietly comes back."""
    assert not hasattr(datasource, "get_taxonomy")
    assert not hasattr(web, "TAXONOMY_MOUNT_PATH")


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


def test_qa_skill_sends_the_agent_to_the_mcp_not_to_a_file():
    """Every records path the skill named is gone. One left behind sends the
    agent hunting a mount that does not exist, and the recovery it improvises is
    the live MCP with no mention of where the number came from."""
    skill = (setup_agent.QA_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "/workspace/index.md" not in skill
    assert "/workspace/jobcards.json" not in skill
    assert "/workspace/reports" not in skill
    assert "xas-app-mcp" in skill, "the skill must say where records come from"


@pytest.mark.parametrize(
    "phrase", ["deliveries", "sales order", "vehicle purchase order", "what is late"]
)
def test_alloc_description_carries_the_words_users_type(phrase):
    """The description is what the platform routes on, and a planner says "check
    the deliveries", never "repair the allocation"."""
    assert phrase in _description(setup_agent.ALLOC_SKILL_DIR / "SKILL.md").lower()


def test_qa_description_disclaims_the_allocation_vocabulary():
    """ "How many VSOs are late" is a COUNT, which reads like reporting -- and is
    an allocation question. Both descriptions must say so or the platform picks
    on surface form."""
    qa = _description(setup_agent.QA_SKILL_DIR / "SKILL.md").lower()
    for phrase in ("deliveries", "vso", "vpo", "supply"):
        assert phrase in qa


def test_alloc_skill_stops_a_status_question_at_the_report():
    skill = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "A question about the state stops at the discrepancy report." in skill
    assert "no VPO ids" in skill, "the VPO-number limit must be stated, not discovered"


def test_prompt_routes_the_everyday_words():
    prompt = setup_agent.SYSTEM_PROMPT.lower()
    for phrase in ("deliveries", "vehicle purchase order", "what's late"):
        assert phrase in prompt


def test_qa_skill_has_a_dead_end_rule():
    """A term that resolves to nothing used to be undefined behaviour, so the
    model improvised -- sometimes answering with the closest-looking code, which
    returns a real-looking number nobody can tell is wrong."""
    skill = (setup_agent.QA_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Never answer with an unresolved term." in skill
    assert "--suggest" in skill, "the typo rung must be documented or it is never run"


def test_prompt_forbids_answering_an_unresolved_term():
    assert "NEVER answer with a term you could not resolve" in setup_agent.SYSTEM_PROMPT


def test_phrasebook_reads_the_taxonomy_beside_itself():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "phrasebook", setup_agent.QA_SKILL_DIR / "phrasebook.py"
    )
    phrasebook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(phrasebook)
    assert phrasebook.INDEX_PATH == (setup_agent.QA_SKILL_DIR / "index.md").resolve()
    assert phrasebook.default_index() is not None


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
    assert "do not read the chart back" in prompt
    assert (
        "do not read the chart back"
        in (setup_agent.QA_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").lower()
    )


def test_web_serves_file_content_for_the_browser():
    paths = {r.path for r in web.app.routes}
    assert "/session/{session_id}/files/{file_id}/content" in paths


@pytest.mark.parametrize(
    "filename,media_type,mode",
    [
        ("late_by_dealer.html", "text/html", "frame"),
        ("chart.png", "image/png", "image"),
        ("plot.svg", "image/svg+xml", "image"),
        ("notes.txt", "text/plain", "link"),
        ("data", "application/octet-stream", "link"),
    ],
)
def test_render_mode_tells_the_browser_how_to_show_an_output(filename, media_type, mode):
    assert web._media_type(filename) == media_type
    assert web._render_mode(filename) == mode


def test_charts_are_self_contained_html():
    """Inline SVG, not a CDN link: the page is opened later in another browser,
    so anything it must fetch is a dependency that can fail. It also measures
    SMALLER than the equivalent PNG (39.5KB vs 55.5KB for the same chart)."""
    skill = (setup_agent.QA_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "self-contained" in skill.lower()
    assert 'format="svg"' in skill, "the recipe must save SVG, not PNG"
    assert "matplotlib.use" in skill, "no display in the sandbox — Agg backend required"
    assert "never reference a cdn" in skill.lower()
    assert "self-contained" in setup_agent.SYSTEM_PROMPT.lower()


def test_html_charts_are_framed_not_trusted():
    """A chart is model-generated HTML. It renders in an opaque origin so it
    cannot reach this page, its cookies, or the session routes."""
    ui = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    frame = ui[ui.index('<iframe class="output-frame"') :][:200]
    assert 'sandbox="allow-scripts"' in frame
    assert "allow-same-origin" not in frame
