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
    skills = setup_agent._skills("sk_alloc", "sk_reporting")
    assert [s["skill_id"] for s in skills] == ["sk_alloc", "sk_reporting"]
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
    assert "`get_account_list` first" in rule, "a name lives on an account"
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
    a PATH -- but working a number out of the data is still the other way to
    produce an allocation claim without the solver (the pull is mounted, and the
    agent can cat it).

    Reworded 2026-08-27 on the merge: the rule used to read "NEVER from a file you
    read yourself", which the skill now contradicts -- `repair_and_report` WRITES
    `plan.json` and every follow-up is a read of it. What is banned is the agent's
    own derivation, and the helpers' own output file is the one exception."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "The plan comes from the solver, not from you." in prompt
    rule = prompt.split("The plan comes from the solver, not from you.")[1][:700]
    assert "NEVER from an `xas-app-mcp` tool" in rule
    assert "never worked out by your own reading of the data" in rule
    assert "The one file you re-read is the plan the helpers wrote." in rule


def test_prompt_names_no_records_mount():
    """The reporting lane reads the live MCP now. A path the host does not mount
    sends the agent looking for a file that is not there -- which is exactly how
    it silently substituted the live system for the records."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "/workspace/reports" not in prompt
    assert "jobcards.json" not in prompt


def test_prompt_names_every_mount():
    prompt = setup_agent.SYSTEM_PROMPT
    for path in alloc_tools.MOUNT_PATHS:
        assert path in prompt, f"{path} is mounted but never explained to the agent"


def test_prompt_says_where_the_taxonomy_lives():
    """It is no longer a mount (DECIDE-16), so the prompt must send the agent to
    the skill directory instead of a path that does not exist."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "index.md` ships inside the `xas-reporting` skill directory" in prompt
    assert "/workspace/reports/index.md" not in prompt


def test_prompt_answers_in_the_users_language():
    """The dealership works in Hebrew and English; a Hebrew question gets Hebrew back."""
    assert "language the person wrote in" in setup_agent.SYSTEM_PROMPT


# --------------------------------------------------------------------------
# Skill routing — the descriptions are what the platform selects on
# --------------------------------------------------------------------------


def test_skill_descriptions_are_disjoint():
    reporting = _description(setup_agent.REPORTING_SKILL_DIR / "SKILL.md")
    alloc = _description(setup_agent.ALLOC_SKILL_DIR / "SKILL.md")
    assert "Do NOT use for allocation repair" in reporting
    assert "Do NOT use for general reporting" in alloc


def test_reporting_skill_does_not_claim_every_turn():
    """It once said 'use on every turn that names a document type' — alone on its
    own agent that was fine; beside xas-allocation it fires on allocation turns,
    because a VSO *is* a document type."""
    assert "every turn" not in _description(setup_agent.REPORTING_SKILL_DIR / "SKILL.md")


# --------------------------------------------------------------------------
# Bundles: code ships, data does not
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bundle,root",
    [
        (setup_agent.alloc_bundle(), "xas-allocation"),
        (setup_agent.reporting_bundle(), "xas-reporting"),
    ],
)
def test_bundle_has_skill_md_at_its_root(bundle, root):
    assert any(name == f"{root}/SKILL.md" for name, _ in bundle)


def test_alloc_bundle_ships_the_solver():
    names = [n for n, _ in setup_agent.alloc_bundle()]
    assert "xas-allocation/xas_allocation/solver.py" in names


def test_reporting_bundle_ships_the_phrasebook_builder_and_the_taxonomy():
    """The taxonomy is the ONE dataset that ships in a bundle (DECIDE-16) — it is
    static config for the single tenant, and phrasebook.py finds it beside
    itself instead of hunting for a mount."""
    assert [n for n, _ in setup_agent.reporting_bundle()] == [
        "xas-reporting/SKILL.md",
        "xas-reporting/index.md",
        "xas-reporting/phrasebook.py",
    ]


@pytest.mark.parametrize("bundle", [setup_agent.alloc_bundle(), setup_agent.reporting_bundle()])
def test_no_session_dataset_is_bundled(bundle):
    """The pull is mounted per session, so regenerating it needs no redeploy.
    (The taxonomy is the deliberate exception — DECIDE-16.)"""
    for name, _ in bundle:
        assert "pull.json" not in name


# --------------------------------------------------------------------------
# Mounts
# --------------------------------------------------------------------------


def test_the_pull_is_the_only_mount():
    """TWO files since 2026-08-27 — the export's two row streams — and nothing
    else. Reporting used to get a third under /workspace/reports/; it reads the
    live MCP now, so a session that mounts anything more is a session whose
    reporting numbers came from somewhere this design does not control."""
    assert web.MOUNTED_INPUT_FILENAMES == frozenset({web.ORDERS_FILENAME, web.VEHICLES_FILENAME})
    source = (REPO_ROOT / "web.py").read_text(encoding="utf-8")
    assert source.count('"type": "file"') == 2, "two resources, or the fence moved"


def test_every_mounted_input_is_filtered_from_outputs():
    """files.list(scope_id=...) returns the inputs too; handing a planner their
    own pull back as an 'output' is noise, and downloading it is worse."""
    mounted = {Path(p).name for p in alloc_tools.MOUNT_PATHS}
    assert mounted == set(web.MOUNTED_INPUT_FILENAMES)


# --------------------------------------------------------------------------
# The bundled taxonomy (DECIDE-16)
# --------------------------------------------------------------------------


def test_bundled_taxonomy_is_the_real_index():
    bundled = dict(setup_agent.reporting_bundle())["xas-reporting/index.md"]
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
    """Both mounts, and the /mnt/session/uploads prefix the platform was actually
    observed to materialize them under."""
    for path in alloc_tools.MOUNT_PATHS:
        candidates = alloc_tools.mount_candidates(path)
        assert path in candidates
        assert f"{alloc_tools.UPLOAD_PREFIX}{path}" in candidates


def test_flatten_command_tries_every_candidate():
    command = alloc_tools.flatten_command()
    for path in alloc_tools.MOUNT_PATHS:
        for candidate in alloc_tools.mount_candidates(path):
            assert candidate in command, f"{candidate} unreachable by the flatten command"


def test_flatten_command_never_searches_from_root():
    """An unbounded rglob from / once swept the container and killed the shell."""
    command = alloc_tools.flatten_command()
    assert "rglob" in command
    assert "p != root" in command


def test_reporting_skill_counts_with_totalcount_not_by_paging_records():
    """Observed 2026-08-20: asked for last month's job cards by type, the agent
    paged 245 full records into context (~83k tokens, re-read on every later
    turn) to compute ten integers. The filter's `totalCount` was in every
    response. Also pins the +03:00 boundary — filters compare in UTC, so a local
    month asked for naively clips its first three hours. That boundary moved to
    `index.md` on 2026-08-23 (it sits beside the bounds shape, which was only ever
    documented there); the skill must still point at it.

    Refined 2026-08-23: the old wording ("never page through records") also banned
    reading ONE page when the cards themselves were the answer. Walking pages to
    add up a total is still forbidden."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "totalCount" in skill
    assert 'paging: {"count": 1}' in skill
    assert "Never walk pages to compute an aggregate" in skill
    index = (setup_agent.REPORTING_SKILL_DIR / "index.md").read_text(encoding="utf-8")
    assert "CreateDateTime" not in index, (
        "index.md is generated taxonomy — date mechanics hand-maintained there are "
        "dropped by the next dump_taxonomy run, and cost two round trips to find"
    )
    assert "index.md" not in skill.split("## Getting the number")[1], (
        "and nothing about a date may send the agent to the taxonomy to look it up"
    )


def test_reporting_skill_does_not_probe_for_its_own_sake():
    """REVERSED AGAIN 2026-08-27, and this time on measurement. The probe was
    prescribed for every question, naming "the columns you are CONSIDERING". Both
    halves fail: `totalCount` rides on every response, so a card list gets the count
    free from the call it was making anyway; and one row cannot establish field
    presence, because presence varies per card — `PlateNo` was absent from one sales
    order and present on 40 of 40 cards sampled across types, so the probe that
    "discovered" it learned something false.

    What survives is the one case a round trip buys something: you cannot bound the
    page without knowing the size, and 20 rows is a list where 2,000 is a summary.
    Then the key alone, no columns."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "there is no separate probe to run first" in skill
    assert "cannot bound the page without" in skill
    assert "never candidate columns" in skill
    assert "has bought nothing" in skill, "a pure duplicate is still waste"
    assert '"Show me the cards that' in skill, "the rows case keeps its heading"


def test_reporting_skill_sends_the_agent_to_the_mcp_not_to_a_file():
    """Every records path the skill named is gone. One left behind sends the
    agent hunting a mount that does not exist, and the recovery it improvises is
    the live MCP with no mention of where the number came from."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
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


def test_reporting_description_disclaims_the_allocation_vocabulary():
    """ "How many VSOs are late" is a COUNT, which reads like reporting -- and is
    an allocation question. Both descriptions must say so or the platform picks
    on surface form."""
    reporting = _description(setup_agent.REPORTING_SKILL_DIR / "SKILL.md").lower()
    for phrase in ("deliveries", "vso", "vpo", "supply"):
        assert phrase in reporting


def test_alloc_skill_stops_a_status_question_at_the_report():
    skill = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    # "the state report" since 2026-08-30: there are two of them now
    # (`discrepancy_report` and `current_state_report`) and either one is where a
    # status question stops.
    assert "A question about the state stops at the state report." in skill
    assert "no VPO ids" in skill, "the VPO-number limit must be stated, not discovered"


def test_prompt_routes_the_everyday_words():
    prompt = setup_agent.SYSTEM_PROMPT.lower()
    for phrase in ("deliveries", "vehicle purchase order", "what's late"):
        assert phrase in prompt


def test_reporting_skill_has_a_dead_end_rule():
    """A term that resolves to nothing used to be undefined behaviour, so the
    model improvised -- sometimes answering with the closest-looking code, which
    returns a real-looking number nobody can tell is wrong."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Never answer with an unresolved term." in skill
    assert "--suggest" in skill, "the typo rung must be documented or it is never run"


def test_prompt_forbids_answering_an_unresolved_term():
    assert "NEVER answer with a term you could not resolve" in setup_agent.SYSTEM_PROMPT


def test_reporting_reply_keeps_the_procedure_out_of_it():
    """The planner is a dealership scheduler: the reply is the figure and what it
    covers, not a work log. Observed before this rule: answers that opened with
    the phrasebook build, the resolved code and the filtered call, and closed with
    the path a chart was written to — none of which the planner can act on."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "none of it belongs in the reply" in skill
    for internal in ("phrasebook", "totalCount", "no file path, no filename"):
        assert internal in skill.split("## Presenting the answer")[1]
    prompt = setup_agent.SYSTEM_PROMPT
    assert "no file paths, no filenames" in prompt
    assert "not a work log" in prompt


def test_between_tool_calls_the_agent_says_nothing():
    """Observed 2026-08-23: the reply was clean, but the turn still shipped
    "Let me check the timeframe first", "28 Service cards — small", and "all 16
    buckets sum to 28 — the split is clean". Every line between tool calls reaches
    the planner, so the rule has to cover the whole turn, not just the answer.

    Moved 2026-08-27 into its own `## The channel` block ahead of the work, after
    the same rule was broken twice more from under the "Presenting the answer"
    heading — see test_reporting_skill_states_the_channel_before_the_output_contract."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Every character you emit reaches the planner" in skill
    prompt = setup_agent.SYSTEM_PROMPT
    assert "there is no working-notes channel" in prompt
    for banned in ("running totals", "point at your own output"):
        assert banned in prompt


def test_agent_does_not_report_where_the_chart_was_written():
    """The browser renders the chart with its filename as a caption, so naming the
    file (or its directory) in the reply is plumbing the planner already sees."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "say the filename in your reply" not in prompt
    assert "Not the filename, not the directory" in prompt
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Not the filename, not the directory" in skill


def test_phrasebook_reads_the_taxonomy_beside_itself():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "phrasebook", setup_agent.REPORTING_SKILL_DIR / "phrasebook.py"
    )
    phrasebook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(phrasebook)
    assert phrasebook.INDEX_PATH == (setup_agent.REPORTING_SKILL_DIR / "index.md").resolve()
    assert phrasebook.default_index() is not None


# --------------------------------------------------------------------------
# Charts reaching the planner's screen
# --------------------------------------------------------------------------

OUTPUTS_DIR = "/mnt/session/outputs"


def test_agent_is_told_where_charts_must_go():
    """Only /mnt/session/outputs is captured by the Files API. A chart written
    anywhere else runs successfully and is seen by nobody."""
    assert OUTPUTS_DIR in setup_agent.SYSTEM_PROMPT
    assert OUTPUTS_DIR in (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_agent_is_told_not_to_read_the_chart_back():
    """Reading a PNG back returns base64 -- ~100KB of context for no new information."""
    prompt = setup_agent.SYSTEM_PROMPT.lower()
    assert "do not read the chart back" in prompt
    assert (
        "do not read the chart back"
        in (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").lower()
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
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
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


def test_reporting_skill_sends_fields_on_every_call():
    """The MCP renamed its six tools on 2026-08-27 and documents `fields` itself:
    a response carries its salient fields whether the answer uses them or not, and
    they stay in context for the session. Prose alone did not hold — the agent
    copies the calls table — so the table itself carries `fields`, and a count asks
    for the key alone because it only ever reads `totalCount`."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Every row sends `fields`" in skill
    assert 'fields: ["DMSJCEntry"]' in skill, "the count row must ask for the key alone"
    assert "A count needs no fields" in skill


def test_reporting_skill_says_an_absent_field_is_not_an_empty_value():
    """`fields` NARROWS and cannot widen: a name the tool does not return is
    dropped in silence — no error, no empty value. Verified live 2026-08-27, asking
    11 vehicle fields and getting 2 back. Without this rule the agent reports "no
    promised date" as a business fact when the field was simply never projected —
    the reporting-side twin of `meta.projection_gaps`."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "narrows; it cannot widen" in skill
    assert "an absent field is not an empty value" in skill.lower()
    assert "NEVER a business fact" in skill


def test_reporting_skill_states_the_channel_before_the_output_contract():
    """Observed 2026-08-27: two mid-turn messages reached the planner — "Let me find
    those tied to Daniil" and "Let me confirm the account filter actually works" —
    naming an internal code and a filter. The rule existed, filed under "Presenting
    the answer", which is not what the agent believes it is doing two calls deep. It
    is now a standing fact about the channel, stated before any of the work."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "## The channel" in skill
    head = skill.split("## Getting the number", 1)[0]
    assert "## The channel" in head, "it must come before the work, not after it"
    assert "It is talking" in skill


def test_reporting_skill_keeps_a_stored_name_whole():
    """Observed 2026-08-27: the account `Daniil123` was reported as "Daniil (account
    123)" — a name nobody stored beside a code the planner may not see. It started a
    turn earlier with a table column of account codes, which is the same rule broken
    as a column rather than a sentence."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "A stored name is ONE string" in skill
    assert "Daniil123" in skill, "name the observed failure, not the abstraction"
    assert 'A column headed\n  "Code" breaks this' in skill


def test_reporting_skill_establishes_before_it_narrows():
    """Observed 2026-08-27: "find all service leads of Daniil" cost five calls and
    three rounds. Two unproven clauses went out together, both returned 0, and the
    rest of the turn worked out which clause was responsible — two control calls
    pulling 50 rows each to read a number off totalCount.

    The ordering is steps 1-2 of one numbered procedure rather than its own heading,
    so nothing has to say how the two compose."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    steps = [
        line for line in skill.splitlines() if line.startswith(("**1. ", "**2. ", "**3. ", "**4. "))
    ]
    assert len(steps) == 4, f"the procedure must stay four ordered steps, saw {steps}"
    assert "Pin down what the question is about" in skill
    assert "carries NO information" in skill
    assert "ONE control call" in skill
    assert "get_account_list" in skill, "a fresh name resolves as an account"


def test_reporting_skill_does_not_demand_a_classification_with_a_status():
    """Checked against all 109 STATUS entries on 2026-08-27: no id carries more than
    one name and no name maps to more than one id, so a status id is unambiguous on
    its own. Rule 4 used to say "always send the classification with it", which
    contradicted rule 5 ("never a call per classification") and would have turned one
    call for "how many open cards" into eight. What is true is that one id SPANS
    classifications, so the count covers every card type and the answer must say so."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "always send the classification with it" not in skill
    assert "an id and its name are 1:1" in skill
    assert "classification only when the planner asked for one" in skill


def test_reporting_skill_translates_codes_on_the_way_out():
    """Observed 2026-08-27: a card table reached the planner reading VRV / VSO /
    Service. The agent had never run phrasebook.py — neither question needed a term
    resolved going IN, and the skill framed the taxonomy as an input tool only, so
    step 0 had no trigger and the procedure ended at fetching rows. The taxonomy is
    now stated as bidirectional, step 0 is unconditional, and translation is step 4:
    a code that will not resolve is named as unresolved, never printed bare."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "in BOTH directions" in skill
    assert "Build it whether or not the question has a term in it" in skill
    assert "Translate every code before you print it" in skill
    assert "NAMED as unresolved" in skill
    assert "once per session" not in skill, "step 0 must not read as conditional"


def test_skill_requires_the_exclusion_census_on_turn_one():
    """Real data is patchy: a sales order with no model on it cannot be matched
    to a car, so a plan may cover a handful of the book. Presenting that as the
    whole book is the worst failure this change can produce, and the only thing
    stopping it is the prose — nothing structural forces the agent to mention it.
    """
    skill = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text()
    assert "exclusion_note" in skill
    assert "Never present it as the whole book" in skill
    # and it must be excluded from the "stays internal" suppression list
    assert "must always be reported" in skill


def test_skill_offers_a_report_for_the_whole_book():
    """ "Show me all the allocations" is a planner's first question. With no helper
    named for it the agent scripts over `snapshot.json` and hand-builds the table
    — the exact re-derivation this skill exists to forbid, and the one that put a
    false claim about free cars in front of a planner."""
    skill = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text()
    assert "current_state_report" in skill
    assert "There IS a report for the whole book, so you never build one." in skill
    # the API block must actually offer it, and count itself correctly
    assert "S.current_state_report(snap)" in skill
    assert "The whole API is four calls" in skill


def test_skill_forbids_retyping_a_printed_table_as_bullets():
    """The rule was already there and was broken anyway, in the one shape it did
    not name: the rows re-listed as bullets, one message after the planner read
    them."""
    skill = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text()
    assert "**A list of bullets is a table.**" in skill


def test_skill_names_the_eligibility_rule_and_its_hardness():
    """Eligibility is exact model equality. A skill that suggests a near match is
    a skill that invites the agent to offer a car nobody can have."""
    skill = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text()
    assert "matched exactly" in skill
    assert "no near-match and no substitution" in skill


def test_skill_separates_the_promise_from_the_arrival():
    """The one confusion that makes nothing ever late: the promise is the ORDER's
    date, the arrival is the CAR's."""
    skill = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text()
    assert "**The promise** is the date on the ORDER" in skill
    assert "**The arrival** is the date on the CAR" in skill
    # and the MCP field names must be gone with the MCP
    for gone in ("DueDateTime", "AvailableBy", "ModelId.Code", "JobKey", "LineNum"):
        assert gone not in skill, f"{gone} is app-MCP vocabulary; the pull is CSV now"


def test_prompt_says_a_bump_authorisation_lasts_one_turn():
    """`may_move.also` is the only key that expires. If the prompt does not say
    so, the agent carries the permission forward like everything else and a later
    turn displaces a settled order on the strength of one old sentence."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "`may_move.also`" in prompt
    assert "`true` for anyone" in prompt
    assert "permission is for ONE turn" in prompt
    assert "session.carry_forward" in prompt


def test_skill_gates_every_repair_behind_the_preferences_question():
    """A repair the planner never stated their preferences for is a plan that
    silently invents them — every order equal, nothing protected. Nothing
    structural can force the ask, so the skill must state it as a rule, name the
    three things to ask about, and say that "fix it" is not an answer to it."""
    skill = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text()
    assert "## Before you repair — ask what matters, every time" in skill
    assert "Never suggest, offer or run a repair before asking the planner" in skill
    assert "none of them is an answer to this question" in skill
    # the three levers the answer compiles into
    for lever in ("`priority`", "`may_move.never`", "`churn_price`"):
        assert lever in skill
    # and the ask must precede the solve, not follow it
    assert "do not solve first" in skill


def test_prompt_gates_every_repair_behind_the_preferences_question():
    """The skill body can be summarised; the prompt is always in context. The
    gate is worth stating twice."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "NEVER offer or run a repair before ASKING the planner what matters" in prompt
    assert "A client can hold several orders" in prompt
    assert "you may never assume it" in prompt


def test_skill_can_answer_in_client_terms_but_steers_on_ids():
    """The export carries `customer.name` and the pull now keeps it, so a planner
    may answer the preferences question with a client rather than an id. The
    label is NOT a solver dimension (that went on 2026-08-27), so the skill must
    say the agent groups orders by client itself and confirms the ids it used —
    a client with three orders and two of them named is half-prioritised."""
    skill = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text()
    assert "Every order also carries the client it is for" in skill
    assert "It is a LABEL" in skill
    assert "resolve it yourself to every order" in skill
    assert "there is no model-wide or client-wide lever" in skill


# --- The planner channel (web.py forwards the solver's marked reports) --------
# `_render` drops builtin tool results as sandbox chatter. That is what forced the
# agent to retype every table into its own reply — two copies of one table in the
# conversation, and every retype a chance to lose a row. A marked span is the
# exception: the solver's reports are already written for the planner.


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


def test_the_skill_tells_the_agent_to_wrap_planner_prints():
    body = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "show(S." in body, "the skill must show the agent how to reach the planner"


def test_the_skill_forbids_retyping_a_table_the_planner_has_seen():
    """The double-copy rule. Prose is the whole mechanism, so pin the prose."""
    lowered = (setup_agent.ALLOC_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").lower()
    assert "already seen" in lowered
    assert "do not repeat the table" in lowered
