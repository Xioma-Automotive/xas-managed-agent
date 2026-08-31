# What the reporting lane needs from `xas-app-mcp`

Three asks, all of them things no rule in this repo can fix, because they are
properties of what the tools RETURN. Each one is measured against the dev tenant,
not estimated — reproduce every figure with `uv run python -m appmcp` (see
`appmcp-connect.md`). `xas-app-mcp` is a different repo; this file is the request,
and it goes away when the request is answered.

Why response size is worth a change request at all: every byte a tool returns
enters the agent's conversation and is re-read on **every later model request** in
the session. One 51-card answer was read four more times before the turn ended.
The skill can choose fewer rows and fewer columns; it cannot choose what a column
weighs.

## 1. Let `fields` name a sub-field of `Accounts.*`

Asked for the customers behind 51 service cards, the only way to get a customer's
name is `fields: ["Accounts.Owner"]`, and that returns the whole owner object:

| Sub-field | Rows carrying it (of 51) |
| --- | --- |
| `AccountUUID` | 51 |
| `AccountName` | 51 |
| `AccountDMSCode` | 51 |
| `AccountPhone1` | 51 |
| `AccountEMail` | 34 |
| `AccountFederalId` | 18 |

14,652 bytes of owner objects (~8.8k tokens as the agent measured it) to print 17
names. The names alone are 3,409 bytes — **77% of it is waste**, and the waste
includes 51 phone numbers and 34 e-mail addresses that then sit in a model
transcript for the rest of the session to print no phone numbers and no e-mail
addresses.

**Ask:** accept `Accounts.Owner.AccountName` (and the same for the other
`Accounts.*` roles) in the `fields` enum. Today the enum stops at the role, so a
dotted sub-path is a schema violation, and `fields` cannot widen or reshape what
it picks from.

Note the asymmetry that makes this surprising: `Accounts.Owner.AccountDMSCode` is
already a documented **filter** key. Filtering on a sub-field works; asking to see
one does not.

## 2. Drop the `states` block unless it is asked for

Every `get_job_list` response appends five state objects — `Locales`, `Color`,
`__v`, `CompanyDB`, `_id` and `Id` for the same value, and `Count: 0` on all five:

```
1,523 bytes per response (~400 tokens), on every job-card call, always identical
```

Nothing reads it. `appmcp.py` strips it by default and needs `--raw` to keep it,
which is the local admission that it says nothing — but the agent talks to the MCP
directly and gets it raw, twice in a two-call turn.

**Ask:** omit it when `fields` is sent, or put it behind an `include` the way
`get_*_details` handles sub-resources. Either is fine; the current behaviour is the
only one that cannot be opted out of.

## 3. Return the list-page URL in `source`

Recorded already in `CLAUDE.md` under the link rules; repeated here because it
belongs with the others. A reporting answer ends in a link to the page behind the
number, and `link.py` builds that URL agent-side from the filter — which means the
sandbox holds a second copy of the tenant's route table and its two URL dialects,
and a raw `$` in the query string returns an EMPTY page rather than an error.

**Ask:** put the URL in the `source` block the response already echoes. `source`
holds the filter, the paging and the sort; the server knows the tenant and the
endpoint. Then nothing is built agent-side and the `$` cannot be got wrong.

## Not asked for, deliberately

- **A `distinct` or `group by`.** It would have turned this turn's 51-row pull into
  17 values, and it is the biggest win of the four. But it is a new query surface
  with its own semantics to agree, where the three above are all subtractive. If
  the tally case keeps coming up, this is the follow-up.
- **A higher `paging.count`.** 200 is plenty; the 50 that cost a round trip on
  2026-08-31 was our own number, and it is now 200 in the skill.
- **`OpenJobCards`**, which returns 0 regardless of the data. The tool description
  already says so and the skill routes around it via status ids. Worth fixing
  upstream, but it costs us nothing today.
