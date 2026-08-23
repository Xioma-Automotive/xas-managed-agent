---
name: xas-qa
description: >-
  Answer REPORTING questions over the dealership's job-card records — counts,
  breakdowns, filters, charts — resolving the business vocabulary a user types
  (any language) to this tenant's system codes via this skill's taxonomy and its
  normalized phrasebook. Use for questions ABOUT the data: how many, which
  branch, what status, draw a chart. Do NOT use for allocation repair — which
  order is late, which vehicle an order gets, bumping or pinning belong to
  xas-allocation, which answers those from the solver. Nor for deliveries,
  arrivals, VSOs / sales orders, or delays in supply or in a VPO: those are
  allocation, even when phrased as a count.
---

# XAS terminology resolution

The tenant's taxonomy is `index.md`, **in this skill's directory** beside
`phrasebook.py`. It is the **only** authority for turning what a user says into
what the records store. Never guess a code, an ObjectId, or a status name from
memory — resolve it here.

The records themselves are NOT here and NOT mounted: they come from the
`xas-app-mcp` read tools, against the live system. Nothing on disk holds job
cards, so there is no file to find, no snapshot to fall back on, and a number you
cannot get from a tool call is a number you do not have.

The taxonomy needs no search either: `phrasebook.py` finds `index.md` beside
itself, so Step 0 just works.

## Step 0 — build the phrasebook (once per session)

```bash
python phrasebook.py            # index.md beside it -> /workspace/phrasebook.tsv
```

`phrasebook.py` ships in this skill. It explodes the index into **one row per
surface string** — every code, name and alias on its own line — with a
`normalized` first column (casefolded, combining marks stripped). That is what
makes Hebrew typed the normal way (`חלפים`) match the index's stored form
(`חֲלָפִים`), and what turns a term lookup into a single anchored grep.

It is pure code: same index in, byte-identical phrasebook out. Run it, don't
reimplement it. If it is missing, fall back to grepping `index.md` in this skill
directory and expect the normalization misses back.

Columns, tab-separated:

```
normalized  surface  role  kind  entity  classification  code  id  name  state  closed
```

`role` is where the string came from (`code` / `name` / `alias` / `businessType`),
`kind` is `entity` / `classification` / `status` / `branch`. `code` is what you
filter on; `name` is what you display. A `branch` row has no `code` — its `id` is
the filter value (rule 11).

## Lookup recipes

Normalize the user's term the same way before an anchored match:

```bash
python phrasebook.py --normalize "<what the user said>"
```

| Goal | Command |
| --- | --- |
| **Exact hit (try this first)** | `grep -P '^<normalized>\t' /workspace/phrasebook.tsv` |
| Fuzzy tail, if exact misses | `grep -i '<term>' /workspace/phrasebook.tsv` |
| Multi-word, any word order | `grep -i 'vehicle' … \| grep -i 'purchase' \| grep -i 'order'` |
| All statuses of a classification | `awk -F'\t' '$4=="status" && $6=="Service"' /workspace/phrasebook.tsv` |
| Every branch, id and name | `awk -F'\t' '$4=="branch" {print $8, $9}' /workspace/phrasebook.tsv` |
| Only the closed ones | add `&& $11=="true"` |
| Reverse: code or id → human name | `grep '<code-or-objectid>' /workspace/phrasebook.tsv` |
| Typo / near-miss, after everything missed | `python phrasebook.py --suggest "<what they said>"` |
| Browse the raw structure | `grep '^ENTITY' <this skill>/index.md` |

**Exact-first, then fuzzy.** An anchored match on `normalized` is the
deterministic hit; only fall back to substring search when it returns nothing.
`service` anchored returns one row; `service` as a substring returns twelve.

**Never read either file whole.** This tenant's index is small; production
tenants run to megabytes, and a whole-file read is the failure mode this design
exists to prevent.

## When a term doesn't resolve

Work down this ladder and stop at the first step that returns rows.

1. **Exact** — anchored match on `normalized`.
2. **Loose** — substring, then the multi-word chain above.
3. **Synonyms — you propose, `grep` decides.** Generate the other wordings a
   person might use for the same thing (the translation, the plural, the industry
   term) and look each one up: `parts` → try `spare parts`, `spareparts`,
   `חלפים`, `Ersatzteile`. Only a wording that RETURNS A ROW may be used. You are
   proposing candidates to check, never confirming a code — the grep confirms.
4. **Typo** — `python phrasebook.py --suggest "<what they said>"`. Letter-overlap
   candidates, which is the one thing synonym guessing cannot reach (`sapre parts`
   → `Spare Parts`). One candidate: say how you read the word and carry on ("I
   read *sapre parts* as **Spare Parts**"). Several: list them and ask. Either
   way the substitution is visible — never swap a word silently.
5. **Ask, and answer nothing else.** Name the term you could not resolve, say you
   looked for it among the terms this dealership uses, and list the nearest ones
   you did find — in their own words — or say there were none. Then stop. Do not
   name this file, the phrasebook, or the command that came back empty.

**Never answer with an unresolved term.** Not with the closest code, not with a
count for "something like it". A wrong-but-close code returns a real-looking
number and the user cannot tell it is wrong, which is strictly worse than no
answer. Every figure you report traces back to a row in the phrasebook.

## Resolution rules

1. **Always carry `entity` with `code`.** Codes are unique per entity, not
   globally — `code="Model"` exists under both the `Model` and `VehicleModels`
   entities. A bare code is ambiguous.
2. **`code` and `name` diverge, routinely.** Tenants rename things locally:
   `code="Service"` carries `name="Distinct_name"`, `code="Contract"` carries
   `name="0510"`. **Filter on `code`, display `name`.** Never infer one from the
   other.
3. **Two or more genuine candidates → ask one short question.** `קריאת שירות`
   resolves to both `ServiceCall` and `Service`. Name the candidates in the
   user's own words and let them pick. Do not silently take the first row.
4. **Substring search matches more than you meant.** `כרטיס עבודה` hits `Service`
   (as an alias) and `Invoice` (inside its Hebrew name). Read the rows you got
   before acting on them.
5. **Filter statuses the way the index says.** JobCard statuses are filtered on
   `JobStatus.ID` using the status `id`. Vehicle statuses have no `id` (they are
   core enums) — filter those on `code`.
6. **A status `id` does not identify a classification.** The same ObjectId
   (`…5b6764` = "Closed") appears under Parts, ServiceCall, VPO, Service and
   more. Always scope a status lookup by its classification.
7. **A lifecycle word IS a status — take it literally.** "open" means the status
   named `Open`, not "everything unfinished". Each lifecycle word is exactly one
   `id`, and that id spans every classification that has it, so it is ONE
   array-valued call and never a loop: `Open` = `6530d9a89c098a33be3e0c73` (8
   classifications), `Closed` = `…05a65b6764` (9), `Canceled` = `…05a65b6765` (6).
   Do not widen a word the planner did not widen. They will say "everything not
   closed" or "all unfinished" when they mean the span — **only then** reach for
   the `closed` flag or the `state` bucket (`New` / `In Process` / `Closed` /
   `Has Alert`), and say in the answer that you read it that way. The two are not
   the same set: `closed=true` is `Closed` **and** `Canceled`, so the status is
   always the narrower reading. Status rows carry no aliases — only `code` and
   `name` — so a lifecycle word in another language resolves by translating to
   the English status name (ladder step 3), not by widening to `state`.
8. **`unresolved=true` in the index means the dictionary is missing that
   status.** Those rows have no `name` and no `state`. Report them as "unknown
   status (code NN)" and count them separately. Never invent a label.
9. **Everything listed is active.** Inactive classifications are already
   omitted; no liveness check is needed.
10. **Parsing the raw index directly?** Strings are quoted (`name="Closed"`) but
    booleans and counts are not (`closed=true`, `fields=649`). A
    `(\w+)="([^"]*)"` regex silently drops every boolean, `closed` included. The
    phrasebook has already handled this — one more reason to use it.
11. **Send a branch `id` from the phrasebook, and nothing else.**
    `{"Branch": ["69f07fdaf930e4ee6d524dc1"]}` is Main;
    `{"Branch": ["Main"]}` returns 0 with no error. `{"Branch": true}` is
    whichever branch the login sits in, not the branch the user named — look the
    name up and send the id. The filter also accepts values that are neither an
    id nor a name — the app's own UI sends `{"Branch": ["-2"]}` — and what those
    select is **not established**. Never send one: a value you cannot resolve to
    a branch name is a count you cannot report. Branch lives on job cards only: vehicle records have
    the field but no usable value, so "which branch is this car at" has no answer
    here.
12. **Job cards with no branch are real.** 610 of this tenant's cards carry none,
    so per-branch buckets do not sum to the total. Say the remainder rather than
    letting it disappear into the biggest branch.

## Getting the number — filter, never page

Every `get_job_cards` response carries **`totalCount` for the filter you sent**,
independent of how many rows come back. That is the count. Ask for one row and
read it:

```
get_job_cards  filter: {…}  paging: {"count": 1}   ->  totalCount
```

**Never walk pages to compute an aggregate.** Asking for page 2, 3, 4 is the thing
that costs roughly forty times as much, because every record you fetch stays in
this conversation and is re-read on every later turn of the session. Records arrive
padded, too: eleven account-role objects and the owner's whole contact list, none of
which a count needs. Reading ONE modest page and tallying it yourself is not paging
— see the two plans below.

**`totalCount` rides on every response, whatever `paging.count` you sent.** So if
you are going to need the rows anyway — to split a small set, to name the cards, to
chart them — ask for them on the FIRST call and read `totalCount` off that same
response. Sending `count: 1` and then re-sending the same filter for rows is the
same query twice.

**A breakdown has two plans. Pick one before the first call.**

| Plan | Costs | Cheaper when |
| --- | --- | --- |
| One filtered count per bucket | one call per bucket, no rows | the population is bigger than the bucket list |
| Fetch one page of rows and tally them yourself | one call, `totalCount` rows | `totalCount` is at or below the number of buckets |

There are 23 JobCard classifications. A split by type over a single day — three or
four cards — is 23 calls the bucket way and **one** the tally way. A narrow window
is the tally case nearly every time; a month or "all time" is not.

You do not know `totalCount` before the first call, so make that call decide it:
set `count` to the most rows you would be willing to tally (20–30 is sane), send it
once, and read `totalCount`.

- `totalCount` at or below what you asked for → you already hold every row. Tally
  and answer. One call, done.
- `totalCount` above it → the population is large after all. Now loop the buckets.
  The rows you fetched were the price of finding that out, which is why the ask
  stays modest.

| Goal | Call |
| --- | --- |
| A count and nothing else | `filter: {…}`, `paging: {"count": 1}` -> `totalCount` |
| A count you will then break down | `paging: {"count": 25}` once — `totalCount` **and** the rows, in one call |
| Breakdown by classification | one call per `code`: `filter: {"JobClassification": "<code>"}` |
| Cards in one status | `filter: {"JobStatus.ID": ["<id>"]}` — **an array**; a bare string is a 500 |
| Breakdown by status | one call per status `id`, each in its own one-element array |
| Breakdown by branch | one call per branch `id`: `filter: {"Branch": ["<id>"]}` |
| Open cards | `filter: {"JobStatus.ID": ["6530d9a89c098a33be3e0c73"]}` — one id, every classification (rule 7) |
| Everything not closed — **only if asked for the span** | the `closed=false` ids in one array, from the phrasebook |
| The buckets to loop over | the phrasebook, not memory: `awk -F'\t' '$4=="classification" && $5=="JobCard" {print $7}' /workspace/phrasebook.tsv \| sort -u` |

Fetch actual rows only when the planner wants to **see** cards — then keep
`paging.count` small and name the ones you show.

**"Open" is a state; "opened" is a date.** Two different questions, one letter
apart:

- *"open job cards"* — the status named `Open`, one id, no date involved (rule 7).
- *"cards opened in July"* — when the card was **created**: `CreateDateTime`.
  Every card carries one.
- *"opened in July and still open"* — both, and it is the common ask. Send the
  state filter and the `CreateDateTime` window together.

`CreateDateTime` is the only field that means "opened". The near misses:

| Field | What it actually is |
| --- | --- |
| `CreateDateTime` | when the card was opened — **this is the one a period means** |
| `ClosedDateTime` | when it was closed; the field for *"closed in July"* |
| `EntryDate` | a date-only business field, on a minority of cards — not the creation stamp |
| `UpdateDateTime` | last touched, and it keeps moving; never a period boundary |
| `DueDate` | the promise, not the opening |

Reaching for `UpdateDateTime` or `EntryDate` on "opened in July" returns a
real-looking number for a question nobody asked. Where the planner's wording
could mean the state or the date, answer one and say which you took it as.

**Dates are compared in UTC; this tenant's records are stamped `+03:00`.** Convert
the local month boundary yourself or you clip the first three hours of the month
and silently absorb three hours of the next one:

```
July 2026  ->  start "2026-06-30T21:00:00.000Z"   end "2026-07-31T20:59:59.999Z"
```

**A tool result big enough to be offloaded to a file is a symptom, not a
convenience.** It means you asked for rows you did not need — the tokens are
already spent by the time you read the file. Re-ask with `count: 1` instead.

## What the taxonomy does NOT contain

Resolve these from the **records**, not from the index or phrasebook — asking the
taxonomy for them wastes a turn and invites invention:

- **Field names.** The index reports only a `fields=<n>` count, never the field
  names or labels. Learn the real shape from one record — fetch a single card
  (`get_job_cards` with `paging: {"count": 1}`) and read its keys.

## Charts

Write every chart as a **self-contained `.html` file** into
**`/mnt/session/outputs/`** (`mkdir -p` it first). That directory is what the
planner's screen renders from — a chart written to `/workspace` or the current
directory exists only inside the sandbox and nobody ever sees it.

**Self-contained means the SVG is inlined in the page.** Never reference a CDN,
an external stylesheet, or a separate image file: the page is opened later, in a
different browser, and anything it has to fetch is a dependency that can fail or
leak. Inline SVG also scales without blurring and keeps the labels as selectable
text — and it is typically *smaller* than the same chart as a PNG.

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
    # Fit the chart to whatever shows it. matplotlib writes a FIXED-size SVG,
    # so without this the chart is cropped and grows its own scrollbars inside
    # the chat frame. Scaling on BOTH axes keeps any aspect ratio fully visible.
    "<style>html,body{height:100%;margin:0}"
    "body{display:grid;place-items:center;font-family:system-ui}"
    "svg{max-width:100%;max-height:100%;width:auto;height:auto}</style>" + buf.getvalue(),
    encoding="utf-8",
)
print(f"wrote {out}")
```

Give the page a `<title>` — it becomes the browser tab name when the planner
opens the chart full size.

**The filename is business-facing: the planner sees it as the caption above the
chart.** Name it in their words (`open-spare-parts-by-branch.html`), never with a
code, an id or an internal field name.

Then say in ONE line what the chart shows — "open spare-parts cards by branch,
July" — and stop. Not the filename, not the directory, not that a file was
written at all: the chart is already on their screen.

**Do not read the chart back.** Reading it returns the whole file into the
conversation — tens of thousands of tokens to tell you what you just plotted.

## Presenting the answer

The planner runs a dealership, not this pipeline. Everything in this file — the
phrasebook, the lookups, the filters, the tool calls — is HOW you got the answer,
and none of it belongs in the reply. Give the business answer: the figure, what
it covers, and anything that changes how they read it.

**Everything you type is the reply.** There is no working-notes channel: the
planner reads every line, including the ones between tool calls. So work in
silence and answer ONCE, at the end. Nothing like:

- *"Let me check the timeframe first"* / *"Now I'll get the per-status split"* —
  announcing a step you are about to take, or narrating the one you just took.
- *"28 cards — small"* / *"all 16 buckets sum to 28, so the split is clean"* — a
  running total, or a cross-check that came out fine. If a check FAILS that is
  worth a sentence; a check that passed is not news.
- *"The chart above shows the breakdown"* — never point at your own output; they
  can see it.
- The buckets that came back empty, unless they asked for them.

Say:

- **The figure in one line, in their words**, with what it covers: "184
  spare-parts cards are Open, Haifa branch, July." Say the status you counted —
  *"are Open"*, not *"still open"*, which the planner reads as the wider
  not-closed span (rule 7). Live numbers get a short
  "from the live system" — the planner cannot tell that from the number.
- **`name`, never `code`, `id`, or an internal field name.** Where the user
  supplied their own wording (an alias, or a term whose `name` is a local
  placeholder like `Distinct_name`), echo **their** wording — it is what they
  will recognise. Chart axis labels and legends follow the same rule.
- **Anything that changes the reading**: which term you took their word to mean,
  a bucket that is an unknown status, a count that came back empty, the one
  question you would need answered to go further.

Never say: phrasebook, taxonomy, index, normalize, grep, awk, `get_job_cards` or
any other tool name, filter, paging, `totalCount`, record, row, field, code,
ObjectId, UTC, sandbox, token — and no file path, no filename, and nothing about
what you saved where or which data operations you ran. "I built the phrasebook,
resolved חלפים to code 4 and read totalCount off a filtered call" is the same
answer as "184 spare-parts cards" with the kitchen on show.

The narration is not a courtesy: it is what the planner has to read past to reach
their number, and it invites them to audit plumbing they cannot change. If a step
went wrong, that is worth a sentence — in business terms ("the live system
returned nothing for July"), never as a tool transcript.
