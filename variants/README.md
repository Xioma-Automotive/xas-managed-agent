# Prompt + skill variants

One env var swaps the system prompt and the reporting `SKILL.md`:

```bash
uv run python setup_agent.py                   # the shipped pair (minimal)
XAS_VARIANT=full uv run python setup_agent.py  # variants/full/
```

Each variant directory holds exactly two files, and both must be present:

| File | Replaces |
| --- | --- |
| `system-prompt.md` | `setup_agent.SYSTEM_PROMPT` |
| `xas-reporting.SKILL.md` | `skills/xas-reporting/SKILL.md` |

Nothing else changes: `resolve.py`, `dates.py`, `charts.md`, the rendered
`phrasebook.tsv` and the whole `xas-allocation` bundle ship the same either way,
and `setup_agent.py` prints which pair it deployed. Switching back is the same
command without the variable — both are re-runnable in place.

## What ships (no variable)

The minimal pair, promoted out of `variants/minimal/` on 2026-09-06. It says what
each component is FOR and leaves the reasoning to the model: it keeps only the
rules that are not the model's to decide — what rides in the first block, the two
kinds of link, and never showing the kitchen — and drops the procedures the full
pair spells out (which call to send, how to read a lookup, how to bound a page,
how to present a figure). Its prompt says nothing about allocation: the
`xas-allocation` skill's own description is what routes to it.

Its prompt also carries the tenant's card, vehicle and account TYPES inline, so
no session spends a lookup on one. `{{CLASSIFICATIONS}}` is substituted at deploy
time from `skills/xas-reporting/index.md` — never edit the list by hand, edit the
taxonomy and redeploy. That is also what takes the type rows OUT of the shipped
table (244 rows, not 373): statuses, branches and states are still looked up.

## `full`

The pair this replaced, archived here on 2026-09-06 and still deployable for
comparison. Its prompt carries no type list, so `XAS_VARIANT=full` ships the
373-row table with the classifications in it — there is nowhere else for that
pair to resolve a type from.

`tests/test_agent_contract.py` pinned it rule by rule until the swap; those 33
phrase tests were deleted with the promotion, so the suite now pins wiring only
and NEITHER pair's prose. Verify a pair by hand — `docs/evals/routing.md` is the
routing check.
