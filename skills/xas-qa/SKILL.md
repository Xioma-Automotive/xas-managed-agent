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
`kind` is `entity` / `classification` / `status`. `code` is what you filter on;
`name` is what you display.

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
   searched this dealership's dictionary for it, and list the nearest entries you
   did find — or say there were none. Then stop.

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
7. **Use `state` and `closed` for lifecycle words.** "closed", "finished",
   "still open", "in progress" resolve to the `closed` flag or the `state`
   bucket (`New` / `In Process` / `Closed` / `Has Alert`) — not to a status name
   you picked. More reliable than matching a label, and it covers the languages
   the status rows have no aliases for.
8. **`unresolved=true` in the index means the dictionary is missing that
   status.** Those rows have no `name` and no `state`. Report them as "unknown
   status (code NN)" and count them separately. Never invent a label.
9. **Everything listed is active.** Inactive classifications are already
   omitted; no liveness check is needed.
10. **Parsing the raw index directly?** Strings are quoted (`name="Closed"`) but
    booleans and counts are not (`closed=true`, `fields=649`). A
    `(\w+)="([^"]*)"` regex silently drops every boolean, `closed` included. The
    phrasebook has already handled this — one more reason to use it.

## Getting the number — filter, never page

Every `get_job_cards` response carries **`totalCount` for the filter you sent**,
independent of how many rows come back. That is the count. Ask for one row and
read it:

```
get_job_cards  filter: {…}  paging: {"count": 1}   ->  totalCount
```

**Never page through records to compute an aggregate.** A breakdown is one
filtered call per bucket, each with `count: 1` — ten buckets is ten cheap calls.
Pulling the rows instead costs roughly forty times as much, because every record
you fetch stays in this conversation and is re-read on every later turn of the
session. It also arrives padded: a job card carries eleven account-role objects
and the owner's whole contact list, none of which a count needs.

| Goal | Call |
| --- | --- |
| One count | `filter: {…}`, `paging: {"count": 1}` -> `totalCount` |
| Breakdown by classification | one call per `code`: `filter: {"JobClassification": "<code>"}` |
| Breakdown by status | one call per status `id`: `filter: {"JobStatus.ID": "<id>"}` |
| Still-open only | `filter: {"OpenJobCards": true}` |
| The buckets to loop over | the phrasebook, not memory: `awk -F'\t' '$4=="classification" && $5=="JobCard" {print $7}' /workspace/phrasebook.tsv \| sort -u` |

Fetch actual rows only when the planner wants to **see** cards — then keep
`paging.count` small and name the ones you show.

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

Then name the file in your reply and stop. **Do not read the chart back.**
Reading it returns the whole file into the conversation — tens of thousands of
tokens to tell you what you just plotted.

## Presenting the answer

Translate back through the phrasebook: show `name`, never `code`, `id`, or an
internal field name. Where the user supplied their own wording (an alias, or a
term whose `name` is a local placeholder like `Distinct_name`), echo **their**
wording — it is what they will recognise. Chart axis labels and legends follow
the same rule.
