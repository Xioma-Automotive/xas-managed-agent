# Prompt + skill variants

One env var swaps the system prompt and the reporting `SKILL.md`:

```bash
uv run python setup_agent.py                      # full pair (default)
XAS_VARIANT=minimal uv run python setup_agent.py  # variants/minimal/
```

Each variant directory holds exactly two files, and both must be present:

| File | Replaces |
| --- | --- |
| `system-prompt.md` | `setup_agent.SYSTEM_PROMPT` |
| `xas-reporting.SKILL.md` | `skills/xas-reporting/SKILL.md` |

Nothing else changes: `resolve.py`, `dates.py`, `link.py`, `charts.md`, the
rendered `phrasebook.tsv` and the whole `xas-allocation` bundle ship the same
either way, and `setup_agent.py` prints which pair it deployed. Switching back is
the same command without the variable — both are re-runnable in place.

## `minimal`

A deliberate experiment: say what each component is FOR and leave the reasoning
to the model. It keeps only the rules that are not the model's to decide — the two
kinds of link, and never showing the kitchen — and drops every procedure the full
pair spells out (which call to send, how to read a lookup, how to bound a page,
how to present a figure). Its prompt says nothing about allocation at all: the
`xas-allocation` skill's own description is what routes to it.

Its prompt also carries the tenant's card, vehicle and account TYPES inline, so
no session spends a lookup on one. `{{CLASSIFICATIONS}}` is substituted at deploy
time from `skills/xas-reporting/index.md` — never edit the list by hand, edit the
taxonomy and redeploy. Statuses, branches and states are still looked up.

Compare it against the full pair by hand — `docs/evals/routing.md` is the routing
check, and `tests/test_agent_contract.py` pins the FULL pair only, so it passes
under either and proves nothing about a variant.
