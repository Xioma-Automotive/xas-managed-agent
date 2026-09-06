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
import phrasebook
import setup_agent
import web

REPO_ROOT = Path(__file__).resolve().parent.parent


def _flat(text: str) -> str:
    """Collapse whitespace: a pinned phrase must survive a re-wrap of the prose."""
    return " ".join(text.split())


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


def test_prompt_stops_claiming_there_is_no_network():
    """It said 'No network access — everything is local', which is false: the
    reporting lane reaches the live dev system through `xas-app-mcp`. The clause
    naming that exception was cut on 2026-09-01 as a duplicate of the Reporting
    section; what must never come back is the false claim itself."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "No network access — everything is local." not in prompt


# --------------------------------------------------------------------------
# The rule that keeps the two lanes from contaminating each other
# --------------------------------------------------------------------------


def test_prompt_names_no_records_mount():
    """The reporting lane reads the live MCP now. A path the host does not mount
    sends the agent looking for a file that is not there -- which is exactly how
    it silently substituted the live system for the records."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "/workspace/reports" not in prompt
    assert "jobcards.json" not in prompt


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


def test_reporting_bundle_ships_the_taxonomy_inside_the_skill():
    """The taxonomy is the ONE dataset that ships in a bundle (DECIDE-16), and
    since 2026-09-06 it ships INSIDE SKILL.md rather than as a table beside a
    matcher: it is rendered host-side into the file the agent already reads, so
    there is no separate artifact, no round trip to search it, and nothing that
    could resolve a word at all. index.md is the source and stays in the repo."""
    assert [n for n, _ in setup_agent.reporting_bundle()] == [
        "xas-reporting/SKILL.md",
        "xas-reporting/charts.md",
        "xas-reporting/dates.py",
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


def test_the_bundled_skill_carries_the_whole_taxonomy_rendered():
    """Rendered from the committed index into the skill, not hand-written.

    Everything a lookup could have answered is 46 records, and writing them out
    costs 1,051 tokens against 489 for ONE `--lookup` call and 1,070 for the one
    `--list` call a breakdown needed. So the types, the statuses, the states and
    the branches are all IN the file the agent reads on its first reporting turn,
    and `resolve.py` / `phrasebook.tsv` are gone with the round trip they cost."""
    skill = dict(setup_agent.reporting_bundle())["xas-reporting/SKILL.md"].decode()
    assert "- Open 6530d9a89c098a33be3e0c73 [In Process]" in skill
    assert "- In Stock `03`" in skill
    assert "- Main 69f07fdaf930e4ee6d524dc1" in skill
    assert "`Service` Vehicle Service Order" in skill
    assert "there is nothing to look up" in _flat(skill).lower()


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


def test_nothing_in_the_sandbox_can_resolve_a_word():
    """The deletion IS the rule. With no table and no matcher there is no way to
    look a classification up — which was the thing prose kept failing to prevent —
    and no round trip between a question and its answer. `dates.py` is the only
    command left, and it computes rather than searches."""
    assert not (setup_agent.REPORTING_SKILL_DIR / "resolve.py").exists()
    assert not (setup_agent.REPORTING_SKILL_DIR / "phrasebook.tsv").exists()
    shipped = dict(setup_agent.reporting_bundle())
    assert [n for n in shipped if n.endswith(".py")] == ["xas-reporting/dates.py"]


def test_the_taxonomy_parser_does_not_ship():
    """It cannot run there — index.md is not in the bundle — and a `main()` that
    looks like it rebuilds the table is an invitation to rebuild the table,
    which is the turn this change removed."""
    shipped = dict(setup_agent.reporting_bundle())
    assert "xas-reporting/phrasebook.py" not in shipped
    for name, blob in shipped.items():
        assert b"ENTITY|CLASSIFICATION|STATUS" not in blob, f"{name} carries the index parser"
    assert phrasebook.INDEX_PATH == (setup_agent.REPORTING_SKILL_DIR / "index.md").resolve()


def test_the_first_block_fires_the_skill_read_and_the_dates_together():
    """A date range is a convention, not a judgment — working one out per turn cost
    two bash calls and ~20s and landed on UTC midnight for a UTC+3 dealership. So
    `dates.py` is a command, and it lives in the PROMPT: it fires in the same block
    as the skill read, before the skill has landed, so a command that lived in the
    skill could not run until a round trip later.

    Observed 2026-09-06 (`sthr_01LsdNCY6pMrNhF1peMGvyXU`): the agent read the skill
    and resolved terms together, then spent a SECOND round trip on the date range,
    because the one-block rule named the read and the lookup but not the dates.
    The lookup is gone entirely now, so the block is exactly two things and both
    are named."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "dates.py" in prompt
    assert "goes in ONE block, never a round trip each" in _flat(prompt)
    assert "read the `xas-reporting` skill" in prompt
    assert "turn every named period into a date range" in prompt
    assert "Never work one out yourself" in prompt


def test_a_when_word_is_a_date_and_never_a_status():
    """One of the two wasted lookups in that trace, and the half that survives the
    lookup's deletion: `opened` found nothing, but the hedge `open` came back as
    the Open STATUS, which counts a different set entirely. The decision is taken
    in the first block, before the skill has landed, so the rule is the prompt's.
    (The other half — `job card` is the entity, not a type — is now structural: the
    skill's own type list says the entity word takes no filter.)"""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "A word about WHEN is a date, not a status" in prompt
    assert '`Open` is a status; "opened" is a date' in prompt


def test_the_prompt_holds_no_taxonomy_of_its_own():
    """One vocabulary, one place. It moved into the skill on 2026-09-06 because it
    is only wanted once a reporting question is already being answered, and by then
    the skill has landed in the same block — while the allocation lane pays for
    every line of this prompt and can never use a status id."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "JobClassification" not in prompt
    assert "6530d9a89c098a33be3e0c73" not in prompt
    assert "{{" not in prompt


# --------------------------------------------------------------------------
# Charts reaching the planner's screen
# --------------------------------------------------------------------------

OUTPUTS_DIR = "/mnt/session/outputs"


CHARTS_MD = "charts.md"


def _charts() -> str:
    return (setup_agent.REPORTING_SKILL_DIR / CHARTS_MD).read_text(encoding="utf-8")


def test_agent_is_told_where_charts_must_go():
    """Only /mnt/session/outputs is captured by the Files API. A chart written
    anywhere else runs successfully and is seen by nobody. The prompt half went
    with the minimal pair (2026-09-06), which says nothing about charts at all:
    the skill names the recipe and the recipe names the directory."""
    assert OUTPUTS_DIR in _charts()


def test_the_chart_recipe_is_a_file_of_its_own_that_the_skill_points_at():
    """Charts fire on a minority of reporting turns, so the recipe is not paid for
    on the first turn of every session — but a rule the agent has to fetch is a rule
    it can skip, so SKILL.md must name the file. The prompt half went with the
    minimal pair (2026-09-06); the skill is now the only thing that points here."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "charts.md" in skill
    assert "matplotlib" not in skill, "the recipe lives in one place"
    assert f"xas-reporting/{CHARTS_MD}" in dict(setup_agent.reporting_bundle())


def test_agent_is_told_not_to_read_the_chart_back():
    """Reading a PNG back returns base64 -- ~100KB of context for no new information."""
    assert "do not read the chart back" in _charts().lower()


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
    charts = _charts()
    assert "self-contained" in charts.lower()
    assert 'format="svg"' in charts, "the recipe must save SVG, not PNG"
    assert "matplotlib.use" in charts, "no display in the sandbox — Agg backend required"
    assert "never reference a cdn" in charts.lower()


def test_html_charts_are_framed_not_trusted():
    """A chart is model-generated HTML. It renders in an opaque origin so it
    cannot reach this page, its cookies, or the session routes."""
    ui = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    frame = ui[ui.index('<iframe class="output-frame"') :][:200]
    assert 'sandbox="allow-scripts"' in frame
    assert "allow-same-origin" not in frame


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
