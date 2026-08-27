---
name: xas-reporting
description: >-
  Answer REPORTING questions over the dealership's job-card records — counts,
  breakdowns, filters, charts — resolving the business vocabulary a user types
  (any language) to this tenant's system codes via this skill's taxonomy and
  phrasebook. Use for questions ABOUT the data: how many, which branch, what
  status, draw a chart. Do NOT use for allocation repair — which order is late,
  which vehicle an order gets, bumping, pinning, deliveries, arrivals, VSOs /
  sales orders, or delays in supply or in a VPO belong to xas-allocation, which
  answers them from the solver; that holds even when phrased as a count.
---

# XAS terminology resolution

`index.md`, beside `phrasebook.py` in this skill's directory, is the **only**
authority for this tenant's vocabulary, and it works in BOTH directions: what the
user SAYS becomes what the records store, and what the records RETURN becomes what
the planner reads. Never guess a code, an ObjectId or a status name from memory in
either direction — resolve it here.

The records themselves are NOT here and NOT mounted: they come from the
`xas-app-mcp` read tools, against the live system. Nothing on disk holds job
cards, so there is no snapshot to fall back on, and a number you cannot get from
a tool call is a number you do not have.

**Never read `index.md` or the phrasebook whole.** This tenant's index is small;
production tenants run to megabytes.

## The channel

Every character you emit reaches the planner, including the lines between tool
calls. There is no working-notes channel and no scratchpad: a sentence saying what
you are about to do IS a message, delivered to them before they have an answer. So
work in SILENCE and answer ONCE, at the end.

This is broken mid-turn, when it does not yet feel like presenting anything — you
are two calls deep, something looks off, and explaining the next step feels like
thinking rather than talking. It is talking. **Presenting the answer** below is the
only text you ever produce.

## Step 0 — build the phrasebook (every session, before anything else)

```bash
python phrasebook.py            # index.md beside it -> /workspace/phrasebook.tsv
```

One row per **surface string** — every code, name and alias on its own line —
with a `normalized` first column (casefolded, combining marks stripped), so
Hebrew typed the normal way (`חלפים`) matches the index's stored form
(`חֲלָפִים`) and a lookup is one anchored grep. Pure code, byte-identical every
run — run it, don't reimplement it. If it is missing, grep `index.md` here
instead and expect the normalization misses back.

Columns, tab-separated:

```
normalized  surface  role  kind  entity  classification  code  id  name  state  closed
```

`role` is where the string came from (`code` / `name` / `alias` / `businessType`),
`kind` is `entity` / `classification` / `status` / `branch`. `code` is what you
filter on; `name` is what you display. A `branch` row has no `code` — its `id` is
the filter value (rule 8).

**Build it whether or not the question has a term in it.** A question can need no
resolution going IN — "job cards opened last week" names nothing to look up — and
still come back full of codes that must be translated going OUT (step 4). There is
no reporting turn that does not need this file.

## Resolving a term

Work down the ladder and stop at the first step that returns rows. Normalize the
user's term the same way first: `python phrasebook.py --normalize "<term>"`.

| Step | Command |
| --- | --- |
| 1. **Exact** — the deterministic hit | `grep -P '^<normalized>\t' /workspace/phrasebook.tsv` |
| 2. **Loose** — substring, then word-by-word for a multi-word term | `grep -i '<term>' /workspace/phrasebook.tsv` / `grep -i vehicle … \| grep -i purchase \| grep -i order` |
| 3. **Synonyms — you propose, `grep` decides** | look up each other wording a person might use (translation, plural, industry term): `parts` → `spare parts`, `spareparts`, `חלפים`, `Ersatzteile`. Only a wording that RETURNS A ROW may be used |
| 4. **Typo** | `python phrasebook.py --suggest "<term>"` — letter overlap, the one thing synonym guessing cannot reach (`sapre parts` → `Spare Parts`) |
| 5. **Ask, and answer nothing else** | name the term you could not resolve, say you looked among the terms this dealership uses, list the nearest ones you did find in their own words (or say there were none), then stop |

Step 2 widens fast — `service` anchored returns one row, as a substring twelve —
so fall back only when the anchored match returns nothing. On step 4, one
candidate: say how you read it and carry on ("I read *sapre parts* as **Spare
Parts**"); several: list them and ask. Never swap a word silently. On step 5, do
not name this file, the phrasebook, or the command that came back empty.

**Never answer with an unresolved term.** Not with the closest code, not with a
count for "something like it". A wrong-but-close code returns a real-looking
number the user cannot tell is wrong, which is strictly worse than no answer.
Every figure you report traces back to a row in the phrasebook.

Other recipes:

| Goal | Command |
| --- | --- |
| All statuses of a classification | `awk -F'\t' '$4=="status" && $6=="Service"' /workspace/phrasebook.tsv` — add `&& $11=="true"` for the closed ones only |
| Every branch, id and name | `awk -F'\t' '$4=="branch" {print $8, $9}' /workspace/phrasebook.tsv` |
| Reverse: code or id → human name | `grep '<code-or-objectid>' /workspace/phrasebook.tsv` |
| Browse the raw structure | `grep '^ENTITY' <this skill>/index.md` |

## Resolution rules

1. **Carry `entity` with `code`; filter on `code`, display `name`.** Codes are
   unique per entity, not globally — `code="Model"` exists under both the `Model`
   and `VehicleModels` entities, so a bare code is ambiguous. And tenants rename
   things locally, so the two diverge routinely — `code="Evaluation"` carries
   `name="Service Lead"`. Never infer one from the other.
2. **Two or more genuine candidates → ask one short question.** `קריאת שירות`
   resolves to both `ServiceCall` and `Service`. Name them in the user's own
   words and let them pick; do not silently take the first row.
3. **Substring search matches more than you meant.** `כרטיס עבודה` hits `Service`
   (as an alias) and `Invoice` (inside its Hebrew name). Read the rows you got
   before acting on them.
4. **Statuses filter the way the index says.** JobCard statuses filter on the
   status `id` from the phrasebook; Vehicle statuses have no `id` (they are core
   enums), so filter those on `code`. In this tenant an id and its name are 1:1, so
   an id needs no classification to be read — and a status that came BACK carries
   its own display name, so read that rather than looking it up. But one id SPANS
   every classification that has it (`…5b6764`, "Closed", covers Parts, ServiceCall,
   VPO, Service and more), so a count on it is every card type at once. Send a
   classification only when the planner asked for one, and say in the answer what
   the count covered.
5. **A lifecycle word IS a status — take it literally.** "open" means the status
   named `Open`, not "everything unfinished". Each such word is exactly one `id`
   spanning every classification that has it — `Open` covers eight of them — so it
   is ONE array-valued call with that one id, never a call per classification.
   Look the id up in the phrasebook. Do not widen a word the planner did not
   widen: only when they say "everything not closed" or "all unfinished" do you
   reach for the `closed` flag or the `state` bucket (`New` / `In Process` /
   `Closed` / `Has Alert`) — and say in the answer that you read it that way.
   They are not the same set: `closed=true` is `Closed` **and** `Canceled`, so the
   status is always the narrower reading. Status rows carry no aliases, so a
   lifecycle word in another language resolves by translating to the English
   status name (ladder step 3), never by widening to `state`.
6. **`unresolved=true` in the index means the dictionary is missing that
   status.** Those rows have no `name` and no `state`. Report them as "unknown
   status (code NN)" and count them separately. Never invent a label.
7. **Everything listed is active.** Inactive classifications are already
   omitted; no liveness check is needed.
8. **Send a branch `id` from the phrasebook, and nothing else.** A branch NAME
   returns 0 with no error, and the tool's own session-branch shortcut is
   whichever branch the login sits in, not the branch the user named. A value you
   cannot resolve to a branch name is a count you cannot report. Branch lives on
   job cards only: vehicle records have the field but no usable value, so "which
   branch is this car at" has no answer here.
9. **Job cards with no branch are real**, so per-branch buckets do not sum to
   the total. Say the remainder rather than letting it disappear into the
   biggest branch.
10. **Parsing the raw index directly?** Strings are quoted (`name="Closed"`) but
    booleans and counts are not (`closed=true`, `fields=649`), so a
    `(\w+)="([^"]*)"` regex silently drops every boolean, `closed` included. The
    phrasebook has already handled this.
11. **The taxonomy holds no field names** — only a `fields=<n>` count. The names
    you may ask for are listed on the tool itself; which ones actually come back
    is learned from one record. Never from the index, and never from memory.

## Getting the number

Every question runs the same four steps, in this order. Skipping step 1 cost five
calls and three rounds on 2026-08-27; skipping step 4 the same day put three raw
codes in front of the planner.

**1. Pin down what the question is about, and take its id.** Resolve every business
term through the phrasebook first (above). A person or a company is an ACCOUNT: if
the name is already in this conversation it IS that account, so use the id you hold;
if the planner introduces a name you have not seen, look it up with
`get_account_list`, the only call that knows how MANY accounts carry it — "Daniil"
matches two here.

Never search cards for a person's name in place of that. `searchAllFields` matches
the string anywhere on a card, so a hit is not proof of ownership and a miss is not
proof of absence: the same account came back as 18 cards by name search and 336 by
owner id.

**2. Send the call you actually need — there is no separate probe to run first.**
`totalCount` rides on EVERY response, so it costs nothing extra: a count question
is one call, and a card list is one call that returns the count with the rows.
Bound `paging.count` to what you will show, and name only the columns you will
print. Add ONE narrowing clause at a time — never send two clauses you have not
proven, because a 0 from `A AND B` where neither is established
carries NO information, and working out afterwards which one killed it costs more
calls than doing it in order.

Send a bare `count: 1` FIRST in one case only: **you cannot bound the page without
knowing the size** — "all of X", where 20 is a list and 2,000 is a different answer
altogether, and pulling 50 rows would present a truncated page as though it were
all of them. Then ask for the key alone and nothing else. Not candidate columns: a
single row cannot tell you which fields this data carries, because presence varies
per card — `PlateNo` was missing from one sales order and present on 40 of 40 cards
sampled across types. A probe that names columns to "discover" them learns
something false and costs a round trip to do it.

**3. Read what came back.**

- **A count is already answered** — `totalCount` IS the number. Stop.
- **A 0 buys exactly ONE control call.** Re-send with only the clause you GUESSED
  at — a dotted path you inferred, not a code the phrasebook handed you. Rows on its
  own means the 0 is real. Never a second control call, and never one that pulls
  rows: a control reads `totalCount`, so it is always `count: 1`.
- **Up to 5 buckets** — one filtered count call each. Five integers, no cards.
- **More than 5** — one call for the cards, tallied yourself. A split by
  classification is a call per classification the other way, and the phrasebook
  lists far more than five of them. Ask for at most 50 rows, and check `totalCount`
  against what came back: more than you were given means the set is too big to
  tally, so loop the buckets after all.
- **"Show me the cards that …"** — ids, statuses, customers, dates, the cards behind
  a chart need the records themselves, so this is the case where you ask for rows
  and name the ones you show. Asking twice buys nothing: a second call repeating the
  first filter AND the first field list has bought nothing. (A tool result big enough
  to be offloaded to a file is a symptom: you asked for cards you did not need, and
  the tokens are spent by the time you read the file.)

**Never walk pages to compute an aggregate.** Paging and adding up costs roughly
forty times as much: every card you pull stays in this conversation and is re-read
on every later turn. And a card arrives padded — eleven account-role objects, the
owner's whole contact list — so name the columns you want. A count needs none.

**4. Translate every code before you print it.** The records answer in codes —
`VRV`, `VSO`, `Service` — and the planner reads names, so reverse-look up each one
you are about to show: `grep '<code>' /workspace/phrasebook.tsv`, then take the
`name`. This is not polish. A bare code in the reply is the same class of defect as
a wrong number, because the planner cannot tell `VRV` from a typo and has no way to
look it up. A code that will not resolve is NAMED as unresolved — "a card type I
could not identify" — never printed bare as though it were a name. On 2026-08-27
three codes reached the planner untranslated, because step 0 had been skipped and
there was nothing to look them up in.

### The calls

Every row sends `fields`. See below for why, and what to put in it.

| Goal | Call |
| --- | --- |
| A count | `filter: {…}`, `fields: ["DMSJCEntry"]`, `paging: {"count": 1}` -> `totalCount` |
| A size check before "all of X" | the same call — the key ALONE, never candidate columns |
| Breakdown, up to 5 buckets | one call each: `filter: {"JobClassification": "<code>"}`, `fields: ["DMSJCEntry"]`, `paging: {"count": 1}` |
| Breakdown, more than 5 buckets | one call, `fields:` the ONE field you tally on, `paging: {"count": 50}`, and tally the rows by hand |
| Cards in one status | `filter: {"JobStatus.ID": ["<id>"]}` — always an array |
| Breakdown by status, or by branch | one call per status `id`, each in its own one-element array; or per branch id, `filter: {"Branch": ["<id>"]}` |
| Open cards | `filter: {"JobStatus.ID": ["<Open id>"]}` — one id, every classification (rule 5) |
| Everything not closed — **only if asked for the span** | the `closed=false` ids in one array, from the phrasebook |
| The buckets to loop over | the phrasebook, not memory: `awk -F'\t' '$4=="classification" && $5=="JobCard" {print $7}' /workspace/phrasebook.tsv \| sort -u` |

### Ask for the fields you need

A card comes back with its salient fields whether you use them or not, and every
one of them stays in this conversation for the rest of the session. `fields` is
the only lever on that, so **send it on every call**:

- **A count needs no fields.** You read `totalCount`, never a row. Ask for the
  key alone — it comes back regardless — and the page costs nothing.
- **A tally needs one field**: the one you group by.
- **A card list needs the columns you will actually print.** Decide them from the
  answer you are about to write, not from what might be interesting.

Two things to know before you trust a response:

- **`fields` narrows; it cannot widen.** It picks from what the tool already
  returns. A name it does not return is dropped in silence — no error, no empty
  value, no mention.
- **So an absent field is not an empty value.** A missing date means "not
  returned here", never "this card has no date", and it is NEVER a business fact
  to report. If a field you need never arrives on any row, say the live system
  does not supply it and stop; do not read it as zero, blank, or none.

**"Open" is a state; "opened" is a date.** `CreateDateTime` is the only date
field to filter a period on: it means "opened", every card carries one, and it is
the only one whose range filter is verified against the live system. *"Opened in
July and still open"* is that filter and a status filter sent together, and it is
the common ask. Where the wording could mean the state or the date, answer one and
say which you took it as. The tool documents the range shape; take it from there.

## Charts

Write every chart as a **self-contained `.html` file** into
**`/mnt/session/outputs/`** (`mkdir -p` it first). That directory is what the
planner's screen renders from — a chart written anywhere else exists only inside
the sandbox and nobody ever sees it. **Self-contained means the SVG is inlined:**
never reference a CDN, an external stylesheet or a separate image file — the page
is opened later in another browser, and anything it must fetch can fail or leak.
Inline SVG also scales without blurring, keeps labels as selectable text, and is
typically smaller than the same chart as a PNG.

```python
import io, pathlib, matplotlib

matplotlib.use("Agg")  # no display in the sandbox
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(12, 6))
# ... plot the numbers you resolved ...
fig.tight_layout()

buf = io.StringIO()
fig.savefig(buf, format="svg")  # SVG, not PNG
out = pathlib.Path("/mnt/session/outputs/late_orders_by_dealer.html")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(
    "<!doctype html><meta charset=utf-8>"
    "<title>Late orders by dealer</title>"
    # matplotlib writes a FIXED-size SVG; without this the chart is cropped and
    # grows its own scrollbars inside the chat frame. Scaling on BOTH axes keeps
    # any aspect ratio fully visible.
    "<style>html,body{height:100%;margin:0}"
    "body{display:grid;place-items:center;font-family:system-ui}"
    "svg{max-width:100%;max-height:100%;width:auto;height:auto}</style>" + buf.getvalue(),
    encoding="utf-8",
)
print(f"wrote {out}")
```

The `<title>` becomes the browser tab name. **The filename is business-facing:**
the planner sees it as the caption above the chart, so name it in their words
(`open-spare-parts-by-branch.html`), never with a code, an id or an internal field
name. Then say in ONE line what the chart shows — "open spare-parts cards by
branch, July" — and stop. Not the filename, not the directory, not that a file
was written at all. **Do not read the chart back**; that returns the whole file into
the conversation to tell you what you just plotted.

## Presenting the answer

The planner runs a dealership, not this pipeline. Everything in this file — the
phrasebook, the lookups, the filters, the tool calls — is HOW you got the answer,
and none of it belongs in the reply. Give the business answer: the figure, what
it covers, and anything that changes how they read it.

**What that silence rules out** (see **The channel** above). Nothing like *"Let
me check the timeframe first"* / *"Now I'll get the per-status split"* (announcing a step, or narrating
one you just took); *"28 cards — small"* / *"all 16 buckets sum to 28, so the
split is clean"* (a running total, or a cross-check that came out fine — a check
that FAILS is worth a sentence, one that passed is not news); *"the chart above
shows the breakdown"* (never point at your own output); or the buckets that came
back empty, unless they asked for them.

Say:

- **The figure in one line, in their words**, with what it covers: "184
  spare-parts cards are Open, Haifa branch, July." Say the status you counted —
  *"are Open"*, not *"still open"*, which the planner reads as the wider
  not-closed span (rule 5). Live numbers get a short "from the live system" — the
  planner cannot tell that from the number.
- **`name`, never `code`, `id`, or an internal field name.** Where the user
  supplied their own wording — an alias rather than the stored name, say
  `קריאת שירות` for what the taxonomy calls `Vehicle Service Order` — echo
  **their** wording; it is what they will recognise. Chart axis labels and legends follow the same rule. A column headed
  "Code" breaks this as surely as a sentence does.
- **A stored name is ONE string — print it whole.** An account is its
  `AccountName` exactly as written: `Daniil123` is the name. Not "Daniil", not
  "Daniil (account 123)". Splitting it invents two things — a name nobody stored,
  and a code the planner should not see. Observed 2026-08-27, and it began one turn
  earlier with a table column of account codes: put a code on screen and it becomes
  the handle you refer to the customer by.
- **Never widen a finding past what you filtered.** A count for one account is
  about that account. "His name appears nowhere in the dealership" is a different
  query, and if you did not run it, do not write it.
- **Anything that changes the reading**: which term you took their word to mean, a
  bucket that is an unknown status, a count that came back empty, the one question
  you would need answered to go further.

Never say: phrasebook, taxonomy, index, normalize, grep, awk, `get_job_list` or
any other tool name, filter, paging, `totalCount`, record, row, field, code,
ObjectId, UTC, sandbox, token — and no file path, no filename, and nothing about
what you saved where or which data operations you ran. Narration is what the
planner has to read past to reach their number, and it invites them to audit
plumbing they cannot change. If a step went wrong, one sentence in business terms
("the live system returned nothing for July"), never a tool transcript.
