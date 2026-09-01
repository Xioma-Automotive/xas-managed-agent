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

# XAS reporting

Counts, breakdowns, lists and charts over this dealership's job-card records. They
live in the LIVE system and reach you through the `xas-app-mcp` read tools; nothing
is on disk, so a number you cannot get from a tool call is a number you do not have.

Filter VALUES come from the phrasebook, filter KEYS from **The calls** below or
from a `source` block a tool echoed. **Never take a filter — key or value — from a
tool's `fields` list**: that list says only which columns you may SEE, and a filter
built from it comes back as 0 rows rather than an error, which reads exactly like a
real answer. `In Stock` is a vehicle STATUS with code `03` in the phrasebook, not
the `inventoryStatus: "InStock"` the field list advertises.

## The phrasebook

`/workspace/skills/xas-reporting/phrasebook.tsv` is there before your first turn,
built and ready. It is the only authority for this tenant's vocabulary, and it
works in BOTH directions: the user's words going in, the records' codes coming
out. Never guess a code, an id or a status name from memory; never build or edit
the file. **Look terms up with the command below; never read it whole** — this
tenant's is small, production tenants run to megabytes. (Missing at that path?
`ls /workspace/skills/*/phrasebook.tsv`.)

One row per surface string — every code, name and alias on its own line — with a
`normalized` first column (casefolded, combining marks stripped), so Hebrew typed
the normal way (`חלפים`) matches the stored form (`חֲלָפִים`) however the user
typed it. Tab-separated:

```
normalized  surface  role  kind  entity  classification  code  id  name  state  closed  route
```

- `kind` — `entity` / `classification` / `status` / `state` / `branch`. One surface
  can appear under two kinds (`Closed` is both a status and a state), so read
  `kind` before acting on a row.
- `route` — classification rows only: the app page those records list on, for
  `link.py --route`. Empty means there is nothing to link.
- `role` and the remaining columns are provenance; you never filter on them.

Everything listed is queryable, so never check whether a type is active — two the
config marks inactive are listed anyway because they still hold cards. A
classification that is ABSENT is not evidence that no card carries it.

## Resolving a term

**One command does the searching.** Give it the user's term and any other
wordings you would have tried — translations, plurals, industry terms — in one
call:

```bash
python /workspace/skills/xas-reporting/resolve.py --lookup "חלפים" "spare parts" "parts"
```

It works the ladder and stops at the first rung that hits — the stored form, a code
or an id read backwards, a substring, word by word, the nearest spelling for a typo
— and its first line says which wording matched and how. **Proposing the wordings
is yours**; only one that RETURNS A ROW may be used.

| Its first line | You do |
| --- | --- |
| `matched … — exact` or `— code or id` | use the row |
| `matched … — partial` or `— words` | pick from the rows, or narrow the term and look again; the line says how many it held back |
| `no match … nearest entries, CONFIRM` | ONE candidate: say how you read it and carry on ("I read *sapre parts* as **Spare Parts**"). Several: list them and ask. Never swap a word silently |
| `no match … ask the user` | name the term you could not resolve, say you looked among the terms this dealership uses, list the nearest ones you did find in their own words, and stop. Do not name this file or the command that missed |

The table is a plain TSV — grep it directly when you want one column and nothing
else.

**Never answer with an unresolved term.** Not the closest code, not a count for
"something like it": a wrong-but-close code returns a real-looking number the user
cannot tell is wrong. Every figure traces back to a row.

**Two genuine candidates → ask one short question.** `קריאת שירות` is both
`ServiceCall` and `Service`: name them in the user's own words and let them pick.

### Reading the row you found

1. **Filter on `code`, display `name`, and carry the `entity` with both.** Codes
   are unique per entity, not globally — `Model` exists under both `Model` and
   `VehicleModels` — and `code` and `name` diverge wherever a tenant renamed
   something (`code=Evaluation` carries `name=Service Lead`).
2. **Take the planner's word literally; never widen it.** "Open" means the status
   named `Open` — ONE id spanning every classification that has it, so ONE
   array-valued call, never a call per classification. Here an id and its name are 1:1,
   so send a classification only when the planner asked for one, and say in the
   answer what the count covered. Widen only where they widened ("not closed",
   "all unfinished"): then the `closed` flag or the `state` bucket (`New` /
   `In Process` / `Pending` / `Closed` / `Has Alert`), and say you read it that way.
   `closed=true` is `Closed` **and** `Canceled`, so the status is always the
   narrower reading.
3. **Never invent what the table does not hold.** A status row with an empty `name`
   is "unknown status (code NN)", counted separately and never relabelled. Cards
   with no branch are real, so per-branch buckets do not sum to the total: state
   the remainder. Status rows carry no aliases, so a lifecycle word in another
   language resolves by translating to the English status name, never by widening
   to `state`.

## Answering a question

**1. Pin down what the question is about, and take its id.** Resolve every business
term through the phrasebook first; a person or a company is an ACCOUNT. An id
already in this conversation needs no lookup. A name you have not seen goes to
`get_account_list` as `search`, which returns both handles: `Code` filters that
customer's cards, `Id` routes their page.
**`get_account_details` sections are PREVIEWS**: `include: ["jobCards"]` returns 10
rows however many exist, with no paging and no `fields`.
So a customer's cards are `get_job_list` filtered on the owner, and that is the
FIRST call — never `get_account_details`.

**2. Send the call, and the link beside it.** Take the row from **The calls** below.
`totalCount` rides on EVERY response, so there is no separate probe to run first,
and a size check before the call you would have made anyway has bought NOTHING. Add
one unproven clause at a time: a 0 from `A AND B` where neither is established
carries NO information. Emit the `link.py` command in the SAME block as the call,
same filter written once — you hold everything the link needs before the response
arrives, so waiting for it costs a round trip to format a string:

```
get_job_list  filter={"JobClassification":"VRV","JobStatus.ID":["…"]}  fields=[…]  paging={"count":1}
bash          python /workspace/skills/xas-reporting/link.py --route /vehicle_planning \
                --filter '{"JobClassification":"VRV","JobStatus.ID":["…"]}'
```

**3. Read what came back.** `totalCount` IS the count — stop there. A 0 buys exactly
ONE control call: re-send with only the clause you GUESSED at — a dotted path you
inferred, not a code the phrasebook handed you — at `count: 1`, never pulling rows,
and never a second control. **Never walk pages to compute an aggregate**: every card
you pull stays in this conversation and is re-read on every later turn.

**4. Translate every code before you print it.** `JobStatus` arrives as
`{ID, Code, Label}` and already carries its label; `JobState` arrives as a BARE
ObjectId, so `--lookup` it and print the `kind=state` row's `name`. A code that will not
resolve is NAMED as unresolved — "a card type I could not identify" — never printed
bare.

### The calls

Every row sends `fields`. **Key case differs by lane, and the wrong case returns 0
rather than an error**: job-card keys are capitalised dotted paths (`JobStatus.ID`,
`Accounts.Owner.AccountDMSCode`); vehicle and account keys are lower-camel
(`status.code`, `code`, `_id`), though their results come back capitalised.

| Goal | Call |
| --- | --- |
| A count | `filter: {…}`, `fields: ["DMSJCEntry"]`, `paging: {"count": 1}` → `totalCount` |
| Breakdown, up to 5 buckets | one call each: `filter: {"JobClassification": "<code>"}`, `fields: ["DMSJCEntry"]`, `paging: {"count": 1}`. Five integers, no cards |
| Breakdown, more than 5 buckets | ONE call for the cards, `fields:` the one field you tally on, `paging: {"count": 200}` — the server's maximum — tallied by hand. **Never page a tally**: page 2 is a whole round trip that almost never changes the answer. If `totalCount` exceeds what you got, the set is too big to tally — loop the buckets instead. 200 is not free: what bounds a page is BYTES, and an `Accounts.*` field arrives as the whole owner object |
| "Show me the cards that …" | the rows, with only the columns you will print. A second call repeating the same filter AND field list has bought nothing |
| "All of X" you must print columns for | first ask: can you bound the page without knowing the size? Where you cannot — 20 rows is a list, 2,000 is a summary — send the key alone, `fields: ["DMSJCEntry"]`, `paging: {"count": 1}`, never candidate columns. Not before a tally: that is already one page of one field |
| All jobs of one customer | `filter: {"Accounts.Owner.AccountDMSCode": "<the account's `Code`>"}` — never their `AccountUUID`, which returns fewer cards and says nothing about the shortfall |
| Cards in one status | `filter: {"JobStatus.ID": ["<id>"]}` — always an array |
| Vehicles in one status | `get_vehicle_list`, `filter: {"status.code": "<code>"}`, `fields: ["VehicleCode"]` — a vehicle status is a CODE, never an id |
| Breakdown by status, or by branch | one call per status `id`, each in its own one-element array; or per branch, `filter: {"Branch": ["<the ObjectId>"]}` — a branch NAME returns 0 with no error, and only job cards carry a usable branch |
| Open cards | `filter: {"JobStatus.ID": ["<Open id>"]}` — one id, every classification |
| Everything not closed — **only if they asked for the span** | the `closed=false` ids in one array, from the phrasebook |

### Ask for the fields you need

A card comes back with all its salient fields whether you use them or not, and they
stay in this conversation for the session, so **`fields` names what you will
actually print** — decided from the answer you are about to write, not from what
might be interesting. Two things to know before you trust a response:

- **`fields` narrows; it cannot widen.** It picks from what the tool already
  returns; a name it does not return is dropped in silence.
- **So an absent field is not an empty value.** A missing date means "not returned
  here", never "this card has no date", and it is NEVER a business fact to report.
  If a field never arrives on any row, say the live system does not supply it and
  stop — do not read it as zero, blank or none.

### Dates

**Never work a date range out yourself** — `dates.py` hands you both halves:

```bash
python /workspace/skills/xas-reporting/dates.py "last week"
{"start": "2026-08-16T21:00:00Z", "end": "2026-08-23T21:00:00Z"}
last week = Mon 17 Aug 2026 to Sun 23 Aug 2026, dealership time
```

Line one is the `CreateDateTime` filter — the only date field to filter a period on
— and line two is the span to tell the planner. It takes today, yesterday,
this/last week, this/last month, this/last year and "last N days"; anything else it
refuses, and then you ask which dates they mean.

## The links

### Naming a record

**Every record you name is a link to its own page — write it yourself.** A detail
page is a path and an id, nothing to encode, so compose it inline as you write.

| Naming a | the label they read | the id that routes | the link |
| --- | --- | --- | --- |
| job card | `JobEntryNum`, the job number | `DMSJCEntry` | `[105374](/job_cards/8745)` |
| vehicle | `LicenseNumber`; `VehicleCode` where there is no plate | `VehicleCode` | `[12-345-67](/vehicles/11370)` |
| customer | `AccountName` | `Accounts.Owner.AccountUUID` on a card, `Id` on an account | `[Hertz](/accounts/655dc47b9c098a054a0791c3)` |

Print the id where the label belongs and you have shown them a number they have
never seen. A card's owner needs no second call: `Accounts.Owner.AccountUUID` IS
that account's `Id`. A record whose id did not come back is named in plain text,
never on a guessed path.

### Linking the set

**Every answer about records ends with a link to the whole set** — one click and
the planner has the real list, sorted, paged and actionable.

```bash
python /workspace/skills/xas-reporting/link.py --tool get_vehicle_list --filter '<the filter you sent>'
python /workspace/skills/xas-reporting/link.py --route <page> --filter '<the filter you sent>'   # job cards
```

- **Vehicles and accounts take `--tool`**: everything those two tools return lists
  on one page, so naming the tool names the page.
- **Job cards take `--route`**, from the `route` column of the classification's
  phrasebook row — never from memory. No classification named, no single page, no
  link to build.
- **Link the query you counted.** One filter, written once into both commands and
  sent in the same block (step 2). Narrow it and re-run: discard the old link, send
  a new pair. After the fact, take the filter from the `source` block the tool
  echoed.
- **Never link a filter you did not run.** Three cards the planner named is
  `{"DMSJCEntry": ["a","b","c"]}`: send it, read the count, then link it.
- **Never hand-write or edit a SET link.** A raw `$` returns an EMPTY page rather
  than an error, and every vehicle and account filter carries one.
- **`Branch: true` and `MyJobCards` cannot be linked** — they mean whoever opens
  the link. Resolve to explicit ids and link those.

## Charts

Asked for a chart? **Read `/workspace/skills/xas-reporting/charts.md` first** —
the recipe is there, and it is what puts a chart on the planner's screen rather
than in a sandbox nobody sees.

## Presenting the answer

Everything above is HOW you got the answer, and none of it belongs in the reply.
Give the figure, what it covers, and anything that changes how they read it:

- **The figure in one line, in their words**, with what it covers: "184 spare-parts
  cards are Open, Haifa branch, July." Name the status you counted — *"are Open"*,
  not *"still open"*, which reads as the wider not-closed span.
- **`name`, never `code`, `id` or a field name.** Where the user gave their own
  wording, echo theirs. A column headed "Code" breaks this as surely as a sentence
  does.
- **A list of customers is a list of links**, not names with one link under them.
- **TWENTY NAMED RECORDS AT MOST, and the link carries the rest.** A long list is
  a table by another name and nobody reads it. Name twenty — the twenty the
  question puts first, or the first twenty that came back where nothing ranks them
  — say how many more there are, and let the set link open all of them: "…and 43
  more — [open all 63](<url>)". Twenty is a ceiling, not a target: three matches
  print three, and a total the planner asked for is never one of the twenty it
  counts.
- **A stored name is ONE string.** `Daniil123` is the name, not "Daniil (account
  123)" — splitting it invents a name nobody stored and puts a code on screen.
- **Never widen a finding past what you filtered.** A count for one account is
  about that account, and a query you did not run is not a finding.
- **Anything that changes the reading**: which term you took their word to mean, an
  unknown status, a count that came back empty, the one question you would need
  answered to go further.
- **The set link last, in a sentence naming what it opens.** "184 spare-parts cards
  are Open, Haifa branch, July — [open the list](<url>)." Not "click here", not a
  bare URL on its own line.

Do not say: a step you took or are about to take, a running total, a cross-check
that passed (one that FAILS is worth a sentence), a pointer at your own output, or
buckets that came back empty unless they asked for them.

| They asked | You print |
| --- | --- |
| A count, a breakdown | the figures, then one link to the set. **No table of cards** |
| "Show me the cards that …" | one line of what is notable in them, then the link. Not the rows |
| One card, one car, one customer | its own page's link, and the facts they asked for |
| A named column — "which customers", "what are the plates" | THAT column, up to TWENTY entries linked, how many more there are, then the set link. Not the other columns |

**Never print a table the link already opens.** A table earns its place only when
the answer IS the shape of the data — a handful of buckets and their counts — and
even then it is the buckets, never the cards inside.

Never say phrasebook, taxonomy, normalize, grep, awk, filter, paging, `totalCount`,
record, row, field, code, ObjectId, UTC, sandbox or token — and no file path, no filename,
no account of what you ran. **The app link is the one exception**: it is
the planner's own system and the answer's other half. If something went wrong, say
it in business terms ("the live system returned nothing for July"), never as a tool
transcript.
