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


def test_prompt_says_where_the_taxonomy_lives():
    """It is no longer a mount (DECIDE-16), so the prompt must send the agent to
    the skill directory instead of a path that does not exist — and to the TABLE,
    not the index it was built from, which no longer ships."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "`phrasebook.tsv` ships inside the `xas-reporting` skill directory" in prompt
    assert "/workspace/reports/index.md" not in prompt
    assert "index.md" not in prompt


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


def test_reporting_bundle_ships_the_built_table_not_the_index():
    """The taxonomy is the ONE dataset that ships in a bundle (DECIDE-16), and it
    ships BUILT: rendering it host-side spends no sandbox turn on a file that is
    byte-identical every run and that the agent cannot change. index.md is the
    source, kept in the repo — shipping it too would only offer a second copy of
    the taxonomy to read."""
    assert [n for n, _ in setup_agent.reporting_bundle()] == [
        "xas-reporting/SKILL.md",
        "xas-reporting/charts.md",
        "xas-reporting/dates.py",
        "xas-reporting/link.py",
        "xas-reporting/phrasebook.tsv",
        "xas-reporting/resolve.py",
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


def test_bundled_table_is_the_real_taxonomy_rendered():
    """Rendered from the committed index, not hand-written: the header legend
    first, then the rows the skill greps."""
    bundled = dict(setup_agent.reporting_bundle())["xas-reporting/phrasebook.tsv"]
    lines = bundled.decode().splitlines()
    assert lines[0].split("\t")[0] == "normalized"
    assert any(line.startswith("service\tService\tcode\tclassification") for line in lines)
    assert len(lines) > 300, "the whole taxonomy, not a fragment"


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
    assert "index.md" not in skill.split("## Answering a question")[1], (
        "and nothing about a date may send the agent to the taxonomy to look it up"
    )


def test_three_sources_and_the_prompt_names_them_the_same_way():
    """The prompt said filter keys come from the taxonomy; the skill's rule 10 said
    the taxonomy holds no field names and a KEY comes from the recipes — and rule
    10's own heading said both. Whichever half the agent believed, one of them sent
    it to a source that cannot answer, which is the shape of the 2026-08-31 guess.

    Three sources, each supplying exactly one thing: the tool says what you may SEE,
    the taxonomy supplies VALUES, the recipes supply KEYS."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    prompt = setup_agent.SYSTEM_PROMPT
    sources = _flat(skill.split("# XAS reporting")[1].split("## The phrasebook")[0])
    assert "Filter VALUES come from the phrasebook" in sources
    assert "filter KEYS from **The calls**" in sources
    assert "which columns you may SEE" in sources and "`fields` list" in sources
    assert "Never take a filter — key or value — from a tool's `fields` list" in sources, (
        "the ban is what the three-way split exists to support"
    )
    assert "VALUES come from the taxonomy and filter KEYS from the skill's recipes" in prompt
    assert "Filter keys and values come from the taxonomy" not in prompt, (
        "the taxonomy holds no filter keys — sending the agent there for one is a dead end"
    )


def test_the_app_link_is_the_one_path_both_sides_allow():
    """The skill mandates a URL at the end of every answer about records; the prompt
    names the link too, because the prompt is what survives a summary of the skill.
    The prompt's own no-plumbing ban was cut on 2026-09-01, so the link rule now
    stands on its own rather than as an exception to it."""
    prompt = setup_agent.SYSTEM_PROMPT
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "those are the only paths you may ever print" in prompt
    assert "The app link is the one" in skill


def test_every_named_record_is_a_link_in_the_shape_the_app_routes():
    """A record the live tools returned is named as a link to its own page, always —
    not only as the one set link that closes an answer. The three shapes are the app's
    own routes (`app/src/routes/index.tsx`), and each pairs a LABEL the planner reads
    with a different field as the TARGET: the job number shows, the entry id routes.
    Get that backwards and the planner is shown an id they have never seen."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "[105374](/job_cards/8333)" in prompt
    assert "[12-345-67](/vehicles/11370)" in prompt
    assert "[Delek Motors](/accounts/6a9144209004759d555d03f1)" in prompt
    assert "written by you from the id on the record itself" in prompt, (
        "a detail page is a path and an id — no filter, so nothing to encode wrong"
    )
    assert "the skill builds it and you never type or edit it" in prompt, (
        "the SET link carries a filter, and a raw `$` in one empties the page"
    )


def test_the_skill_pairs_a_label_with_the_id_that_routes():
    """Naming a record is a link, and the two halves come off DIFFERENT fields: the
    job number is what the planner knows the card by, the entry is what the page
    routes on. The customer's id is the one nobody would guess — a card carries its
    owner's account `Id` under `AccountUUID`, so a list already pulled links its
    customers with no second call."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Every record you name is a link to its own page — write it yourself" in skill
    assert "`Accounts.Owner.AccountUUID` IS that account's `Id`" in _flat(skill)
    for field in ("DMSJCEntry", "VehicleCode", "JobEntryNum", "LicenseNumber"):
        assert field in skill
    assert "Never hand-write or edit a SET link" in skill, (
        "the encoding hazard is the filter, and only the set link carries one"
    )


def test_a_named_list_stops_at_twenty_and_the_link_carries_the_rest():
    """Observed 2026-09-01: "which vehicles does Hertz hold" came back as 63 linked
    rows — the table the set link already opens, printed anyway, and re-read on every
    later turn. "Every entry linked" had no ceiling, so the longer the answer the more
    faithfully the rule was followed. Both sides carry the cap, because the prompt is
    what survives a summary of the skill."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "Name at most TWENTY records in one answer" in prompt
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    # Said TWICE, not three times (2026-09-02): the prompt, which survives a summary
    # of the skill, and the row where the decision is taken. The bullet that carried
    # a third copy two paragraphs above that row was 491 characters read on every
    # reporting session to repeat what the row already said.
    assert "up to TWENTY entries linked, how many more there are" in skill, (
        "the named-column row is where the decision is taken"
    )
    assert "Twenty is a ceiling, not a target" in _flat(skill), (
        "three matches print three — the cap must not pad an answer out to twenty"
    )


def test_a_tally_is_one_page_at_the_servers_maximum():
    """Observed 2026-08-31: a tally of 51 cards asked for 50, then spent a whole
    round trip on page 2 — 17 seconds of a 45-second turn — to collect one card
    whose customer was already in the list. "Never walk pages to compute an
    aggregate" already forbade that call, but it sits in a later paragraph than the
    bullet where the decision is taken, and 50 was our own number: the server's
    maximum is 200, so the shortfall need not have arisen at all.

    200 rows is a token cost, not a free win, so the rule says what bounds a page —
    bytes — and names the `Accounts.*` fields, which arrive as whole owner objects
    (~175 tokens a row here, contact details included) rather than one value.

    And a page short of `totalCount` is a SAMPLE. On 2026-09-01 the vehicles turn
    pulled 200 of 1,334 cars and there is no breakdown in those rows at any price;
    the old wording said "too big to tally, loop the buckets instead", which reads as
    a routing hint rather than as "what you are holding cannot answer this"."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    tally = next(
        l for l in skill.splitlines() if l.startswith("| Breakdown on anything it does not")
    )
    assert '"count": 200' in tally, "the tally page must be the server maximum"
    assert "never page a tally" in tally.lower()
    assert "BYTES" in tally and "`Accounts.*`" in tally, "200 of a fat field is not the same page"
    assert "is a SAMPLE and holds no tally at all" in tally, (
        "a short page is not a smaller answer, it is no answer"
    )
    assert '{"count": 50}' not in skill, "no recipe may still prescribe the old page"


def test_bucket_looping_has_no_cap_and_names_the_single_block():
    """Measured on 2026-09-01 (session sesn_01Ar2oFNgj7nskxibNPLNuTS, "what inventoy
    vehcles we have by status?"): twelve parallel `count: 1` calls answered the whole
    1,334-car fleet exactly in ONE round trip for ~9k characters — and then the same
    turn ALSO pulled 200 rows (34,173 characters) that contributed nothing to the
    answer and could not have, being a sample.

    The cap is what invited that second call: the table read "up to 5 buckets" for the
    loop and "more than 5" for a hand tally, so a twelve-bucket question was routed to
    rows by the skill's own words. Nothing about a `count: 1` call gets more expensive
    at the sixth bucket — what matters is whether the bucket VALUES can be enumerated
    at all, which is a property of the phrasebook and not of their count."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    buckets = next(
        l for l in skill.splitlines() if l.startswith("| Breakdown on anything the phrasebook")
    )
    assert "NO cap on how many" in buckets, "the cap is what routed twelve buckets to rows"
    assert "in a SINGLE block" in buckets, "twelve calls must cost one round trip, not twelve"
    assert '"count": 1' in buckets and "WHOLE set" in buckets
    assert "IS the answer once every bucket returns" in buckets, (
        "the buckets ARE the breakdown — the vehicles turn re-queried the full set after them"
    )
    assert "do not re-query the full set" in buckets
    assert "up to 5 buckets" not in skill and "more than 5 buckets" not in skill, (
        "no recipe may still split the breakdown at five"
    )


def test_operators_do_not_nest_on_the_vehicle_lane():
    """Observed 2026-09-01 in the vehicles turn: reaching for the residual bucket, the
    agent sent `{"status.code": {"$not": {"$in": [...]}}}` and got a 500 — "Cast to
    string failed for value {'$in': [...]}". The vehicle/account lane runs a filter
    through an adapter that re-wraps a bare value by the tenant's field TYPE, so it
    meets an operator object where it expects a scalar.

    The rule is about NESTING, not about a list of banned operators. `$in` works (it is
    what `link.py` builds for that lane, and `$in: [null]` is what finally counted the
    611 statusless cars) and `$like` works on a name — a rule banning those would send
    the agent looping buckets it could have filtered in one call, which is the same
    round trip wasted in the other direction. `$nin` and `$regex` are untested here and
    so go unmentioned: this file states what was measured.

    The residual needed no operator at all. Twelve bucket counts and the total were
    already in hand, and 1,334 - 723 = 611 is subtraction."""
    skill = _flat((setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
    assert "Operators do not nest" in skill
    assert "500 Cast to string failed" in skill, "a loud failure is worth distinguishing from a 0"
    assert "gives the residual by subtraction" in skill
    assert "$nin" not in skill and "$regex" not in skill, (
        "never ban an operator nobody measured — a false ban costs a round trip too"
    )


def test_rows_are_for_display_or_an_unnameable_key_and_one_page_is_still_rows():
    """The general rule ("never walk pages to compute an aggregate") did not stop the
    vehicles turn, because one page of 200 does not read as walking pages. So the row
    path now states its own two reasons at the point of decision, and says outright
    that a single page is still a row pull.

    "Pull rows only to DISPLAY records" would be too strong on its own: grouping by
    customer has no bucket list to loop, so rows are the only route there and the cap
    is what makes it answerable or not."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    rows = next(
        l for l in skill.splitlines() if l.startswith("| Breakdown on anything it does not")
    )
    assert "Rows are for two things only" in rows
    assert "ONE page of 200 is still pulling rows" in rows
    assert "cannot be named in advance" in rows, "the tally case must survive the rule"


def test_an_inline_result_is_counted_in_the_model_not_retyped_into_bash():
    """An inline result is already in the context window, and re-emitting it into a
    bash command pays for the payload a second time in OUTPUT tokens — the vehicles
    turn's 34,173-character result would have cost ~90 seconds to retype against ~15
    seconds to read.

    The skill used to carry the other half of this too: past ~100,000 characters the
    platform writes a tool's output to a file and returns a preview plus the path, so
    THOSE rows are code work. Cut on 2026-09-02, because nothing we send can land on
    that side of the cliff — the 200-row page cap keeps every reporting response
    below it, so those six lines were read on every session to describe a branch that
    cannot be reached. Reinstate them the day a call is allowed to return more."""
    skill = _flat((setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
    assert "Rows that arrive INLINE are already in front of you: count those yourself" in skill
    assert "pays for the whole payload a second time" in skill, (
        "the reason is the rule: without it, retyping looks free"
    )


def test_prompt_carries_the_lookup_command_so_it_can_ride_with_the_skill_read():
    """The skill read is a round trip of its own — ~9s and 17k tokens on the first
    reporting turn of every session — and the block that follows it is a taxonomy
    lookup for words taken from the planner's question, not from the procedure. So
    the two go together.

    PERMISSION WAS NOT ENOUGH (measured 2026-09-01 over 8 live sessions). The prompt
    already said the lookup MAY ride along, and named `resolve.py --lookup` — but the
    runnable command, with its path and its many-wordings-at-once form, lived only in
    SKILL.md. So the agent could not fire it until it had read the skill, and every
    session spent two serial round trips (~5-11s) before touching data; not one of
    the eight rode along. The invocation therefore lives HERE, where it is readable
    before the skill arrives, and the skill keeps only how to read the result — which
    is not needed until the result is in hand, by which point both have landed.

    What the rule fences is still the 2026-08-31 failure: a FILTER fired in the same
    block as the read, before the procedure it was fetching had arrived. A lookup
    cannot come back wrong; a filter can. Keep both halves — dropping the second
    reopens the hole, dropping the first pays for the round trip again."""
    prompt = setup_agent.SYSTEM_PROMPT
    # Restructured 2026-09-01 into an ordered FIRST/THEN, so the rule spans lines:
    # scope to the Reporting section rather than to one line of it.
    rule = prompt.split("\nReporting\n")[1]
    assert "BEFORE the first `xas-app-mcp` call" in rule, "the ban is on a tool call, not a grep"
    assert "RIDES IN THAT SAME BLOCK" in rule, "riding along is an instruction, not a permission"
    assert "never in a round trip after it" in rule
    assert "a lookup cannot come back wrong, a filter can" in rule

    # The command must be runnable from the prompt alone — path included.
    assert "python /workspace/skills/xas-reporting/resolve.py --lookup" in prompt, (
        "an agent that must read the skill to learn the command cannot fire it in the "
        "same block as that read"
    )

    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "resolve.py --lookup" not in skill, (
        "the invocation lives in the prompt only — a second copy is a second place to "
        "drift, and the skill copy is the one that arrives too late to be used"
    )
    assert "| A block reading | You do |" in skill, (
        "reading the result stays in the skill: it is not needed until the result is "
        "in hand, and the prompt is paid by the allocation lane too"
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
    assert "can you bound the page without" in skill
    assert "has bought NOTHING" in skill, "the wasted size check must be named as waste"
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


def test_the_bucket_list_command_sits_where_the_loop_is_decided():
    """`--lookup` answers what a word MEANS; `--list` answers what the values ARE.
    Without the second, a session invents status names, looks each guess up, and
    still misses the one it did not think of -- measured live on 2026-09-02, three
    round trips that never reached `99 Disabled`.

    The command belongs in the row where the loop is DECIDED, not in a recipes
    section further down: the awk one-liner it replaces lived in such a section
    and was cut on 2026-09-01 without anything noticing it was load-bearing. It
    stays in the SKILL rather than the prompt because, unlike the lookup, it is
    never wanted before the skill has landed -- and the allocation lane pays for
    every line of the prompt."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    loop_row = next(
        line for line in skill.splitlines() if "call PER bucket" in line and "|" in line
    )
    assert "resolve.py --list" in loop_row, (
        "a command in a different paragraph from the decision is a command that does not get run"
    )
    assert "never from your own memory" in loop_row


def test_reporting_skill_has_a_dead_end_rule():
    """A term that resolves to nothing used to be undefined behaviour, so the
    model improvised -- sometimes answering with the closest-looking code, which
    returns a real-looking number nobody can tell is wrong."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Never answer with an unresolved term." in skill
    assert "works the ladder" in skill, "the ladder must be documented or its result is misread"
    for rung in ("nearest entries, CONFIRM", "ask the user"):
        assert rung in skill, f"the reply to `{rung}` is what the agent has to act on"


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


def test_agent_does_not_report_where_the_chart_was_written():
    """The browser renders the chart with its filename as a caption, so naming the
    file (or its directory) in the reply is plumbing the planner already sees."""
    prompt = setup_agent.SYSTEM_PROMPT
    assert "say the filename in your reply" not in prompt
    assert "Not the filename, not the directory" in prompt
    assert "Not the filename, not the directory" in _charts()


def test_the_shipped_resolver_reads_the_table_beside_itself():
    """The skill file must stand alone: it finds the table through __file__ and
    knows nothing about this repo, because in the sandbox there is no repo."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "resolve", setup_agent.REPORTING_SKILL_DIR / "resolve.py"
    )
    resolve = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolve)
    assert resolve.PHRASEBOOK_PATH.name == "phrasebook.tsv"
    assert resolve.PHRASEBOOK_PATH.parent == setup_agent.REPORTING_SKILL_DIR.resolve()
    assert not hasattr(resolve, "build"), "the taxonomy parser is host-side only"


def test_the_taxonomy_parser_does_not_ship():
    """It cannot run there — index.md is not in the bundle — and a `main()` that
    looks like it rebuilds the table is an invitation to rebuild the table,
    which is the turn this change removed."""
    shipped = dict(setup_agent.reporting_bundle())
    assert "xas-reporting/phrasebook.py" not in shipped
    for name, blob in shipped.items():
        assert b"ENTITY|CLASSIFICATION|STATUS" not in blob, f"{name} carries the index parser"
    assert phrasebook.INDEX_PATH == (setup_agent.REPORTING_SKILL_DIR / "index.md").resolve()


def test_reporting_skill_builds_nothing_at_session_start():
    """Step 0 was `python phrasebook.py`, every session, to produce a file that is
    byte-identical every run: ~6s and a whole model turn before the first lookup.
    The table ships built, so the skill must not ask for one to be made — and it
    must not send the agent to an index that no longer reaches the sandbox."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "index.md" not in skill
    assert "Step 0" not in skill
    for build_it in ("python phrasebook.py", "python resolve.py --build", "Step 0"):
        assert build_it not in skill, f"nothing may tell it to build the table ({build_it})"
    assert "/workspace/skills/xas-reporting/phrasebook.tsv" in skill
    assert "there before your first" in skill


def test_reporting_skill_says_the_account_sections_are_previews():
    """`get_account_details(include=["jobCards"])` returns 10 rows whatever the
    total (401 for one account here), takes no paging and no `fields`. The agent
    reached for it twice — once per trace — because the tool reads as though it
    returns the account's cards. Costly, and the truncation is the real risk:
    10 of 401 presented as a customer's history."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "sections are PREVIEWS" in skill
    assert "never `get_account_details`" in skill


def test_reporting_skill_resolves_periods_with_the_helper():
    """A date range is a convention, not a judgment. Working it out per turn cost
    two bash calls and ~20s, and landed on UTC midnight for a UTC+3 dealership."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "dates.py" in skill
    assert "Never work a date range out yourself" in skill
    assert "The tool documents the range shape" not in skill


# --------------------------------------------------------------------------
# Charts reaching the planner's screen
# --------------------------------------------------------------------------

OUTPUTS_DIR = "/mnt/session/outputs"


CHARTS_MD = "charts.md"


def _charts() -> str:
    return (setup_agent.REPORTING_SKILL_DIR / CHARTS_MD).read_text(encoding="utf-8")


def test_agent_is_told_where_charts_must_go():
    """Only /mnt/session/outputs is captured by the Files API. A chart written
    anywhere else runs successfully and is seen by nobody."""
    assert OUTPUTS_DIR in setup_agent.SYSTEM_PROMPT
    assert OUTPUTS_DIR in _charts()


def test_the_chart_recipe_is_a_file_of_its_own_that_the_skill_points_at():
    """Charts fire on a minority of reporting turns, so the recipe is not paid for
    on the first turn of every session — but a rule the agent has to fetch is a rule
    it can skip, so SKILL.md must name the file and the prompt must send it there."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "/workspace/skills/xas-reporting/charts.md" in skill
    assert "charts.md" in setup_agent.SYSTEM_PROMPT
    assert "matplotlib" not in skill, "the recipe lives in one place"
    assert f"xas-reporting/{CHARTS_MD}" in dict(setup_agent.reporting_bundle())


def test_agent_is_told_not_to_read_the_chart_back():
    """Reading a PNG back returns base64 -- ~100KB of context for no new information."""
    prompt = setup_agent.SYSTEM_PROMPT.lower()
    assert "do not read the chart back" in prompt
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
    for the key alone because it only ever reads `totalCount`.

    Says "no COLUMNS" since 2026-08-31: "a count needs no fields" read as a
    contradiction of the "Every row sends `fields`" heading two lines above it. The
    rule is the same one — `fields` is always sent, and for a count it names the key
    and nothing else."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Every row sends `fields`" in skill
    assert 'fields: ["DMSJCEntry"]' in skill, "the count row must ask for the key alone"
    count_row = next(l for l in skill.splitlines() if l.startswith("| A count |"))
    assert 'fields: ["DMSJCEntry"]' in count_row, "a count asks for the key and no columns"
    assert "A count needs no fields" not in skill, "`fields` is sent on every call"


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


def test_the_reply_contract_is_the_last_thing_the_skill_says():
    """The skill's own output rules sit at the end, after the procedure that produces
    the answer, and the mid-turn silence rule is the prompt's (see
    test_between_tool_calls_the_agent_says_nothing) so it holds on every request
    rather than once per session."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert skill.rstrip().split("## ")[-1].startswith("Presenting the answer")


def test_reporting_skill_keeps_a_stored_name_whole():
    """Observed 2026-08-27: the account `Daniil123` was reported as "Daniil (account
    123)" — a name nobody stored beside a code the planner may not see. It started a
    turn earlier with a table column of account codes, which is the same rule broken
    as a column rather than a sentence."""
    skill = (setup_agent.REPORTING_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "A stored name is ONE string" in skill
    assert "Daniil123" in skill, "name the observed failure, not the abstraction"
    assert 'A column headed "Code" breaks this' in _flat(skill)


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
